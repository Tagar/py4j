"""Summary statistics helpers for the perf framework.

Pure functions on a list of per-round timings — no I/O, no JVM, no
network. Used by ``build_scenario_entry`` to populate the ``stats``
block on each scenario, and by the markdown rendering layer to
compute noise bands.

Kept separate from ``report.py`` so the comparison / rendering
machinery can grow without inflating the file that holds the
primitive aggregations.
"""

import statistics
from typing import Dict, List


def compute_stats(rounds: List[float]) -> Dict[str, float]:
    """Summary stats for a list of per-round timings."""
    if not rounds:
        return {
            "min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0,
            "stddev": 0.0, "iqr": 0.0,
            "p5": 0.0, "p95": 0.0, "p99": 0.0, "p99_9": 0.0,
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
        "p99_9": pct(99.9),
    }


def noise_fraction(stats: Dict[str, float]) -> float:
    """(p95 - p5) / median, clipped to 0 when median is 0.

    Robust dispersion estimate used by the heuristic-fallback verdict
    when per-round samples are unavailable.
    """
    median = stats.get("median", 0.0)
    if median <= 0:
        return 0.0
    return (stats["p95"] - stats["p5"]) / median
