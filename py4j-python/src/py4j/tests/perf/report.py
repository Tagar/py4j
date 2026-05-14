"""Report I/O: JSON read/write and single-run markdown writer.

Comparison logic (``--compare``) is added in phase 4; this phase 1 skeleton
covers the shape and the single-run path so end-to-end flow can be
exercised early.

JSON schema (version 1.0)::

    {
      "version": "1.0",
      "environment": { ... see environment.capture_metadata() ... },
      "warnings": ["..."],
      "scenarios": [
        {
          "id": "M1",
          "name": "static_call_no_args",
          "runner": "pytest-benchmark" | "macro",
          "unit": "seconds",
          "warmup_rounds": int,
          "measured_rounds": int,
          "iterations_per_round": int,
          "budget_triggered": bool,
          "rounds": [float, ...],
          "stats": {
            "min", "max", "mean", "median", "stddev", "iqr",
            "p5", "p95", "p99"
          }
        }, ...
      ]
    }
"""

import bisect
import json
import random
import statistics
import sys
from collections import namedtuple
from typing import Any, Dict, List, Optional, Tuple

from py4j.tests.perf import REPORT_SCHEMA_VERSION
# Re-export inference primitives and stats helpers so existing call
# sites and tests that do `from py4j.tests.perf.report import _xxx`
# continue to work after the v2 split into `stats.py` and
# `inference.py`.
from py4j.tests.perf.stats import compute_stats, noise_fraction
from py4j.tests.perf.inference import (
    REGRESSION_PCT as _REGRESSION_PCT,
    IMPROVEMENT_PCT as _IMPROVEMENT_PCT,
    NOISE_MULTIPLIER as _NOISE_MULTIPLIER,
    PVALUE_THRESHOLD as _PVALUE_THRESHOLD,
    BOOTSTRAP_RESAMPLES as _BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_MAX_SAMPLES_PER_SIDE as _BOOTSTRAP_MAX_SAMPLES_PER_SIDE,
    BOOTSTRAP_RANDOM_SEED as _BOOTSTRAP_RANDOM_SEED,
    _mannwhitney_u,
    _ks_two_sample,
    _hodges_lehmann_2sample,
    _compute_bootstrap_deltas,
    _ci_from_deltas,
    _rope_probabilities,
    _bootstrap_median_delta_ci,
    _stats_verdict,
    _confident_verdict,
    _verdict,
    sprt_decide,
)
# Local alias used in this module for the heuristic-fallback path.
_noise_fraction = noise_fraction


ComparisonResult = namedtuple(
    "ComparisonResult",
    ["markdown", "faster_ids", "regressed_ids",
     "inconclusive_ids", "neutral_ids", "missing_ids", "new_ids"],
)


def _compute_metrics(rounds: List[float], cpu_rounds: Optional[List[float]],
                     iterations_per_round: int, errors: int,
                     bytes_per_iteration: Optional[int]) -> Dict[str, Any]:
    """Stage-3a derived metrics from existing per-round timings.

    Throughput, per-op latency, CPU/wall ratio, and bandwidth are
    all functions of the round duration plus per-scenario constants.
    Computing them in one place keeps the schema consistent — every
    consumer (markdown rendering, comparison, downstream tooling)
    reads from this block instead of re-deriving.

    Returns a dict with these keys; values are None when not applicable
    (e.g. bandwidth without a declared ``bytes_per_iteration``):
      - ``latency_per_op_s``: median seconds per logical operation
      - ``throughput_ops_per_s``: median operations per second
      - ``bandwidth_bytes_per_s``: median bytes per second
      - ``cpu_time_ratio``: median(cpu_round / wall_round); ~1.0 means
        CPU-bound, ~0.1 means largely waiting (IO or sleep)
      - ``errors``: count of measure() exceptions across all rounds
    """
    metrics: Dict[str, Any] = {
        "latency_per_op_s": None,
        "throughput_ops_per_s": None,
        "bandwidth_bytes_per_s": None,
        "cpu_time_ratio": None,
        "errors": errors,
    }
    if rounds and iterations_per_round > 0:
        median_round = statistics.median(rounds)
        if median_round > 0:
            metrics["latency_per_op_s"] = median_round / iterations_per_round
            metrics["throughput_ops_per_s"] = iterations_per_round / median_round
            if bytes_per_iteration and bytes_per_iteration > 0:
                metrics["bandwidth_bytes_per_s"] = (
                    iterations_per_round * bytes_per_iteration / median_round)
    if cpu_rounds and rounds and len(cpu_rounds) == len(rounds):
        ratios = [c / w for c, w in zip(cpu_rounds, rounds) if w > 0]
        if ratios:
            metrics["cpu_time_ratio"] = statistics.median(ratios)
    return metrics


def build_scenario_entry(
    scenario_id: str,
    name: str,
    runner: str,
    rounds: List[float],
    warmup_rounds: int,
    iterations_per_round: int = 1,
    budget_triggered: bool = False,
    adaptive_stopped_early: bool = False,
    sprt_decision: Optional[str] = None,
    expected_cv: Optional[float] = None,
    cpu_rounds: Optional[List[float]] = None,
    errors: int = 0,
    bytes_per_iteration: Optional[int] = None,
    unit: str = "seconds",
) -> Dict[str, Any]:
    """Construct one scenario record matching the JSON schema.

    ``expected_cv`` is the scenario's declared noise budget (e.g. 0.10
    means "I expect at most a 10 % coefficient of variation across
    rounds"). If observed CV > 2 * expected_cv, the report flags this
    scenario as exceeding its budget — useful signal that the scenario
    or the runner environment is too unstable for the verdict to be
    trustworthy.

    ``cpu_rounds`` (Stage 3a) is the per-round process CPU time
    measured via ``time.process_time()``. Used to derive the
    cpu_time_ratio metric (CPU-bound vs IO/sleep-bound).

    ``bytes_per_iteration`` (Stage 3a) is the per-iteration data
    transfer size, used to derive bandwidth (bytes/sec). None for
    scenarios that don't transfer data.

    ``errors`` is the count of ``measure()`` calls that raised an
    exception during measurement (not aborting; counted, included
    in the report).
    """
    stats = compute_stats(rounds)
    cv = (stats["stddev"] / stats["mean"]) if stats["mean"] > 0 else 0.0
    over_budget = (expected_cv is not None
                   and cv > 2.0 * expected_cv)
    metrics = _compute_metrics(
        rounds, cpu_rounds, iterations_per_round, errors, bytes_per_iteration)
    return {
        "id": scenario_id,
        "name": name,
        "runner": runner,
        "unit": unit,
        "warmup_rounds": warmup_rounds,
        "measured_rounds": len(rounds),
        "iterations_per_round": iterations_per_round,
        "budget_triggered": budget_triggered,
        "adaptive_stopped_early": adaptive_stopped_early,
        "sprt_decision": sprt_decision,
        "expected_cv": expected_cv,
        "observed_cv": cv,
        "noise_over_budget": over_budget,
        "rounds": rounds,
        "cpu_rounds": cpu_rounds,
        "stats": stats,
        "metrics": metrics,
    }


def build_report(environment, warnings, scenarios):
    """Top-level report dict ready to be JSON-serialized."""
    return {
        "version": REPORT_SCHEMA_VERSION,
        "environment": environment,
        "warnings": list(warnings),
        "scenarios": list(scenarios),
    }


def write_json(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def compact_report(report: dict, sample_size: int = 1000) -> dict:
    """Return a copy of `report` with ``rounds`` downsampled to ``sample_size``.

    Full reports can reach hundreds of megabytes on non-quick runs because
    every individual round timing is retained. We previously stripped
    ``rounds`` entirely; that made compact baselines tiny (~16 KB) but
    crippled downstream statistical methods (bootstrap CIs and Mann-Whitney
    U tests both need the actual per-round samples).

    Compromise: keep a uniformly-random sample of up to ``sample_size``
    rounds. 1000 samples per scenario gives bootstrap CIs within a fraction
    of a percent of "true" CIs (computed against all rounds) and is more
    than enough for Mann-Whitney to detect real differences. JSON size
    stays under 1 MB for the full 24-scenario suite.

    Older compact reports (with ``rounds: []``) remain readable; the
    comparison layer falls back to the heuristic median+noise verdict
    when round samples are absent.
    """
    import copy
    out = copy.deepcopy(report)
    out["compact"] = True
    rng = random.Random(_BOOTSTRAP_RANDOM_SEED)
    for s in out.get("scenarios", []):
        all_rounds = s.get("rounds", []) or []
        s["rounds_count"] = len(all_rounds)
        if len(all_rounds) > sample_size:
            s["rounds"] = rng.sample(all_rounds, sample_size)
        # else keep all rounds as-is
    return out


def _human_duration(seconds: float) -> str:
    """Scale a duration in seconds to a human-readable string."""
    if seconds >= 1.0:
        return "{0:.3f} s".format(seconds)
    if seconds >= 1e-3:
        return "{0:.3f} ms".format(seconds * 1e3)
    if seconds >= 1e-6:
        return "{0:.3f} \u00b5s".format(seconds * 1e6)
    return "{0:.3f} ns".format(seconds * 1e9)

def write_markdown(report: dict, path: str) -> None:
    """Single-run markdown report. Comparison markdown comes in phase 4."""
    env = report["environment"]
    scenarios = report["scenarios"]
    warnings = report.get("warnings", [])

    lines = []
    lines.append("# py4j perf report")
    lines.append("")
    lines.append("**Branch:** {0} (rev {1}{2})  ".format(
        env.get("git_branch", "?"), env.get("git_rev", "?"),
        ", dirty" if env.get("git_dirty") else ""))
    lines.append("**Timestamp:** {0}  ".format(
        env.get("timestamp_utc", "?")))
    lines.append("**OS / CPU:** {0} - {1}  ".format(
        env.get("os", "?"), env.get("cpu", "?")))
    lines.append("**RAM:** {0:.1f} GB  ".format(
        env.get("ram_bytes", 0) / (1024 ** 3)))
    lines.append("**Python / Java:** {0} / {1}  ".format(
        env.get("python", "?"), env.get("java", "?")))
    lines.append("**py4j:** {0}".format(env.get("py4j_version", "?")))
    renice = env.get("renice")
    if renice:
        if renice.get("succeeded"):
            lines.append("**Process priority:** nice={0} "
                         "(reniced from {1})".format(
                             renice.get("after"), renice.get("before")))
        else:
            lines.append("**Process priority:** nice={0} "
                         "(renice not applied: {1})".format(
                             renice.get("before"), renice.get("reason")))
    lines.append("")
    if warnings:
        lines.append("**Environment warnings:**")
        for w in warnings:
            lines.append("- {0}".format(w))
        lines.append("")

    lines.append("| ID | Scenario | Median | Latency/op | Throughput | Bandwidth | CPU/wall | p95 | Stddev | Noise | Rounds | CV vs budget | Errors |")
    lines.append("|----|----------|--------|------------|------------|-----------|----------|-----|--------|-------|--------|--------------|--------|")
    noisy = []
    for s in scenarios:
        stats = s["stats"]
        noise = _noise_fraction(stats) * 100
        rounds_cell = "{0}{1}{2}".format(
            s["measured_rounds"],
            " (budget)" if s.get("budget_triggered") else "",
            " (adaptive)" if s.get("adaptive_stopped_early") else "")
        expected_cv = s.get("expected_cv")
        observed_cv = s.get("observed_cv", 0.0) or 0.0
        if expected_cv is None or expected_cv <= 0:
            budget_cell = "n/a"
        else:
            ratio = observed_cv / expected_cv
            marker = " !!" if s.get("noise_over_budget") else ""
            budget_cell = "{0:.2f}x{1}".format(ratio, marker)
            if s.get("noise_over_budget"):
                noisy.append(s["id"])
        metrics = s.get("metrics") or {}
        lat_cell = _fmt_latency(metrics.get("latency_per_op_s"))
        tput_cell = _fmt_throughput(metrics.get("throughput_ops_per_s"))
        bw_cell = _fmt_bandwidth(metrics.get("bandwidth_bytes_per_s"))
        cpu_cell = _fmt_cpu_ratio(metrics.get("cpu_time_ratio"))
        err_cell = str(metrics.get("errors", 0)) if metrics.get("errors") else "0"
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9:.1f}% | {10} | {11} | {12} |".format(
                s["id"],
                s["name"],
                _human_duration(stats["median"]),
                lat_cell,
                tput_cell,
                bw_cell,
                cpu_cell,
                _human_duration(stats["p95"]),
                _human_duration(stats["stddev"]),
                noise,
                rounds_cell,
                budget_cell,
                err_cell,
            ))
    lines.append("")
    lines.append(
        "*Noise = (p95 - p5) / median within a single run.*")
    lines.append(
        "*CV vs budget = observed coefficient of variation divided by "
        "the scenario's declared `expected_cv`. Values >= 2x are flagged "
        "with `!!` — either the scenario is unstable or the runner "
        "environment is too noisy for the verdict to be trusted.*")
    if noisy:
        lines.append("")
        lines.append("**{0} scenario(s) over noise budget**: {1}. "
                     "Consider --strict-bench, more --n-runs, or a "
                     "quieter machine.".format(
                         len(noisy), ", ".join(noisy)))
    lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ==================================================================== compare

_CRITICAL_ENV_KEYS = ("os", "cpu", "python", "java", "py4j_version")


def _env_mismatch(baseline_env, current_env):
    """Return a list of (key, baseline_val, current_val) for mismatched keys."""
    mismatches = []
    for key in _CRITICAL_ENV_KEYS:
        bval = baseline_env.get(key, "?")
        cval = current_env.get(key, "?")
        if bval != cval:
            mismatches.append((key, bval, cval))
    return mismatches


def _fmt_pct(pct):
    if pct is None:
        return "n/a"
    sign = "+" if pct > 0 else ""
    return "{0}{1:.1%}".format(sign, pct)


def _fmt_pvalue(p):
    if p is None:
        return "n/a"
    if p == 0.0 or p < 1e-200:
        return "<1e-200"
    if p < 1e-3:
        return "{0:.1e}".format(p)
    return "{0:.3f}".format(p)


def _fmt_latency(seconds):
    """Format per-op latency in the most appropriate unit."""
    if seconds is None or seconds <= 0:
        return "n/a"
    return _human_duration(seconds)


def _fmt_throughput(ops_per_s):
    """Format throughput as e.g. '4.5 k ops/s' or '12 M ops/s'."""
    if ops_per_s is None or ops_per_s <= 0:
        return "n/a"
    if ops_per_s >= 1e9:
        return "{0:.2f} G ops/s".format(ops_per_s / 1e9)
    if ops_per_s >= 1e6:
        return "{0:.2f} M ops/s".format(ops_per_s / 1e6)
    if ops_per_s >= 1e3:
        return "{0:.2f} k ops/s".format(ops_per_s / 1e3)
    return "{0:.2f} ops/s".format(ops_per_s)


def _fmt_bandwidth(bytes_per_s):
    """Format bandwidth in MB/s, GB/s, etc."""
    if bytes_per_s is None or bytes_per_s <= 0:
        return "n/a"
    if bytes_per_s >= 1024 ** 3:
        return "{0:.2f} GB/s".format(bytes_per_s / 1024 ** 3)
    if bytes_per_s >= 1024 ** 2:
        return "{0:.2f} MB/s".format(bytes_per_s / 1024 ** 2)
    if bytes_per_s >= 1024:
        return "{0:.2f} KB/s".format(bytes_per_s / 1024)
    return "{0:.0f} B/s".format(bytes_per_s)


def _fmt_cpu_ratio(ratio):
    """Format CPU/wall ratio. ~1.0 = CPU-bound; ~0.1 = mostly waiting."""
    if ratio is None:
        return "n/a"
    return "{0:.2f}".format(ratio)


def _fmt_prob(p):
    """Format a region-of-practical-equivalence probability (0..1).

    Returns "n/a" when the value is missing, otherwise a 2-decimal
    fraction. We avoid percent formatting here because the column
    already lives in a row of percentages (delta, CI); reading
    "0.87" as "87% of the bootstrap distribution" is unambiguous.
    """
    if p is None:
        return "n/a"
    return "{0:.2f}".format(p)


def _fmt_verdict(verdict):
    return {
        "faster": "**faster**",
        "regression": "**regression**",
        "inconclusive": "inconclusive",
        "neutral": "neutral",
    }.get(verdict, verdict)


def compare(baseline_report, current_report):
    """Diff two reports and return a ComparisonResult.

    Scenarios are matched by ID. The output markdown includes an
    environment header for both runs, a warning block if critical
    environment keys differ, and a per-scenario diff table with
    verdicts.
    """
    baseline_scenarios = {s["id"]: s for s in baseline_report["scenarios"]}
    current_scenarios = {s["id"]: s for s in current_report["scenarios"]}

    baseline_env = baseline_report.get("environment", {})
    current_env = current_report.get("environment", {})
    mismatches = _env_mismatch(baseline_env, current_env)

    baseline_ids = set(baseline_scenarios)
    current_ids = set(current_scenarios)
    common_ids = baseline_ids & current_ids
    missing_ids = sorted(baseline_ids - current_ids)
    new_ids = sorted(current_ids - baseline_ids)

    faster, regressed, inconclusive, neutral = [], [], [], []

    lines = []
    lines.append("# py4j perf comparison")
    lines.append("")
    lines.append("**Baseline:** {0} (rev {1}, {2})  ".format(
        baseline_env.get("git_branch", "?"),
        baseline_env.get("git_rev", "?"),
        baseline_env.get("timestamp_utc", "?")))
    lines.append("**Current:**  {0} (rev {1}, {2})".format(
        current_env.get("git_branch", "?"),
        current_env.get("git_rev", "?"),
        current_env.get("timestamp_utc", "?")))
    lines.append("")

    if mismatches:
        lines.append("> **WARNING - environment mismatch between runs.**")
        lines.append("> A comparison across different machines / JVMs / "
                     "Pythons is rarely meaningful.")
        lines.append(">")
        lines.append("> | Key | Baseline | Current |")
        lines.append("> |-----|----------|---------|")
        for key, bval, cval in mismatches:
            lines.append("> | {0} | {1} | {2} |".format(key, bval, cval))
        lines.append("")

    # Catch the easy mistake of running --compare without applying the change
    # first: baseline and current were captured from the same commit, so the
    # deltas you see below are run-to-run noise, not the effect of any code
    # change. (For an intentional noise-floor characterization the warning is
    # informational; for an optimization PR it means you forgot to cherry-pick.)
    baseline_rev = baseline_env.get("git_rev")
    current_rev = current_env.get("git_rev")
    if (baseline_rev and current_rev
            and baseline_rev != "?" and baseline_rev == current_rev):
        lines.append(
            "> **WARNING - same git rev on both sides ({0}).**".format(
                baseline_rev))
        lines.append("> Baseline and Current were captured from the same "
                     "commit. Any deltas below reflect run-to-run noise, not "
                     "the effect of a code change. If you intended to compare "
                     "two versions, make sure the change is committed or "
                     "cherry-picked before running `--compare`.")
        lines.append("")
        sys.stderr.write(
            "WARNING: --compare detected identical git rev ({0}) on baseline "
            "and current. Deltas reflect run-to-run noise, not code change "
            "effects.\n".format(baseline_rev))

    current_warnings = current_report.get("warnings", [])
    if current_warnings:
        lines.append("**Current-run warnings:**")
        for w in current_warnings:
            lines.append("- {0}".format(w))
        lines.append("")

    lines.append("| ID | Scenario | n (B/C) | Baseline median | Current median | "
                 "Delta median | 95% CI | M-W p | P(better) | P(same) | P(worse) | Verdict |")
    lines.append("|----|----------|---------|-----------------|----------------|"
                 "--------------|--------|-------|-----------|---------|----------|---------|")

    def pct_of(b, c):
        return (c - b) / b if b > 0 else 0.0

    used_heuristic = False

    for sid in sorted(common_ids, key=_sort_key):
        b_sc = baseline_scenarios[sid]
        c_sc = current_scenarios[sid]
        b_stats = b_sc["stats"]
        c_stats = c_sc["stats"]
        verdict, info = _verdict(b_sc, c_sc)

        if verdict == "faster":
            faster.append(sid)
        elif verdict == "regression":
            regressed.append(sid)
        elif verdict == "inconclusive":
            inconclusive.append(sid)
        else:
            neutral.append(sid)

        delta_median = pct_of(b_stats["median"], c_stats["median"])

        if info is not None and info.get("method", "").startswith("bootstrap+mannwhitney"):
            ci_cell = "[{0}, {1}]".format(
                _fmt_pct(info["ci_lo"]), _fmt_pct(info["ci_hi"]))
            p_cell = _fmt_pvalue(info.get("p_value"))
            p_better = _fmt_prob(info.get("p_better"))
            p_same = _fmt_prob(info.get("p_same"))
            p_worse = _fmt_prob(info.get("p_worse"))
        else:
            used_heuristic = True
            noise_pct = max(_noise_fraction(b_stats),
                            _noise_fraction(c_stats)) * 100
            ci_cell = "noise {0:.1f}%".format(noise_pct)
            p_cell = "n/a"
            p_better = "n/a"
            p_same = "n/a"
            p_worse = "n/a"

        n_cell = "{0}/{1}".format(
            b_sc.get("measured_rounds", len(b_sc.get("rounds") or [])),
            c_sc.get("measured_rounds", len(c_sc.get("rounds") or [])))
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} |"
                     .format(
                         sid, c_sc["name"], n_cell,
                         _human_duration(b_stats["median"]),
                         _human_duration(c_stats["median"]),
                         _fmt_pct(delta_median),
                         ci_cell, p_cell,
                         p_better, p_same, p_worse,
                         _fmt_verdict(verdict)))

    lines.append("")
    lines.append("*Verdict: confident faster/regression requires "
                 "Mann-Whitney p < {0}, bootstrap 95% CI excluding zero, "
                 "AND |Hodges-Lehmann delta| >= 5%. Otherwise: inconclusive.*"
                 .format(_PVALUE_THRESHOLD))
    lines.append("*P(better), P(same), P(worse) are region-of-practical-"
                 "equivalence probabilities derived from the bootstrap "
                 "distribution: fractions of resamples with Δ < -5%, "
                 "|Δ| ≤ 5%, and Δ > +5% respectively. Human-meaningful "
                 "direct probabilities — useful when the verdict is "
                 "\"inconclusive\" but you want to know which side the "
                 "evidence leans.*")
    if used_heuristic:
        lines.append("")
        lines.append("*Note: one or more rows used the heuristic fallback "
                     "because rounds[] was absent from the baseline "
                     "(saved by an older --save-compact). Re-baseline to "
                     "get bootstrap CI + Mann-Whitney p-values everywhere.*")

    # Per-percentile delta section: complements the median-based verdict
    # by showing whether the tail moved with or against the median.
    # A change that improves median by 5% but regresses p99 by 30% is
    # invisible to median-only tests; this table makes it obvious.
    _append_percentile_deltas_section(
        lines, sorted(common_ids, key=_sort_key),
        baseline_scenarios, current_scenarios)

    # Distribution-shape section: catches tail-growth regressions that
    # a median-based verdict misses (e.g. p99 doubles while median is
    # unchanged - the framework's median verdict says "neutral" but
    # tail-sensitive workloads experience a real regression).
    _append_distribution_shape_section(
        lines, sorted(common_ids, key=_sort_key),
        baseline_scenarios, current_scenarios)

    if missing_ids:
        lines.append("")
        lines.append("**Missing in current run** (present in baseline): {0}"
                     .format(", ".join(missing_ids)))
    if new_ids:
        lines.append("")
        lines.append("**New in current run** (not in baseline): {0}"
                     .format(", ".join(new_ids)))

    if regressed:
        lines.append("")
        lines.append("**{0} regression(s): {1}**".format(
            len(regressed), ", ".join(regressed)))
    if faster:
        lines.append("")
        lines.append("**{0} improvement(s): {1}**".format(
            len(faster), ", ".join(faster)))

    return ComparisonResult(
        markdown="\n".join(lines),
        faster_ids=faster,
        regressed_ids=regressed,
        inconclusive_ids=inconclusive,
        neutral_ids=neutral,
        missing_ids=missing_ids,
        new_ids=new_ids,
    )


def _append_percentile_deltas_section(lines, sorted_ids,
                                      baseline_scenarios, current_scenarios):
    """Append a per-percentile delta table to lines.

    Shows Δ median / Δ p95 / Δ p99 / Δ p99.9 side by side. A change
    that improves the median but regresses the tail is otherwise
    invisible to the median-based verdict above.
    """
    def pct_of(b, c):
        if b is None or c is None or b <= 0:
            return None
        return (c - b) / b

    rows = []
    for sid in sorted_ids:
        b_stats = baseline_scenarios[sid]["stats"]
        c_stats = current_scenarios[sid]["stats"]
        deltas = {
            "median": pct_of(b_stats.get("median"), c_stats.get("median")),
            "p95":    pct_of(b_stats.get("p95"),    c_stats.get("p95")),
            "p99":    pct_of(b_stats.get("p99"),    c_stats.get("p99")),
            "p99_9":  pct_of(b_stats.get("p99_9"),  c_stats.get("p99_9")),
        }
        rows.append((sid, current_scenarios[sid]["name"], deltas))

    if not rows:
        return
    lines.append("")
    lines.append("### Per-percentile deltas")
    lines.append("")
    lines.append("*Side-by-side relative changes at each percentile. "
                 "A scenario whose median improves but whose p99 "
                 "regresses signals a tail-latency regression — "
                 "easy to miss if only the median is compared.*")
    lines.append("")
    lines.append("| ID | Scenario | Δ median | Δ p95 | Δ p99 | Δ p99.9 |")
    lines.append("|----|----------|----------|-------|-------|---------|")
    for sid, name, d in rows:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            sid, name,
            _fmt_pct(d["median"]),
            _fmt_pct(d["p95"]),
            _fmt_pct(d["p99"]),
            _fmt_pct(d["p99_9"])))


def _append_distribution_shape_section(lines, sorted_ids,
                                       baseline_scenarios, current_scenarios):
    """Append a distribution-shape comparison table to lines.

    Surfaces tail-percentile changes (p99, p99.9, tail-ratio) and the KS
    two-sample p-value. Doesn't drive the headline verdict — that stays
    median-based — but flags scenarios where the median is stable yet
    the tail is shifting, which median-only tests miss.
    """
    rows = []
    for sid in sorted_ids:
        b_sc = baseline_scenarios[sid]
        c_sc = current_scenarios[sid]
        b_stats = b_sc["stats"]
        c_stats = c_sc["stats"]
        b_rounds = b_sc.get("rounds") or []
        c_rounds = c_sc.get("rounds") or []
        if not b_rounds or not c_rounds:
            # Heuristic-fallback path: no per-round data, no KS test.
            continue
        b_p99 = b_stats.get("p99", 0.0)
        c_p99 = c_stats.get("p99", 0.0)
        b_p99_9 = b_stats.get("p99_9", 0.0)
        c_p99_9 = c_stats.get("p99_9", 0.0)
        tail_ratio = (c_p99 / b_p99) if b_p99 > 0 else None
        _d, ks_p = _ks_two_sample(b_rounds, c_rounds)
        rows.append((sid, c_sc["name"],
                     b_p99, c_p99, b_p99_9, c_p99_9, tail_ratio, ks_p))

    if not rows:
        return
    lines.append("")
    lines.append("### Distribution-shape comparison")
    lines.append("")
    lines.append("*Tail-percentile changes and Kolmogorov-Smirnov test on the "
                 "full round distributions. The median verdict above is the "
                 "primary signal; this section catches regressions where the "
                 "median is stable but the tail grows (or vice versa).*")
    lines.append("")
    lines.append("| ID | Scenario | p99 base → curr | p99.9 base → curr | "
                 "Tail ratio (curr/base) | KS p |")
    lines.append("|----|----------|-----------------|-------------------|"
                 "------------------------|------|")
    for sid, name, b_p99, c_p99, b_p99_9, c_p99_9, tail_ratio, ks_p in rows:
        p99_cell = "{0} → {1}".format(
            _human_duration(b_p99), _human_duration(c_p99))
        p99_9_cell = "{0} → {1}".format(
            _human_duration(b_p99_9), _human_duration(c_p99_9))
        tail_cell = "{0:.2f}x".format(tail_ratio) if tail_ratio is not None else "n/a"
        ks_cell = _fmt_pvalue(ks_p)
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            sid, name, p99_cell, p99_9_cell, tail_cell, ks_cell))


def _sort_key(scenario_id):
    """Natural-sort key so 'M2a' < 'M10' and 'X2-1k' < 'X2-10k'."""
    # Simple heuristic: split digits from letters.
    import re
    parts = re.split(r"(\d+)", scenario_id)
    return tuple(int(p) if p.isdigit() else p for p in parts)
