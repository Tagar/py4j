"""Statistical-inference primitives used by the comparison verdict.

Pure functions on per-round sample lists. No I/O. No JVM.

What lives here:
  - Mann-Whitney U two-sample test (rank-based; tests if medians differ)
  - Kolmogorov-Smirnov two-sample test (tests if distributions differ)
  - Hodges-Lehmann pseudo-median (robust location-shift point estimate)
  - Bootstrap distribution of the relative median delta + CI extraction
  - Region-of-practical-equivalence (ROPE) probabilities
  - SPRT sequential-stopping decision rule
  - Heuristic-fallback and confident verdict assemblers

Kept separate from ``report.py`` so the report layer can stay focused
on schema construction, JSON I/O, and orchestration; this module is
where you go to read or change the statistics.
"""

import math
import random
from typing import Dict, List, Optional, Tuple

from py4j.tests.perf.stats import noise_fraction


# Verdict thresholds
REGRESSION_PCT = 0.05     # +5% worse
IMPROVEMENT_PCT = -0.05   # -5% better
NOISE_MULTIPLIER = 2.0    # |delta| must exceed 2 x noise (heuristic fallback)
PVALUE_THRESHOLD = 0.01   # Mann-Whitney p < 0.01 = confident change

# Bootstrap parameters
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_MAX_SAMPLES_PER_SIDE = 1000  # downsample if rounds[] is huge
BOOTSTRAP_RANDOM_SEED = 42  # deterministic; comparisons of the same JSON
                             # always produce the same CI


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


def _ks_two_sample(a: List[float], b: List[float]
                   ) -> Tuple[Optional[float], Optional[float]]:
    """Two-sample Kolmogorov-Smirnov test.

    Returns (D, p_value) where D is the maximum absolute difference
    between the two empirical CDFs and p_value is the asymptotic
    two-sided probability of observing that D under H0 (samples drawn
    from the same distribution).

    Complements the Mann-Whitney + bootstrap-CI verdict: those test
    whether the *medians* differ, while KS detects any distribution-
    shape change — most importantly tail-growth regressions (p99/p99.9
    creeping up while the median holds). A perf change that doubles
    p99 latency but leaves the median unchanged will fly under the
    median-based verdict's radar but light up KS.

    Uses the asymptotic approximation:
        p ≈ 2 * exp(-2 * (n*m / (n+m)) * D^2)
    which is accurate for n, m ≥ 5 and conservative otherwise.
    """
    if not a or not b:
        return None, None
    sa = sorted(a)
    sb = sorted(b)
    n_a = len(sa)
    n_b = len(sb)

    # Walk both sorted samples computing empirical CDFs at each step;
    # D is the max |F_a - F_b| over the union of sample points.
    i = 0
    j = 0
    d_max = 0.0
    while i < n_a and j < n_b:
        if sa[i] <= sb[j]:
            i += 1
        else:
            j += 1
        d = abs(i / n_a - j / n_b)
        if d > d_max:
            d_max = d

    # Asymptotic two-sided p-value.
    en = (n_a * n_b) / (n_a + n_b)
    p = 2.0 * math.exp(-2.0 * en * d_max * d_max)
    return d_max, max(0.0, min(1.0, p))


def _hodges_lehmann_2sample(a: List[float], b: List[float]) -> Optional[float]:
    """Two-sample Hodges-Lehmann estimator of the location shift (b - a).

    Returns the median of all pairwise differences ``{b_j - a_i : i, j}``.
    This is the natural point estimate paired with the Mann-Whitney U test
    and is robust to single-outlier rounds (GC pauses, scheduler hiccups)
    in a way that ``median(b) - median(a)`` is not.

    Returns ``None`` if either sample is empty.

    To bound pure-Python cost, we cap each side at
    ``BOOTSTRAP_MAX_SAMPLES_PER_SIDE`` (default 1000). The resulting
    pairwise grid (~1M elements) sorts in tens of milliseconds; the HL
    estimate is within fractions of a percent of the full-sample value.
    """
    if not a or not b:
        return None
    cap = BOOTSTRAP_MAX_SAMPLES_PER_SIDE
    if len(a) > cap:
        rng = random.Random(BOOTSTRAP_RANDOM_SEED)
        a = rng.sample(a, cap)
    if len(b) > cap:
        rng = random.Random(BOOTSTRAP_RANDOM_SEED + 1)
        b = rng.sample(b, cap)
    diffs = [bv - av for bv in b for av in a]
    diffs.sort()
    n = len(diffs)
    if n % 2:
        return diffs[n // 2]
    return (diffs[n // 2 - 1] + diffs[n // 2]) / 2.0


def _compute_bootstrap_deltas(
        a: List[float], b: List[float],
        n_resamples: int = BOOTSTRAP_RESAMPLES,
        seed: int = BOOTSTRAP_RANDOM_SEED,
) -> List[float]:
    """Build the sorted bootstrap distribution of the relative median delta.

    Each resample draws ``len(a_use)`` samples (with replacement) from
    ``a`` and ``len(b_use)`` from ``b``, computes both medians, and
    records ``(median_b - median_a) / median_a``. Returns the sorted list.

    Both the CI band and the region-of-practical-equivalence probabilities
    are derived from this same distribution so they remain consistent.

    Uses ``random.Random(seed)`` for determinism so re-comparing the same
    JSON files produces the same numbers.

    To bound pure-Python cost we cap each side at
    ``BOOTSTRAP_MAX_SAMPLES_PER_SIDE`` before resampling: 1000 × 2000
    resamples is ~2 s; full samples × 2000 resamples can be minutes.
    The result is within a small fraction of a percent of the full
    bootstrap, much finer than human reviewers care about.
    """
    if not a or not b:
        return []
    rng = random.Random(seed)
    cap = BOOTSTRAP_MAX_SAMPLES_PER_SIDE
    a_use = a if len(a) <= cap else rng.sample(a, cap)
    b_use = b if len(b) <= cap else rng.sample(b, cap)
    n_a, n_b = len(a_use), len(b_use)
    deltas = []
    for _ in range(n_resamples):
        sa = sorted(a_use[rng.randrange(n_a)] for _ in range(n_a))
        sb = sorted(b_use[rng.randrange(n_b)] for _ in range(n_b))
        ma = sa[n_a // 2] if n_a % 2 else (sa[n_a // 2 - 1] + sa[n_a // 2]) / 2.0
        mb = sb[n_b // 2] if n_b % 2 else (sb[n_b // 2 - 1] + sb[n_b // 2]) / 2.0
        if ma > 0:
            deltas.append((mb - ma) / ma)
    deltas.sort()
    return deltas


def _ci_from_deltas(deltas: List[float], ci: float = 0.95
                    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Extract the (lower, point, upper) CI band from sorted bootstrap deltas."""
    if not deltas:
        return None, None, None
    alpha = (1.0 - ci) / 2.0
    lo_idx = int(alpha * len(deltas))
    hi_idx = max(lo_idx, int((1.0 - alpha) * len(deltas)) - 1)
    point_idx = len(deltas) // 2
    return deltas[lo_idx], deltas[point_idx], deltas[hi_idx]


def _rope_probabilities(deltas: List[float], rope_pct: float = 0.05
                        ) -> Dict[str, float]:
    """Region-of-practical-equivalence probabilities from bootstrap deltas.

    Returns the empirical fractions:
      - ``p_better``: P(Δ < -rope_pct), i.e. "meaningfully faster"
      - ``p_same`` : P(|Δ| ≤ rope_pct), i.e. "within practical equivalence"
      - ``p_worse``: P(Δ > +rope_pct), i.e. "meaningfully slower"

    These add up to 1 (modulo floating-point) and are human-meaningful
    direct probabilities — useful complement to the Mann-Whitney p-value
    (which only tells you *whether* the distributions differ, not the
    *direction or magnitude* of the practical difference).

    Default ``rope_pct=0.05`` matches the IMPROVEMENT/REGRESSION threshold
    used elsewhere in the verdict logic.
    """
    if not deltas:
        return {"p_better": None, "p_same": None, "p_worse": None}
    n = len(deltas)
    n_better = sum(1 for d in deltas if d < -rope_pct)
    n_worse = sum(1 for d in deltas if d > rope_pct)
    n_same = n - n_better - n_worse
    return {
        "p_better": n_better / n,
        "p_same": n_same / n,
        "p_worse": n_worse / n,
    }


def sprt_decide(baseline_rounds: List[float], current_rounds: List[float],
                mde: float = 0.05, n_resamples: int = 500
                ) -> str:
    """Sequential-stopping decision: faster / regression / neutral / undecided.

    Uses the bootstrap CI on the relative median delta (current vs
    baseline) combined with a region-of-practical-equivalence band
    of ±``mde`` (default ±5 %) to decide at each step whether more
    data is needed.

    Decisions:
      - ``"faster"``   : 95 % CI is entirely below ``-mde``
                         (current median is meaningfully faster)
      - ``"regression"``: 95 % CI is entirely above ``+mde``
      - ``"neutral"``   : 95 % CI is entirely inside ``[-mde, +mde]``
                         (practical equivalence established)
      - ``"undecided"`` : CI straddles a boundary; need more rounds

    This is the HDI+ROPE Bayesian decision rule applied as a sequential
    test: it stops early on clear effects AND clear equivalences,
    only continuing when the data leaves the answer ambiguous. Uses
    ``n_resamples=500`` (vs. 2000 for the final verdict) because the
    SPRT runs after every batch and we want it cheap.
    """
    if not baseline_rounds or not current_rounds:
        return "undecided"
    deltas = _compute_bootstrap_deltas(
        baseline_rounds, current_rounds, n_resamples=n_resamples)
    lo, _point, hi = _ci_from_deltas(deltas)
    if lo is None or hi is None:
        return "undecided"
    if hi < -mde:
        return "faster"
    if lo > mde:
        return "regression"
    if lo > -mde and hi < mde:
        return "neutral"
    return "undecided"


def _bootstrap_median_delta_ci(
        a: List[float], b: List[float],
        n_resamples: int = BOOTSTRAP_RESAMPLES,
        ci: float = 0.95,
        seed: int = BOOTSTRAP_RANDOM_SEED,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Bootstrap CI on the relative median delta (median(b)/median(a) - 1).

    Returns (lower, point, upper). Thin wrapper over
    ``_compute_bootstrap_deltas`` and ``_ci_from_deltas`` kept for backward
    compatibility with existing call sites and tests.
    """
    deltas = _compute_bootstrap_deltas(a, b, n_resamples=n_resamples, seed=seed)
    return _ci_from_deltas(deltas, ci=ci)


def _stats_verdict(b_stats: dict, c_stats: dict
                   ) -> Tuple[str, Optional[dict]]:
    """Heuristic fallback verdict (no rounds[] data)."""
    b_median = b_stats.get("median", 0.0)
    c_median = c_stats.get("median", 0.0)
    if b_median <= 0:
        return "neutral", None
    pct = (c_median - b_median) / b_median
    b_noise = noise_fraction(b_stats)
    c_noise = noise_fraction(c_stats)
    noise_band = max(b_noise, c_noise) * NOISE_MULTIPLIER

    if abs(pct) <= noise_band:
        return "inconclusive", None
    if pct <= IMPROVEMENT_PCT:
        return "faster", None
    if pct >= REGRESSION_PCT:
        return "regression", None
    return "neutral", None


def _confident_verdict(b_rounds: List[float], c_rounds: List[float],
                       b_median: float, c_median: float
                       ) -> Tuple[str, dict]:
    """Verdict using bootstrap CI + Mann-Whitney U on per-round samples.

    Returns (verdict, stats_dict) where stats_dict carries the CI bounds
    and p-value so the report layer can show them.
    """
    # Compute bootstrap deltas once and derive both the CI band and the
    # region-of-practical-equivalence probabilities from it. Keeps the
    # two consistent (same resamples, same rng seed).
    deltas = _compute_bootstrap_deltas(b_rounds, c_rounds)
    lo, point, hi = _ci_from_deltas(deltas)
    rope = _rope_probabilities(deltas, rope_pct=REGRESSION_PCT)
    _u, p = _mannwhitney_u(b_rounds, c_rounds)

    # Hodges-Lehmann (median of pairwise differences) as the point
    # estimate of the location shift. Robust to single-outlier rounds
    # in a way that median(c) - median(b) is not. The relative HL delta
    # is what drives the verdict gates below.
    hl_shift = _hodges_lehmann_2sample(b_rounds, c_rounds)
    if hl_shift is not None and b_median > 0:
        pct = hl_shift / b_median
        pct_method = "hodges-lehmann"
    else:
        pct = (c_median - b_median) / b_median if b_median > 0 else 0.0
        pct_method = "median-diff"

    info = {
        "ci_lo": lo, "ci_hi": hi, "ci_point": point,
        "p_value": p,
        "p_better": rope["p_better"],
        "p_same": rope["p_same"],
        "p_worse": rope["p_worse"],
        "hl_delta_pct": pct if pct_method == "hodges-lehmann" else None,
        "method": "bootstrap+mannwhitney+hodges-lehmann+rope",
        "point_estimate_method": pct_method,
    }

    # Confident verdict needs three things to align:
    # (1) Mann-Whitney p-value below threshold (samples differ)
    # (2) bootstrap CI excludes zero (the median difference is real)
    # (3) point estimate is at least the IMPROVEMENT/REGRESSION_PCT
    #     (the difference is large enough to care about)
    if (p is not None and p < PVALUE_THRESHOLD
            and lo is not None and hi is not None
            and ((lo > 0 and hi > 0) or (lo < 0 and hi < 0))):
        if pct <= IMPROVEMENT_PCT:
            return "faster", info
        if pct >= REGRESSION_PCT:
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
