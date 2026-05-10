"""Environment guards and metadata capture for the perf framework.

Guards run at tool startup and warn (or, under ``--strict``, fail) when the
machine is in a state likely to produce noisy measurements: on battery,
under load, or thermally throttled. The metadata block is embedded in every
report so a reviewer can tell at a glance whether two runs are comparable.
"""

import os
import platform
import subprocess
import sys
from datetime import datetime, timezone

import psutil

from py4j.version import __version__ as PY4J_VERSION


class EnvironmentWarning(str):
    """Marker type so callers can distinguish warnings from random strings."""


def _run(cmd, timeout=2):
    """Run a short shell command, return stdout stripped. Return '' on error."""
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout)
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return ""


def _git_info():
    """Best-effort git rev / branch / dirty flag for the repository."""
    rev = _run(["git", "rev-parse", "--short", "HEAD"])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["git", "status", "--porcelain"])
    return {
        "rev": rev or "unknown",
        "branch": branch or "unknown",
        "dirty": bool(status),
    }


def _java_version():
    """Return a one-line java -version string, or 'unknown'."""
    # java -version writes to stderr on most JDKs
    try:
        result = subprocess.run(
            ["java", "-version"], capture_output=True, timeout=3)
        out = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace").strip()
        first_line = out.split("\n", 1)[0] if out else ""
        return first_line or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"


def _cpu_model():
    """Best-effort CPU model name across platforms.

    ``platform.processor()`` is useless on macOS arm64 ('arm') and often
    uninformative on Linux. Prefer platform-specific sysctl/cpuinfo.
    """
    system = platform.system()
    if system == "Darwin":
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name:
            return name
    elif system == "Linux":
        try:
            with open("/proc/cpuinfo", "r") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except (OSError, IndexError):
            pass
    return platform.processor() or platform.machine() or "unknown"


def _cpu_description():
    """Human-readable CPU line: model, physical cores, logical cores."""
    model = _cpu_model()
    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False) or 0
    try:
        freq = psutil.cpu_freq()
        # Apple Silicon reports freq.max = 0; skip in that case.
        max_ghz = (" @ {0:.2f} GHz".format(freq.max / 1000.0)
                   if freq and freq.max else "")
    except (AttributeError, OSError, NotImplementedError):
        max_ghz = ""
    return "{0}{1}, {2} physical / {3} logical cores".format(
        model, max_ghz, physical, logical)


def capture_metadata():
    """Snapshot of the environment, embedded into every report."""
    git = _git_info()
    return {
        "os": "{0} {1} ({2})".format(
            platform.system(), platform.release(), platform.machine()),
        "cpu": _cpu_description(),
        "ram_bytes": psutil.virtual_memory().total,
        "python": "{0} ({1})".format(
            sys.version.split()[0], platform.python_implementation()),
        "java": _java_version(),
        "py4j_version": PY4J_VERSION,
        "git_rev": git["rev"],
        "git_branch": git["branch"],
        "git_dirty": git["dirty"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }


def _check_battery():
    """Warn if running on battery power (CPU frequency may be throttled)."""
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError):
        return None
    if battery is None:
        return None
    if not battery.power_plugged:
        return EnvironmentWarning(
            "running on battery power (laptop CPUs often throttle when "
            "unplugged; plug in for stable numbers)")
    return None


def _check_load_average():
    """Warn if the 1-minute load average is over half the core count."""
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    cores = os.cpu_count() or 1
    threshold = 0.5 * cores
    if load_1m > threshold:
        return EnvironmentWarning(
            "1-minute load average is {0:.2f} (threshold {1:.2f} = 0.5 x "
            "{2} cores); another workload is consuming CPU".format(
                load_1m, threshold, cores))
    return None


def _check_thermal_macos():
    """Best-effort macOS thermal check via pmset."""
    if platform.system() != "Darwin":
        return None
    out = _run(["pmset", "-g", "therm"])
    if not out:
        return None
    for line in out.splitlines():
        if "CPU_Speed_Limit" in line and "=" in line:
            value = line.split("=", 1)[1].strip()
            if value.isdigit() and int(value) < 100:
                return EnvironmentWarning(
                    "macOS thermal throttling active (CPU_Speed_Limit="
                    "{0}%)".format(value))
    return None


def check_guards():
    """Run all guards and return a list of warnings (may be empty)."""
    warnings = []
    for check in (_check_battery, _check_load_average, _check_thermal_macos):
        result = check()
        if result is not None:
            warnings.append(result)
    return warnings


def current_nice():
    """Return the process's current nice value, or ``None`` if unavailable."""
    try:
        return os.nice(0)
    except (OSError, AttributeError):
        return None


def try_renice(target_nice=-15, verbose=True):
    """Attempt to raise this process's priority via ``sudo renice``.

    Child processes spawned after this call (the per-scenario JVMs)
    inherit the new nice value, so the actual scenario work runs at
    the elevated priority. Failures are non-fatal - if sudo isn't
    available, the user cancels the password prompt, or we're on an
    OS without nice, we warn and continue at the original priority.

    Returns a dict describing the outcome, suitable for stashing into
    the report metadata so comparisons can tell whether both runs had
    the same priority treatment:
        {"attempted", "succeeded", "before", "after", "target", "reason"}
    """
    result = {
        "attempted": False, "succeeded": False,
        "before": None, "after": None,
        "target": target_nice, "reason": "",
    }
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        result["reason"] = "unsupported-os:{0}".format(system)
        if verbose:
            print("note: renice not attempted on {0}".format(system),
                  file=sys.stderr)
        return result

    before = current_nice()
    result["before"] = before
    if before is None:
        result["reason"] = "nice-not-readable"
        return result
    if before <= target_nice:
        result["succeeded"] = True
        result["after"] = before
        result["reason"] = "already-at-or-below-target"
        if verbose:
            print("Already at nice={0}; skipping renice to {1}."
                  .format(before, target_nice))
        return result

    pid = os.getpid()
    prompt = ("sudo password (renice perf run to {0} for cleaner numbers; "
              "pass --no-renice to skip): ".format(target_nice))
    cmd = ["sudo", "-p", prompt,
           "renice", "-n", str(target_nice), str(pid)]
    result["attempted"] = True
    if verbose:
        print("Elevating priority: sudo renice -n {0} {1}"
              .format(target_nice, pid))
    try:
        completed = subprocess.run(cmd)
    except FileNotFoundError:
        result["reason"] = "sudo-not-found"
        if verbose:
            print("warning: sudo not found; continuing at nice={0}"
                  .format(before), file=sys.stderr)
        return result
    except KeyboardInterrupt:
        result["reason"] = "user-cancelled"
        if verbose:
            print("warning: renice cancelled; continuing at nice={0}"
                  .format(before), file=sys.stderr)
        return result

    if completed.returncode != 0:
        result["reason"] = "renice-exit-{0}".format(completed.returncode)
        if verbose:
            print("warning: renice failed (exit {0}); continuing at "
                  "nice={1}".format(completed.returncode, before),
                  file=sys.stderr)
        return result

    after = current_nice()
    result["after"] = after
    result["succeeded"] = True
    result["reason"] = "ok"
    if verbose:
        print("Priority now at nice={0}.".format(after))
    return result
