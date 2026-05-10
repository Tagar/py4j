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
import math
import random
import statistics
from collections import namedtuple
from typing import Any, Dict, List, Optional, Tuple

from py4j.tests.perf import REPORT_SCHEMA_VERSION


ComparisonResult = namedtuple(
    "ComparisonResult",
    ["markdown", "faster_ids", "regressed_ids",
     "inconclusive_ids", "neutral_ids", "missing_ids", "new_ids"],
)

# Verdict thresholds
_REGRESSION_PCT = 0.05     # +5% worse
_IMPROVEMENT_PCT = -0.05   # -5% better
_NOISE_MULTIPLIER = 2.0    # |delta| must exceed 2 x noise (heuristic fallback)
_PVALUE_THRESHOLD = 0.01   # Mann-Whitney p < 0.01 = confident change

# Bootstrap parameters
_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_MAX_SAMPLES_PER_SIDE = 1000  # downsample if rounds[] is huge
_BOOTSTRAP_RANDOM_SEED = 42  # deterministic; comparisons of the same JSON
                              # always produce the same CI


def compute_stats(rounds: List[float]) -> Dict[str, float]:
    """Summary stats for a list of per-round timings."""
    if not rounds:
        return {
            "min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0,
            "stddev": 0.0, "iqr": 0.0,
            "p5": 0.0, "p95": 0.0, "p99": 0.0,
        }
    sorted_rounds = sorted(rounds)
    n = len(sorted_rounds)

    def pct(p):
        if n == 1:
            return sorted_rounds[0]
        rank = p / 100.0 * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        return sorted_rounds[lo] * (1 - frac) + sorted_rounds[hi] * frac

    mean = statistics.fmean(rounds)
    stddev = statistics.pstdev(rounds) if n > 1 else 0.0
    return {
        "min": sorted_rounds[0],
        "max": sorted_rounds[-1],
        "mean": mean,
        "median": statistics.median(sorted_rounds),
        "stddev": stddev,
        "iqr": pct(75) - pct(25),
        "p5": pct(5),
        "p95": pct(95),
        "p99": pct(99),
    }


def build_scenario_entry(
    scenario_id: str,
    name: str,
    runner: str,
    rounds: List[float],
    warmup_rounds: int,
    iterations_per_round: int = 1,
    budget_triggered: bool = False,
    unit: str = "seconds",
) -> Dict[str, Any]:
    """Construct one scenario record matching the JSON schema."""
    return {
        "id": scenario_id,
        "name": name,
        "runner": runner,
        "unit": unit,
        "warmup_rounds": warmup_rounds,
        "measured_rounds": len(rounds),
        "iterations_per_round": iterations_per_round,
        "budget_triggered": budget_triggered,
        "rounds": rounds,
        "stats": compute_stats(rounds),
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


# =============================================================== statistics


def _mannwhitney_u(a: List[float], b: List[float]
                   ) -> Tuple[Optional[float], Optional[float]]:
    """Two-sided Mann-Whitney U test with normal approximation.

    Returns (U, p_value). For our sample sizes (hundreds to thousands per
    side) the normal approximation is very accurate. We hand-roll the
    test rather than depend on scipy because it's ~30 lines and we don't
    want to add scipy as a perf-framework dependency.

    Returns (None, None) if either sample is empty.
    """
    if not a or not b:
        return None, None
    n1, n2 = len(a), len(b)

    # Combined ranking, with average rank for ties.
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda t: t[0])

    R1 = 0.0
    i = 0
    n = len(combined)
    while i < n:
        j = i
        while j < n and combined[j][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-indexed average
        for k in range(i, j):
            if combined[k][1] == 0:
                R1 += avg_rank
        i = j

    U1 = R1 - n1 * (n1 + 1) / 2.0
    U2 = n1 * n2 - U1
    U = min(U1, U2)

    mu = n1 * n2 / 2.0
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    if sigma == 0:
        return U, 1.0
    z = abs(U - mu) / sigma
    # Two-sided p-value via the standard-normal CDF.
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2))))
    return U, max(0.0, min(1.0, p))


def _bootstrap_median_delta_ci(
        a: List[float], b: List[float],
        n_resamples: int = _BOOTSTRAP_RESAMPLES,
        ci: float = 0.95,
        seed: int = _BOOTSTRAP_RANDOM_SEED,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Bootstrap CI on the relative median delta (median(b)/median(a) - 1).

    Returns (lower, point, upper) of the (1-alpha) CI on the relative
    delta, where alpha = 1 - ci. If both bounds have the same sign,
    the change is "confident" (CI excludes zero).

    Implementation note: uses ``random.Random(seed)`` for determinism
    so re-comparing the same JSON files produces the same CI.

    To keep this fast we cap the sample size per side at
    ``_BOOTSTRAP_MAX_SAMPLES_PER_SIDE``: bootstrapping with 1000 samples
    × 2000 resamples is ~2 seconds in pure Python; bootstrapping with
    100 000 samples × 2000 resamples is several minutes. The CI from
    a 1000-sample bootstrap is within a fraction of a percent of the
    full-sample CI, which is more than the precision a human reviewer
    cares about.
    """
    if not a or not b:
        return None, None, None

    rng = random.Random(seed)

    # Downsample if necessary so the bootstrap is fast.
    cap = _BOOTSTRAP_MAX_SAMPLES_PER_SIDE
    a_use = a if len(a) <= cap else rng.sample(a, cap)
    b_use = b if len(b) <= cap else rng.sample(b, cap)
    n_a, n_b = len(a_use), len(b_use)

    deltas = []
    for _ in range(n_resamples):
        # With-replacement resampling.
        sa = sorted(a_use[rng.randrange(n_a)] for _ in range(n_a))
        sb = sorted(b_use[rng.randrange(n_b)] for _ in range(n_b))
        ma = sa[n_a // 2] if n_a % 2 else (sa[n_a // 2 - 1] + sa[n_a // 2]) / 2.0
        mb = sb[n_b // 2] if n_b % 2 else (sb[n_b // 2 - 1] + sb[n_b // 2]) / 2.0
        if ma > 0:
            deltas.append((mb - ma) / ma)

    if not deltas:
        return None, None, None
    deltas.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = int(alpha * len(deltas))
    hi_idx = max(lo_idx, int((1.0 - alpha) * len(deltas)) - 1)
    point_idx = len(deltas) // 2
    return deltas[lo_idx], deltas[point_idx], deltas[hi_idx]


def _stats_verdict(b_stats: dict, c_stats: dict
                   ) -> Tuple[str, Optional[dict]]:
    """Heuristic fallback verdict (no rounds[] data)."""
    b_median = b_stats.get("median", 0.0)
    c_median = c_stats.get("median", 0.0)
    if b_median <= 0:
        return "neutral", None
    pct = (c_median - b_median) / b_median
    b_noise = _noise_fraction(b_stats)
    c_noise = _noise_fraction(c_stats)
    noise_band = max(b_noise, c_noise) * _NOISE_MULTIPLIER

    if abs(pct) <= noise_band:
        return "inconclusive", None
    if pct <= _IMPROVEMENT_PCT:
        return "faster", None
    if pct >= _REGRESSION_PCT:
        return "regression", None
    return "neutral", None


def _confident_verdict(b_rounds: List[float], c_rounds: List[float],
                       b_median: float, c_median: float
                       ) -> Tuple[str, dict]:
    """Verdict using bootstrap CI + Mann-Whitney U on per-round samples.

    Returns (verdict, stats_dict) where stats_dict carries the CI bounds
    and p-value so the report layer can show them.
    """
    lo, point, hi = _bootstrap_median_delta_ci(b_rounds, c_rounds)
    _u, p = _mannwhitney_u(b_rounds, c_rounds)
    pct = (c_median - b_median) / b_median if b_median > 0 else 0.0

    info = {
        "ci_lo": lo, "ci_hi": hi, "ci_point": point,
        "p_value": p,
        "method": "bootstrap+mannwhitney",
    }

    # Confident verdict needs three things to align:
    # (1) Mann-Whitney p-value below threshold (samples differ)
    # (2) bootstrap CI excludes zero (the median difference is real)
    # (3) point estimate is at least the IMPROVEMENT/REGRESSION_PCT
    #     (the difference is large enough to care about)
    if (p is not None and p < _PVALUE_THRESHOLD
            and lo is not None and hi is not None
            and ((lo > 0 and hi > 0) or (lo < 0 and hi < 0))):
        if pct <= _IMPROVEMENT_PCT:
            return "faster", info
        if pct >= _REGRESSION_PCT:
            return "regression", info
        # Statistically real but smaller than 5% - call it neutral.
        return "neutral", info
    return "inconclusive", info


def _verdict(baseline_scenario: dict, current_scenario: dict
             ) -> Tuple[str, Optional[dict]]:
    """Pick the strongest verdict the input data supports.

    If both scenarios have non-empty rounds[] (true for current runs and
    for compact baselines saved with the new sample-keeping
    compact_report), use bootstrap CI + Mann-Whitney U. Otherwise fall
    back to the median/noise heuristic.
    """
    b_stats = baseline_scenario["stats"]
    c_stats = current_scenario["stats"]
    b_rounds = baseline_scenario.get("rounds") or []
    c_rounds = current_scenario.get("rounds") or []

    if b_rounds and c_rounds:
        return _confident_verdict(
            b_rounds, c_rounds,
            b_stats["median"], c_stats["median"])
    return _stats_verdict(b_stats, c_stats)


def _human_duration(seconds: float) -> str:
    """Scale a duration in seconds to a human-readable string."""
    if seconds >= 1.0:
        return "{0:.3f} s".format(seconds)
    if seconds >= 1e-3:
        return "{0:.3f} ms".format(seconds * 1e3)
    if seconds >= 1e-6:
        return "{0:.3f} \u00b5s".format(seconds * 1e6)
    return "{0:.3f} ns".format(seconds * 1e9)


def _noise_fraction(stats: Dict[str, float]) -> float:
    """(p95 - p5) / median, clipped to 0 when median is 0."""
    median = stats.get("median", 0.0)
    if median <= 0:
        return 0.0
    return (stats["p95"] - stats["p5"]) / median


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

    lines.append("| ID | Scenario | Median | p95 | Stddev | Noise | Rounds |")
    lines.append("|----|----------|--------|-----|--------|-------|--------|")
    for s in scenarios:
        stats = s["stats"]
        noise = _noise_fraction(stats) * 100
        rounds_cell = "{0}{1}".format(
            s["measured_rounds"],
            " (budget)" if s.get("budget_triggered") else "")
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5:.1f}% | {6} |".format(
                s["id"],
                s["name"],
                _human_duration(stats["median"]),
                _human_duration(stats["p95"]),
                _human_duration(stats["stddev"]),
                noise,
                rounds_cell,
            ))
    lines.append("")
    lines.append(
        "*Noise = (p95 - p5) / median within a single run.*")
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

    current_warnings = current_report.get("warnings", [])
    if current_warnings:
        lines.append("**Current-run warnings:**")
        for w in current_warnings:
            lines.append("- {0}".format(w))
        lines.append("")

    lines.append("| ID | Scenario | Baseline median | Current median | "
                 "Delta median | 95% CI | M-W p | Verdict |")
    lines.append("|----|----------|-----------------|----------------|"
                 "--------------|--------|-------|---------|")

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

        if info is not None and info.get("method") == "bootstrap+mannwhitney":
            ci_cell = "[{0}, {1}]".format(
                _fmt_pct(info["ci_lo"]), _fmt_pct(info["ci_hi"]))
            p_cell = _fmt_pvalue(info.get("p_value"))
        else:
            used_heuristic = True
            noise_pct = max(_noise_fraction(b_stats),
                            _noise_fraction(c_stats)) * 100
            ci_cell = "noise {0:.1f}%".format(noise_pct)
            p_cell = "n/a"

        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |"
                     .format(
                         sid, c_sc["name"],
                         _human_duration(b_stats["median"]),
                         _human_duration(c_stats["median"]),
                         _fmt_pct(delta_median),
                         ci_cell, p_cell, _fmt_verdict(verdict)))

    lines.append("")
    lines.append("*Verdict: confident faster/regression requires "
                 "Mann-Whitney p < {0}, bootstrap 95% CI excluding zero, "
                 "AND |delta median| >= 5%. Otherwise: inconclusive.*"
                 .format(_PVALUE_THRESHOLD))
    if used_heuristic:
        lines.append("")
        lines.append("*Note: one or more rows used the heuristic fallback "
                     "because rounds[] was absent from the baseline "
                     "(saved by an older --save-compact). Re-baseline to "
                     "get bootstrap CI + Mann-Whitney p-values everywhere.*")

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


def _sort_key(scenario_id):
    """Natural-sort key so 'M2a' < 'M10' and 'X2-1k' < 'X2-10k'."""
    # Simple heuristic: split digits from letters.
    import re
    parts = re.split(r"(\d+)", scenario_id)
    return tuple(int(p) if p.isdigit() else p for p in parts)
