"""Self-tests for the perf framework.

Tests are fully in-process - no JVM spawn, no network. They cover the
statistical helpers, comparison verdicts, filtering logic, and report
I/O round-trips. End-to-end scenarios (which do need a JVM) are
validated by running `python -m py4j.tests.perf smoke` manually.
"""

import json
import os
import platform
import random
import tempfile
import time

import pytest

from py4j.tests.perf.report import (
    build_report,
    build_scenario_entry,
    compare,
    compute_stats,
    read_json,
    write_json,
    write_markdown,
)
from py4j.tests.perf.scenarios import (
    ALL_SCENARIOS,
    filter_scenarios,
)


# --------------------------------------------------------------- compute_stats


def test_compute_stats_empty_returns_zeros():
    stats = compute_stats([])
    for key in ("min", "max", "mean", "median", "stddev",
                "iqr", "p5", "p95", "p99"):
        assert stats[key] == 0.0, key


def test_compute_stats_single_value():
    stats = compute_stats([0.5])
    assert stats["min"] == 0.5
    assert stats["max"] == 0.5
    assert stats["median"] == 0.5
    assert stats["stddev"] == 0.0
    # Percentiles degenerate to the single value.
    assert stats["p5"] == 0.5
    assert stats["p95"] == 0.5


def test_compute_stats_known_distribution():
    data = list(range(1, 101))  # 1..100
    stats = compute_stats(data)
    assert stats["min"] == 1
    assert stats["max"] == 100
    assert stats["median"] == pytest.approx(50.5, rel=1e-6)
    # Linear interpolation: p5 ~ 5.95, p95 ~ 95.05
    assert stats["p5"] == pytest.approx(5.95, rel=1e-2)
    assert stats["p95"] == pytest.approx(95.05, rel=1e-2)


# --------------------------------------------------------------- round-trip


def test_json_roundtrip():
    scen = build_scenario_entry(
        scenario_id="M1", name="static_call", runner="pytest-benchmark",
        rounds=[1.0, 1.1, 0.9],
        warmup_rounds=5,
        iterations_per_round=10,
    )
    report = build_report(
        environment={"os": "Darwin", "py4j_version": "0.10.9.9"},
        warnings=["battery"],
        scenarios=[scen],
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.json")
        write_json(report, path)
        loaded = read_json(path)
        assert loaded["version"] == report["version"]
        assert loaded["scenarios"][0]["id"] == "M1"
        assert loaded["scenarios"][0]["rounds"] == [1.0, 1.1, 0.9]


def test_markdown_renders_without_error():
    scen = build_scenario_entry(
        scenario_id="M1", name="static_call", runner="pytest-benchmark",
        rounds=[100e-6, 110e-6, 95e-6],
        warmup_rounds=5, iterations_per_round=1,
    )
    report = build_report({"os": "Darwin"}, [], [scen])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.md")
        write_markdown(report, path)
        with open(path) as fh:
            content = fh.read()
    assert "# py4j perf report" in content
    assert "M1" in content


# ----------------------------------------------------------------- filtering


def test_filter_only():
    result = filter_scenarios(ALL_SCENARIOS, only=["M1", "X5"])
    ids = [s.id for s in result]
    assert ids == ["M1", "X5"]


def test_filter_skip():
    result = filter_scenarios(ALL_SCENARIOS, skip=["M1", "X5"])
    ids = [s.id for s in result]
    assert "M1" not in ids
    assert "X5" not in ids
    assert len(result) == len(ALL_SCENARIOS) - 2


def test_filter_only_and_skip():
    result = filter_scenarios(ALL_SCENARIOS,
                              only=["M1", "M2a", "M2b"],
                              skip=["M2a"])
    assert [s.id for s in result] == ["M1", "M2b"]


def test_filter_unknown_id_returns_empty():
    result = filter_scenarios(ALL_SCENARIOS, only=["ZZ9-plural-Z-alpha"])
    assert result == []


# ------------------------------------------------------------------- compare


def _make_stats(median, p95, p5):
    return {
        "min": median * 0.9, "max": p95 * 1.1,
        "mean": median, "median": median,
        "stddev": (p95 - p5) / 4.0, "iqr": (p95 - p5) / 2.0,
        "p5": p5, "p95": p95, "p99": p95,
    }


def _make_scenario(sid, median, p95, p5, rounds=None):
    """Create a synthetic scenario.

    By default ``rounds`` is empty so tests exercise the heuristic
    fallback path (no per-round samples to bootstrap from). Tests that
    want to exercise the bootstrap+Mann-Whitney path pass an explicit
    ``rounds`` array.
    """
    return {
        "id": sid, "name": sid.lower(), "runner": "pytest-benchmark",
        "unit": "seconds", "warmup_rounds": 5, "measured_rounds": 30,
        "iterations_per_round": 1, "budget_triggered": False,
        "rounds": list(rounds) if rounds is not None else [],
        "stats": _make_stats(median, p95, p5),
    }


def _gauss_rounds(mean, sd, n=500, seed=0):
    """Synthetic normal-ish round timings for stats-path tests."""
    import random
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


_ENV_DARWIN = {"os": "Darwin", "cpu": "M4", "python": "3.14",
               "java": "21", "py4j_version": "0.10.9.9",
               "git_rev": "abc", "git_branch": "main",
               "timestamp_utc": "2026-01-01T00:00:00+00:00"}


def test_compare_identifies_regression_outside_noise():
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("X2", 1.0, 1.05, 0.95)])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("X2", 1.5, 1.55, 1.45)])  # +50%
    r = compare(base, curr)
    assert r.regressed_ids == ["X2"]
    assert r.faster_ids == []
    assert "**regression**" in r.markdown


def test_compare_identifies_improvement_outside_noise():
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 100e-6, 105e-6, 95e-6)])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 60e-6, 65e-6, 55e-6)])  # -40%
    r = compare(base, curr)
    assert r.faster_ids == ["M1"]
    assert r.regressed_ids == []
    assert "**faster**" in r.markdown


def test_compare_classifies_within_noise_as_inconclusive_heuristic():
    """Heuristic fallback path: rounds[] absent, decision is from
    stats + noise band."""
    # Median moves 10% but noise is 100% -> should be inconclusive.
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.0, 1.5, 0.5)])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.1, 1.6, 0.6)])
    r = compare(base, curr)
    assert r.inconclusive_ids == ["M5"]
    # With no rounds[], we should have used the heuristic path:
    assert "noise" in r.markdown.lower()


def test_compare_bootstrap_path_detects_regression():
    """Bootstrap+M-W path: rounds present, large stable shift -> regression."""
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("X2", 1.0, 1.05, 0.95,
                                        rounds=_gauss_rounds(1.0, 0.05,
                                                              seed=1))])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("X2", 1.5, 1.55, 1.45,
                                        rounds=_gauss_rounds(1.5, 0.05,
                                                              seed=2))])
    r = compare(base, curr)
    assert r.regressed_ids == ["X2"]
    # Bootstrap path produces M-W p-value column header in markdown:
    assert "M-W p" in r.markdown


def test_compare_bootstrap_path_detects_improvement():
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 100e-6, 105e-6, 95e-6,
                                        rounds=_gauss_rounds(100e-6, 5e-6,
                                                              seed=1))])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 60e-6, 65e-6, 55e-6,
                                        rounds=_gauss_rounds(60e-6, 5e-6,
                                                              seed=2))])
    r = compare(base, curr)
    assert r.faster_ids == ["M1"]


def test_compare_bootstrap_path_inconclusive_on_noise():
    """High variance + small shift -> CI straddles zero -> inconclusive."""
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.0, 2.0, 0.0,
                                        rounds=_gauss_rounds(1.0, 0.5,
                                                              seed=1))])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.05, 2.05, 0.05,
                                        rounds=_gauss_rounds(1.05, 0.5,
                                                              seed=2))])
    r = compare(base, curr)
    assert r.inconclusive_ids == ["M5"]


def test_compare_detects_env_mismatch():
    curr_env = dict(_ENV_DARWIN, os="Linux", java="8")
    base = build_report(_ENV_DARWIN, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    curr = build_report(curr_env, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    r = compare(base, curr)
    assert "environment mismatch" in r.markdown.lower()
    # Both differing keys should appear in the warning block.
    assert "Linux" in r.markdown
    assert "Darwin" in r.markdown


def test_compare_warns_when_same_git_rev_on_both_sides(capsys):
    # If you forget to cherry-pick the change before `--compare`, baseline
    # and current end up captured from the same commit and the deltas are
    # pure run-to-run noise. Guard surfaces this in the markdown and stderr.
    base = build_report(_ENV_DARWIN, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    curr = build_report(_ENV_DARWIN, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    r = compare(base, curr)
    assert "same git rev on both sides" in r.markdown.lower()
    assert "abc" in r.markdown  # the shared rev appears in the warning
    captured = capsys.readouterr()
    assert "identical git rev" in captured.err.lower()


def test_compare_no_same_rev_warning_when_revs_differ():
    base_env = dict(_ENV_DARWIN, git_rev="abc")
    curr_env = dict(_ENV_DARWIN, git_rev="def")
    base = build_report(base_env, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    curr = build_report(curr_env, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    r = compare(base, curr)
    assert "same git rev on both sides" not in r.markdown.lower()


def test_compare_no_same_rev_warning_when_rev_is_placeholder():
    # A "?" rev means the framework couldn't read git metadata; treating that
    # as a same-rev match would fire the warning on every metadata-less run.
    base_env = dict(_ENV_DARWIN, git_rev="?")
    curr_env = dict(_ENV_DARWIN, git_rev="?")
    base = build_report(base_env, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    curr = build_report(curr_env, [], [_make_scenario("M1", 1.0, 1.0, 1.0)])
    r = compare(base, curr)
    assert "same git rev on both sides" not in r.markdown.lower()


def test_compare_reports_missing_and_new():
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 1.0, 1.0, 1.0),
                         _make_scenario("OLD", 1.0, 1.0, 1.0)])
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M1", 1.0, 1.0, 1.0),
                         _make_scenario("NEW", 1.0, 1.0, 1.0)])
    r = compare(base, curr)
    assert r.missing_ids == ["OLD"]
    assert r.new_ids == ["NEW"]


# ----------------------------------------------------------------- env guards


def test_environment_metadata_has_required_keys():
    from py4j.tests.perf.environment import capture_metadata
    meta = capture_metadata()
    for key in ("os", "cpu", "ram_bytes", "python",
                "py4j_version", "git_rev", "git_branch",
                "timestamp_utc"):
        assert key in meta, key


def test_environment_guards_returns_list():
    from py4j.tests.perf.environment import check_guards
    warnings = check_guards()
    # May be empty or non-empty depending on machine; just type-check.
    assert isinstance(warnings, list)
    for w in warnings:
        assert isinstance(w, str)


# -------------------------------------------------------------- renice helper


def test_current_nice_returns_int_or_none():
    from py4j.tests.perf.environment import current_nice
    nv = current_nice()
    assert nv is None or isinstance(nv, int)


def test_try_renice_returns_expected_shape(monkeypatch):
    """try_renice always returns the same dict shape regardless of outcome.

    Force the sudo invocation to fail non-interactively so no real
    sudo prompt surfaces in CI.
    """
    from py4j.tests.perf import environment

    class _FakeCompleted:
        returncode = 1

    monkeypatch.setattr(environment.subprocess, "run",
                        lambda *a, **kw: _FakeCompleted())
    result = environment.try_renice(target_nice=-15, verbose=False)
    assert set(result.keys()) == {
        "attempted", "succeeded", "before", "after", "target", "reason"}
    assert result["target"] == -15
    assert isinstance(result["attempted"], bool)
    assert isinstance(result["succeeded"], bool)


# ------------------------------------------------------------- statistics


def test_mannwhitney_distinct_distributions():
    """Clearly different samples should yield very small p-value."""
    from py4j.tests.perf.report import _mannwhitney_u
    a = _gauss_rounds(100, 5, seed=1)
    b = _gauss_rounds(80, 5, seed=2)
    _u, p = _mannwhitney_u(a, b)
    assert p < 1e-10


def test_mannwhitney_identical_distributions():
    """Identical-mean samples should yield p well above 0.05."""
    from py4j.tests.perf.report import _mannwhitney_u
    a = _gauss_rounds(100, 5, seed=1)
    b = _gauss_rounds(100, 5, seed=2)
    _u, p = _mannwhitney_u(a, b)
    assert p > 0.05


def test_mannwhitney_handles_empty():
    from py4j.tests.perf.report import _mannwhitney_u
    assert _mannwhitney_u([], [1, 2, 3]) == (None, None)
    assert _mannwhitney_u([1, 2, 3], []) == (None, None)


def test_bootstrap_ci_excludes_zero_for_real_change():
    """A 20% shift with low noise should produce CI fully on one side."""
    from py4j.tests.perf.report import _bootstrap_median_delta_ci
    a = _gauss_rounds(100, 2, seed=1)
    b = _gauss_rounds(80, 2, seed=2)
    lo, point, hi = _bootstrap_median_delta_ci(a, b)
    assert lo < 0 and hi < 0  # both bounds negative -> confident speedup
    assert -0.30 < point < -0.10  # point estimate roughly -20%


def test_bootstrap_ci_straddles_zero_for_noise():
    """Same distribution -> CI should include zero."""
    from py4j.tests.perf.report import _bootstrap_median_delta_ci
    a = _gauss_rounds(100, 5, seed=1)
    b = _gauss_rounds(100, 5, seed=2)
    lo, _point, hi = _bootstrap_median_delta_ci(a, b)
    assert lo < 0 < hi  # straddles zero


def test_compact_report_keeps_round_sample():
    """compact_report must downsample rounds, not strip them."""
    from py4j.tests.perf.report import compact_report
    rounds = list(range(5000))
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1)
    report = build_report({}, [], [scen])
    compact = compact_report(report, sample_size=1000)
    s = compact["scenarios"][0]
    assert compact.get("compact") is True
    assert s["rounds_count"] == 5000
    assert len(s["rounds"]) == 1000
    assert all(r in rounds for r in s["rounds"])


def test_compact_report_keeps_all_when_under_sample_size():
    from py4j.tests.perf.report import compact_report
    rounds = [0.1, 0.2, 0.3]
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1)
    report = build_report({}, [], [scen])
    compact = compact_report(report, sample_size=1000)
    s = compact["scenarios"][0]
    assert s["rounds_count"] == 3
    assert s["rounds"] == rounds


def test_compact_report_is_deterministic():
    """compact_report(same input) must produce the same downsample twice
    so a baseline file is byte-stable across re-runs."""
    from py4j.tests.perf.report import compact_report
    rounds = list(range(5000))
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1)
    report = build_report({}, [], [scen])
    a = compact_report(report, sample_size=1000)
    b = compact_report(report, sample_size=1000)
    assert a["scenarios"][0]["rounds"] == b["scenarios"][0]["rounds"]


def test_mannwhitney_handles_ties_correctly():
    """The hand-rolled rank-averaging path is the easiest place for a
    bug. With many tied values, identical samples must give p ~ 1
    (not NaN, not 0), and a real shift across ties must still be
    detected.
    """
    from py4j.tests.perf.report import _mannwhitney_u

    # Identical samples saturated with ties: p should be very high.
    a = [1.0] * 50 + [2.0] * 50
    b = [1.0] * 50 + [2.0] * 50
    _u, p = _mannwhitney_u(a, b)
    assert p > 0.95

    # Same value space but b skews higher: p must drop sharply.
    a = [1.0] * 80 + [2.0] * 20
    b = [1.0] * 20 + [2.0] * 80
    _u, p = _mannwhitney_u(a, b)
    assert p < 0.001


def test_bootstrap_ci_is_deterministic_with_default_seed():
    """Same inputs + same default seed -> identical CI on every call.
    Critical for reproducibility of --compare across re-runs."""
    from py4j.tests.perf.report import _bootstrap_median_delta_ci
    a = _gauss_rounds(100, 5, n=200, seed=11)
    b = _gauss_rounds(80, 5, n=200, seed=22)
    ci_one = _bootstrap_median_delta_ci(a, b)
    ci_two = _bootstrap_median_delta_ci(a, b)
    assert ci_one == ci_two


def test_bootstrap_ci_handles_constant_arrays():
    """All values identical on each side: median delta has no
    uncertainty. CI should collapse to a point at the deterministic
    delta, not NaN or crash."""
    from py4j.tests.perf.report import _bootstrap_median_delta_ci
    a = [1.0] * 200
    b = [1.5] * 200
    lo, point, hi = _bootstrap_median_delta_ci(a, b)
    # Every bootstrap resample produces median(a)=1.0 and median(b)=1.5,
    # so delta = 0.5 (= +50%) on every sample.
    assert lo == hi == point
    assert abs(point - 0.5) < 1e-9


def test_compare_neutral_verdict_for_small_real_change():
    """Statistically significant but smaller than the 5% material-
    change threshold -> verdict is 'neutral', not 'faster'."""
    base = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.000, 1.005, 0.995,
                                        rounds=_gauss_rounds(1.000, 0.005,
                                                              n=400, seed=1))])
    # 2% slower with very tight noise: M-W will say "different",
    # CI will exclude zero, but |delta| < 5% -> neutral.
    curr = build_report(_ENV_DARWIN, [],
                        [_make_scenario("M5", 1.020, 1.025, 1.015,
                                        rounds=_gauss_rounds(1.020, 0.005,
                                                              n=400, seed=2))])
    r = compare(base, curr)
    assert "M5" not in r.regressed_ids
    assert "M5" not in r.faster_ids
    assert "M5" not in r.inconclusive_ids
    assert "M5" in r.neutral_ids


def test_compare_routes_per_scenario_between_paths():
    """One scenario has rounds[]; the other does not. compare() must
    use bootstrap+M-W on the first and the heuristic on the second
    in the same diff, and the markdown should flag the heuristic
    fallback."""
    base = build_report(_ENV_DARWIN, [], [
        _make_scenario("WITH", 1.0, 1.05, 0.95,
                       rounds=_gauss_rounds(1.0, 0.05, seed=1)),
        _make_scenario("WITHOUT", 1.0, 1.5, 0.5),  # rounds=[]
    ])
    curr = build_report(_ENV_DARWIN, [], [
        _make_scenario("WITH", 1.5, 1.55, 1.45,
                       rounds=_gauss_rounds(1.5, 0.05, seed=2)),
        _make_scenario("WITHOUT", 1.1, 1.6, 0.6),  # rounds=[]
    ])
    r = compare(base, curr)
    # 'WITH' uses bootstrap and detects the 50% regression:
    assert "WITH" in r.regressed_ids
    # 'WITHOUT' falls back to heuristic; with 100% noise the 10% delta
    # is inconclusive:
    assert "WITHOUT" in r.inconclusive_ids
    # Markdown should warn that one row used the heuristic fallback:
    assert "heuristic fallback" in r.markdown.lower()


@pytest.mark.skipif(platform.system() == "Windows",
                    reason="os.nice is POSIX-only; try_renice returns "
                           "unsupported-os on Windows without reaching "
                           "the already-at-target branch")
def test_try_renice_skips_if_already_at_target(monkeypatch):
    """If the process is already at <= target nice, renice is skipped."""
    from py4j.tests.perf import environment

    # Pretend we're already running at nice = -15.
    monkeypatch.setattr(environment.os, "nice", lambda inc: -15)
    result = environment.try_renice(target_nice=-15, verbose=False)
    assert result["attempted"] is False
    assert result["succeeded"] is True
    assert result["before"] == -15
    assert result["after"] == -15
    assert "already" in result["reason"]


# -------- v2 additions: Hodges-Lehmann, ROPE, KS, p99.9 --------


def test_hodges_lehmann_recovers_known_shift():
    """HL of (b - a) should match the known shift on clean data."""
    from py4j.tests.perf.report import _hodges_lehmann_2sample
    a = _gauss_rounds(100, 2, seed=1, n=200)
    b = _gauss_rounds(115, 2, seed=2, n=200)
    shift = _hodges_lehmann_2sample(a, b)
    # True shift is +15; HL should land close to it.
    assert 10 < shift < 20


def test_hodges_lehmann_robust_to_outliers():
    """A single huge outlier in 'b' must not move HL more than ~one
    rank position. Plain median(b) - median(a) survives this fine; the
    point of this test is to confirm HL is at least as robust."""
    from py4j.tests.perf.report import _hodges_lehmann_2sample
    a = _gauss_rounds(100, 2, seed=1, n=100)
    b = _gauss_rounds(110, 2, seed=2, n=100)
    clean_hl = _hodges_lehmann_2sample(a, b)
    b[0] = 100000.0  # one huge GC-pause-style outlier
    dirty_hl = _hodges_lehmann_2sample(a, b)
    # The outlier should shift HL by less than 1% of the clean estimate.
    assert abs(dirty_hl - clean_hl) / abs(clean_hl) < 0.01


def test_hodges_lehmann_handles_empty():
    from py4j.tests.perf.report import _hodges_lehmann_2sample
    assert _hodges_lehmann_2sample([], [1.0]) is None
    assert _hodges_lehmann_2sample([1.0], []) is None


def test_rope_probabilities_sum_to_one():
    """The three ROPE buckets must partition the bootstrap distribution."""
    from py4j.tests.perf.report import (
        _compute_bootstrap_deltas, _rope_probabilities)
    a = _gauss_rounds(100, 5, n=100, seed=1)
    b = _gauss_rounds(85, 5, n=100, seed=2)
    deltas = _compute_bootstrap_deltas(a, b)
    rope = _rope_probabilities(deltas, rope_pct=0.05)
    total = rope["p_better"] + rope["p_same"] + rope["p_worse"]
    assert abs(total - 1.0) < 1e-9


def test_rope_probabilities_skew_to_better_for_clear_speedup():
    """A clean ~15% speedup must produce P(better) ~ 1, not 0.5."""
    from py4j.tests.perf.report import (
        _compute_bootstrap_deltas, _rope_probabilities)
    a = _gauss_rounds(100, 2, n=200, seed=1)
    b = _gauss_rounds(85, 2, n=200, seed=2)
    deltas = _compute_bootstrap_deltas(a, b)
    rope = _rope_probabilities(deltas, rope_pct=0.05)
    assert rope["p_better"] > 0.95
    assert rope["p_worse"] < 0.05


def test_ks_detects_tail_growth_when_median_unchanged():
    """KS must catch tail regressions that median-based tests miss.

    Construct 'b' by moving the upper 20% of samples 5x higher. The
    median stays unchanged (still in the lower 50%); the tail region
    shifts dramatically. KS should reject H0.
    """
    from py4j.tests.perf.report import _ks_two_sample
    a = _gauss_rounds(100, 3, n=400, seed=1)
    b = list(_gauss_rounds(100, 3, n=400, seed=2))
    # Replace the top 20% of b with 5x-larger values — moves the CDF
    # by ~0.20 in the upper region, well above KS detection floor.
    b.sort()
    n_tail = max(1, len(b) // 5)
    for i in range(len(b) - n_tail, len(b)):
        b[i] *= 5.0
    _d, p = _ks_two_sample(a, b)
    assert p < 0.05


def test_ks_returns_high_p_for_identical_distributions():
    from py4j.tests.perf.report import _ks_two_sample
    a = _gauss_rounds(100, 5, n=200, seed=1)
    b = _gauss_rounds(100, 5, n=200, seed=2)
    _d, p = _ks_two_sample(a, b)
    assert p > 0.05  # cannot reject H0


def test_ks_handles_empty():
    from py4j.tests.perf.report import _ks_two_sample
    d, p = _ks_two_sample([], [1.0])
    assert d is None and p is None


def test_compute_stats_includes_p99_9():
    """compute_stats must expose p99.9 for the distribution-shape table."""
    rounds = [float(i) for i in range(1, 1001)]
    s = compute_stats(rounds)
    assert "p99_9" in s
    # p99.9 of 1..1000 lands near 999.x.
    assert 998 <= s["p99_9"] <= 1001


def test_compare_report_includes_distribution_shape_section():
    """The new 'Distribution-shape comparison' table must appear when
    both sides carry rounds[]."""
    a = _gauss_rounds(100, 3, n=80, seed=1)
    b = _gauss_rounds(95, 3, n=80, seed=2)
    base_scen = build_scenario_entry(
        scenario_id="X1", name="x1", runner="macro",
        rounds=a, warmup_rounds=3, iterations_per_round=1)
    curr_scen = build_scenario_entry(
        scenario_id="X1", name="x1", runner="macro",
        rounds=b, warmup_rounds=3, iterations_per_round=1)
    base = build_report({}, [], [base_scen])
    curr = build_report({}, [], [curr_scen])
    r = compare(base, curr)
    assert "Distribution-shape comparison" in r.markdown
    assert "Tail ratio" in r.markdown
    assert "KS p" in r.markdown


def test_pytest_micro_translate_produces_metrics_block():
    """Regression: the micro-runner JSON translator must route through
    build_scenario_entry so micro scenarios carry the same metrics
    block (latency_per_op_s, throughput_ops_per_s, errors, ...) as
    macro scenarios. Before this fix the translator constructed the
    dict manually and silently dropped the metrics fields."""
    import tempfile
    from py4j.tests.perf._pytest_micro import _translate

    bench_json = {
        "benchmarks": [
            {
                "name": "test_m1_static_call_no_args",
                "stats": {
                    "data": [1e-5, 1.1e-5, 0.9e-5, 1.05e-5, 0.95e-5],
                    "iterations": 1000,
                },
            },
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(bench_json, fh)
        path = fh.name
    try:
        scenarios = _translate(path)
    finally:
        os.unlink(path)
    assert len(scenarios) == 1
    s = scenarios[0]
    assert "metrics" in s, "micro translator must populate metrics block"
    assert s["metrics"]["latency_per_op_s"] is not None
    assert s["metrics"]["throughput_ops_per_s"] is not None
    # cpu_time_ratio stays None — pytest-benchmark doesn't expose CPU time.
    assert s["metrics"]["cpu_time_ratio"] is None
    assert s["metrics"]["errors"] == 0


def test_fmt_throughput_chooses_correct_units():
    """Throughput formatter must pick k/M/G based on magnitude."""
    from py4j.tests.perf.report import _fmt_throughput
    assert "n/a" == _fmt_throughput(None)
    assert "n/a" == _fmt_throughput(0)
    assert "ops/s" in _fmt_throughput(500.0)
    assert "k ops/s" in _fmt_throughput(50_000.0)
    assert "M ops/s" in _fmt_throughput(5_000_000.0)
    assert "G ops/s" in _fmt_throughput(5_000_000_000.0)


def test_fmt_bandwidth_chooses_correct_units():
    """Bandwidth formatter must pick B/KB/MB/GB based on magnitude."""
    from py4j.tests.perf.report import _fmt_bandwidth
    assert "n/a" == _fmt_bandwidth(None)
    assert "n/a" == _fmt_bandwidth(0)
    assert "B/s" in _fmt_bandwidth(500)
    assert "KB/s" in _fmt_bandwidth(50_000)
    assert "MB/s" in _fmt_bandwidth(5_000_000)
    assert "GB/s" in _fmt_bandwidth(5_000_000_000)


def test_fmt_cpu_ratio_and_latency():
    """CPU ratio formats as 2-decimal float; latency reuses human_duration."""
    from py4j.tests.perf.report import _fmt_cpu_ratio, _fmt_latency
    assert "n/a" == _fmt_cpu_ratio(None)
    assert "0.95" == _fmt_cpu_ratio(0.95)
    assert "n/a" == _fmt_latency(None)
    assert "n/a" == _fmt_latency(0.0)
    assert "ms" in _fmt_latency(0.0012)
    assert "µs" in _fmt_latency(0.000012) or "\u00b5s" in _fmt_latency(0.000012)
    assert "ns" in _fmt_latency(1e-9)


def test_metrics_robust_to_zero_iterations_per_round():
    """Defensive: iterations_per_round=0 should not divide by zero."""
    rounds = [0.01] * 5
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=0)
    assert scen["metrics"]["latency_per_op_s"] is None
    assert scen["metrics"]["throughput_ops_per_s"] is None


def test_metrics_robust_to_empty_rounds():
    """No rounds -> all derived metrics are None (or 0 for errors)."""
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=[], warmup_rounds=3,
        iterations_per_round=10)
    assert scen["metrics"]["latency_per_op_s"] is None
    assert scen["metrics"]["throughput_ops_per_s"] is None
    assert scen["metrics"]["bandwidth_bytes_per_s"] is None
    assert scen["metrics"]["cpu_time_ratio"] is None
    assert scen["metrics"]["errors"] == 0


def test_metrics_cpu_ratio_handles_mismatched_lengths():
    """If cpu_rounds and rounds have different lengths (defensive coding),
    cpu_time_ratio should not crash — return None instead."""
    rounds = [0.01] * 5
    cpu_rounds = [0.009] * 3  # mismatched
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=1,
        cpu_rounds=cpu_rounds)
    assert scen["metrics"]["cpu_time_ratio"] is None


def test_stats_module_compute_stats_matches_report_alias():
    """The split out stats.compute_stats is what report.compute_stats
    re-exports — must produce identical output."""
    from py4j.tests.perf import stats as stats_mod
    from py4j.tests.perf import report as report_mod
    rounds = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats_mod.compute_stats(rounds) == report_mod.compute_stats(rounds)


def test_inference_module_publishes_all_named_exports():
    """Sanity: importing the moved names from report (back-compat) gives
    the SAME object as importing from inference (the new home)."""
    from py4j.tests.perf import inference
    from py4j.tests.perf import report
    for name in ("_mannwhitney_u", "_ks_two_sample",
                 "_hodges_lehmann_2sample", "_compute_bootstrap_deltas",
                 "_ci_from_deltas", "_rope_probabilities",
                 "_bootstrap_median_delta_ci", "sprt_decide",
                 "_stats_verdict", "_confident_verdict", "_verdict"):
        assert getattr(inference, name) is getattr(report, name), \
            "{0} not re-exported".format(name)


def test_metrics_throughput_and_latency_derived_from_rounds():
    """build_scenario_entry must populate metrics.throughput and
    metrics.latency_per_op_s from per-round duration + iterations."""
    rounds = [0.010] * 30  # 10 ms each, with 1000 ops per round
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=1000)
    m = scen["metrics"]
    # latency: 10 ms / 1000 ops = 10 µs / op
    assert m["latency_per_op_s"] is not None
    assert abs(m["latency_per_op_s"] - 1e-5) < 1e-7
    # throughput: 1000 ops / 10 ms = 100k ops/sec
    assert m["throughput_ops_per_s"] is not None
    assert abs(m["throughput_ops_per_s"] - 100000.0) < 1.0


def test_metrics_bandwidth_when_bytes_per_iteration_declared():
    """metrics.bandwidth_bytes_per_s must populate iff scenario declares
    bytes_per_iteration."""
    rounds = [0.001] * 10  # 1 ms each
    scen_with = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=100,
        bytes_per_iteration=1024)
    # 100 ops × 1024 bytes / 1 ms = 102.4 MB/s
    assert scen_with["metrics"]["bandwidth_bytes_per_s"] is not None
    expected = 100 * 1024 / 0.001
    assert abs(scen_with["metrics"]["bandwidth_bytes_per_s"] - expected) < 1.0

    # Without declaration, bandwidth stays None.
    scen_without = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=100)
    assert scen_without["metrics"]["bandwidth_bytes_per_s"] is None


def test_metrics_cpu_time_ratio_from_cpu_rounds():
    """metrics.cpu_time_ratio = median(cpu_round / wall_round)."""
    rounds = [0.010, 0.010, 0.010, 0.010, 0.010]
    cpu_rounds = [0.0095, 0.0099, 0.0090, 0.0098, 0.0096]  # ~95-99% CPU
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=1,
        cpu_rounds=cpu_rounds)
    ratio = scen["metrics"]["cpu_time_ratio"]
    assert ratio is not None
    # Median of [0.95, 0.99, 0.90, 0.98, 0.96] = 0.96
    assert 0.94 < ratio < 0.99


def test_metrics_cpu_time_ratio_none_when_cpu_rounds_missing():
    rounds = [0.010] * 5
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=1)
    assert scen["metrics"]["cpu_time_ratio"] is None


def test_metrics_errors_propagates_to_scenario():
    """The errors count from the runner must surface in metrics."""
    rounds = [0.01] * 5
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3,
        iterations_per_round=1,
        errors=3)
    assert scen["metrics"]["errors"] == 3


def test_runner_counts_measure_errors_without_aborting():
    """A scenario whose measure() sometimes raises must NOT abort the
    run; the framework counts errors and finishes the round."""
    from py4j.tests.perf.runner import run_macro

    class _Flaky(object):
        repeats_per_round = 5
        iterations_per_round = 5

        def __init__(self):
            self._calls = 0

        def measure(self, gateway):
            self._calls += 1
            if self._calls % 7 == 0:
                raise RuntimeError("simulated transient failure")

    outcome = run_macro(
        _Flaky(), gateway=None,
        warmup_rounds=1, max_rounds=5, min_rounds=3,
        max_seconds=60.0, target_ci_width=None,
        auto_scale_repeats=False)
    assert outcome["errors"] > 0  # at least some failures counted
    # But the round loop completed all 5 rounds anyway.
    assert len(outcome["rounds"]) == 5


def test_compare_report_includes_percentile_deltas():
    """The 'Per-percentile deltas' section must appear with Δ median /
    p95 / p99 / p99.9 columns."""
    a = _gauss_rounds(100, 3, n=80, seed=1)
    b = _gauss_rounds(95, 3, n=80, seed=2)
    base = build_report(
        {}, [],
        [build_scenario_entry(
            scenario_id="X1", name="x1", runner="macro",
            rounds=a, warmup_rounds=3, iterations_per_round=1)])
    curr = build_report(
        {}, [],
        [build_scenario_entry(
            scenario_id="X1", name="x1", runner="macro",
            rounds=b, warmup_rounds=3, iterations_per_round=1)])
    r = compare(base, curr)
    assert "Per-percentile deltas" in r.markdown
    assert "Δ p99" in r.markdown
    assert "Δ p99.9" in r.markdown


def test_sprt_decides_faster_for_clear_speedup():
    """A clean 15% speedup with tight noise must produce 'faster'."""
    from py4j.tests.perf.report import sprt_decide
    base = _gauss_rounds(100, 2, n=80, seed=1)
    curr = _gauss_rounds(85, 2, n=80, seed=2)
    decision = sprt_decide(base, curr, mde=0.05)
    assert decision == "faster"


def test_sprt_decides_regression_for_clear_slowdown():
    from py4j.tests.perf.report import sprt_decide
    base = _gauss_rounds(100, 2, n=80, seed=1)
    curr = _gauss_rounds(115, 2, n=80, seed=2)
    decision = sprt_decide(base, curr, mde=0.05)
    assert decision == "regression"


def test_sprt_decides_neutral_for_identical_distributions():
    """When both sides have the same distribution and enough rounds,
    the CI should fit inside the ROPE band and SPRT decides 'neutral'."""
    from py4j.tests.perf.report import sprt_decide
    base = _gauss_rounds(100, 1, n=200, seed=1)
    curr = _gauss_rounds(100, 1, n=200, seed=2)
    decision = sprt_decide(base, curr, mde=0.05)
    assert decision == "neutral"


def test_sprt_undecided_for_marginal_effect_with_few_samples():
    """A small effect (3%) with few samples must remain undecided —
    the CI straddles the ROPE boundary."""
    from py4j.tests.perf.report import sprt_decide
    base = _gauss_rounds(100, 5, n=15, seed=1)
    curr = _gauss_rounds(97, 5, n=15, seed=2)
    decision = sprt_decide(base, curr, mde=0.05)
    assert decision == "undecided"


def test_sprt_undecided_for_empty_inputs():
    from py4j.tests.perf.report import sprt_decide
    assert sprt_decide([], [1.0]) == "undecided"
    assert sprt_decide([1.0], []) == "undecided"


def test_estimate_rounds_needed_scales_with_cv_and_target():
    """Rounds needed grows as (CV / target)^2."""
    from py4j.tests.perf.runner import _estimate_rounds_needed
    # Low CV, loose target: very few rounds.
    assert _estimate_rounds_needed(0.02, 0.05) <= 5
    # Higher CV at same target: more rounds.
    n_low = _estimate_rounds_needed(0.05, 0.03)
    n_high = _estimate_rounds_needed(0.20, 0.03)
    assert n_high > n_low
    # Tighter target at same CV: more rounds.
    n_loose = _estimate_rounds_needed(0.10, 0.05)
    n_tight = _estimate_rounds_needed(0.10, 0.01)
    assert n_tight > n_loose
    # Always >= 3 (framework minimum).
    assert _estimate_rounds_needed(0.0, 0.0) == 3
    # Capped at 1000 even for pathological inputs.
    assert _estimate_rounds_needed(10.0, 0.001) == 1000


def test_power_analysis_warmup_returns_per_round_and_cv():
    """power_analysis_warmup must produce (per_round, cv, repeats) on a
    scenario whose measure() has a known duration."""
    from py4j.tests.perf.runner import power_analysis_warmup

    class _Stable(object):
        # ~5 ms per call, modest jitter.
        repeats_per_round = 1
        _rng = random.Random(7)

        def measure(self, gateway):
            time.sleep(0.005 + self._rng.gauss(0, 0.0005))

    per_round, cv, repeats = power_analysis_warmup(
        _Stable(), gateway=None,
        n_warmup_rounds=3,
        auto_scale_repeats_flag=False)
    assert per_round > 0
    assert cv >= 0.0
    assert repeats == 1


def test_noise_budget_flags_over_budget_scenarios():
    """A scenario whose observed CV is more than 2x its expected_cv
    must be marked noise_over_budget=True."""
    # Construct rounds with CV ~30% (way over 2 * 0.05 = 10%).
    rounds = [1.0, 0.5, 1.5, 0.7, 1.3, 0.6, 1.4]
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1,
        expected_cv=0.05)
    assert scen["noise_over_budget"] is True
    # Observed CV should land near 0.3.
    assert 0.2 < scen["observed_cv"] < 0.5


def test_noise_budget_clean_scenario_within_budget():
    """A clean scenario whose observed CV is below 2x its expected_cv
    must NOT be flagged."""
    # Rounds with CV ~2% (well under 2 * 0.10 = 20%).
    rounds = [1.00, 1.01, 0.99, 1.02, 0.98, 1.00, 1.01]
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1,
        expected_cv=0.10)
    assert scen["noise_over_budget"] is False


def test_noise_budget_n_a_when_not_declared():
    """expected_cv=None disables the budget check entirely."""
    rounds = [1.0, 0.5, 1.5, 0.7]
    scen = build_scenario_entry(
        scenario_id="X", name="x", runner="macro",
        rounds=rounds, warmup_rounds=3, iterations_per_round=1,
        expected_cv=None)
    assert scen["noise_over_budget"] is False
    assert scen["expected_cv"] is None


def test_auto_scale_repeats_bumps_short_scenarios():
    """A short measure() call must produce repeats > 1 so each timed
    round is ~100 ms.

    We don't pin a specific repeat count because OS timer resolution
    varies wildly:
      - Linux / macOS: ~1 ms (time.sleep(0.001) ~ 1 ms)
      - Windows: ~15.6 ms (time.sleep(0.001) ~ 15 ms)
    so the same code produces ~100 reps on Linux/macOS but ~7 on
    Windows. What matters is that scaling kicks in at all.
    """
    from py4j.tests.perf.runner import _auto_scale_repeats

    class _Short(object):
        repeats_per_round = 1

        def measure(self, gateway):
            time.sleep(0.001)

    repeats = _auto_scale_repeats(_Short(), gateway=None, warmup_rounds=1)
    # On any platform, a sub-100-ms scenario must scale repeats up.
    assert repeats > 1
    # Sanity: should not blow up to the safety cap (10000).
    assert repeats < 10000


def test_auto_scale_repeats_respects_declared_value():
    """If a scenario already declares repeats_per_round > 1, the
    auto-scaler must not overwrite it."""
    from py4j.tests.perf.runner import _auto_scale_repeats

    class _AlreadyTuned(object):
        repeats_per_round = 7

        def measure(self, gateway):
            time.sleep(0.0001)  # would be auto-scaled WAY higher

    repeats = _auto_scale_repeats(
        _AlreadyTuned(), gateway=None, warmup_rounds=1)
    assert repeats == 7


def test_auto_scale_repeats_leaves_long_scenarios_alone():
    """A scenario whose measure() already takes >= 100 ms doesn't need
    multiple reps; the auto-scaler returns 1."""
    from py4j.tests.perf.runner import _auto_scale_repeats

    class _Long(object):
        repeats_per_round = 1

        def measure(self, gateway):
            time.sleep(0.12)

    repeats = _auto_scale_repeats(_Long(), gateway=None, warmup_rounds=1)
    assert repeats == 1


def test_strict_bench_runs_additional_linux_checks(monkeypatch):
    """check_guards(strict_bench=True) must invoke the Linux noise-floor
    checks; check_guards() without the flag must not."""
    from py4j.tests.perf import environment

    called = []
    monkeypatch.setattr(
        environment, "_check_intel_turbo_linux",
        lambda: (called.append("turbo"), None)[1])
    monkeypatch.setattr(
        environment, "_check_cpu_governor_linux",
        lambda: (called.append("gov"), None)[1])
    monkeypatch.setattr(
        environment, "_check_smt_siblings_linux",
        lambda: (called.append("smt"), None)[1])

    called.clear()
    environment.check_guards(strict_bench=False)
    assert "turbo" not in called
    assert "gov" not in called
    assert "smt" not in called

    called.clear()
    environment.check_guards(strict_bench=True)
    assert "turbo" in called
    assert "gov" in called
    assert "smt" in called


@pytest.mark.skipif(platform.system() != "Linux",
                    reason="Turbo / governor / SMT checks read /sys "
                           "which is Linux-only; on other OSes they "
                           "short-circuit to None.")
def test_strict_bench_linux_checks_dont_crash():
    """On a real Linux box, the strict-bench guards must run without
    raising (they may or may not warn depending on /sys state)."""
    from py4j.tests.perf.environment import (
        _check_intel_turbo_linux, _check_cpu_governor_linux,
        _check_smt_siblings_linux)
    # None or EnvironmentWarning is acceptable — anything that raises
    # is a bug.
    for check in (_check_intel_turbo_linux,
                  _check_cpu_governor_linux,
                  _check_smt_siblings_linux):
        result = check()
        assert result is None or isinstance(result, str)


def test_adaptive_should_stop_clean_distribution():
    """Tight bootstrap CI on the median -> should_stop returns True.

    Tests the decision logic in isolation with a hand-crafted round
    array. Bypasses the timing-dependent run_macro integration so the
    assertion is platform-independent.
    """
    from py4j.tests.perf.runner import _adaptive_should_stop
    # 30 rounds clustered tightly around 1.0 (~1% spread).
    rounds = [1.0 + (i - 15) * 0.0005 for i in range(30)]
    # Half-width vs median should easily be < 5%.
    assert _adaptive_should_stop(rounds, target_ci_width=0.05) is True


def test_adaptive_should_stop_noisy_distribution():
    """Wide bootstrap CI -> should_stop returns False.

    The CI half-width on a high-CV sample (~30%) won't be tight
    enough to satisfy a 1% target.
    """
    from py4j.tests.perf.runner import _adaptive_should_stop
    rng = random.Random(99)
    # 30 rounds with ~30% CV.
    rounds = [max(0.1, 1.0 + rng.gauss(0, 0.3)) for _ in range(30)]
    assert _adaptive_should_stop(rounds, target_ci_width=0.01) is False


def test_adaptive_should_stop_target_none_returns_false():
    """target_ci_width=None or <= 0 disables the check."""
    from py4j.tests.perf.runner import _adaptive_should_stop
    rounds = [1.0] * 10
    assert _adaptive_should_stop(rounds, None) is False
    assert _adaptive_should_stop(rounds, 0.0) is False
    assert _adaptive_should_stop(rounds, -0.05) is False


def test_adaptive_should_stop_empty_rounds_returns_false():
    """Fewer than 2 rounds -> can't bootstrap a CI -> don't stop."""
    from py4j.tests.perf.runner import _adaptive_should_stop
    assert _adaptive_should_stop([], 0.05) is False
    assert _adaptive_should_stop([1.0], 0.05) is False


def test_adaptive_disabled_does_not_stop_early():
    """target_ci_width=None must leave adaptive_stopped_early False
    regardless of how many rounds the loop ultimately runs."""
    from py4j.tests.perf.runner import run_macro

    class _Trivial(object):
        iterations_per_round = 1
        repeats_per_round = 1

        def measure(self, gateway):
            # No-op (still executes a python statement).
            return None

    outcome = run_macro(
        _Trivial(), gateway=None,
        warmup_rounds=1, max_rounds=10, min_rounds=3,
        max_seconds=60.0, target_ci_width=None,
        # Disable auto-scale so the test doesn't interact with the
        # auto-repeats heuristic on Windows (where sleep precision
        # would otherwise inflate repeats and trip the time budget).
        auto_scale_repeats=False)
    # The only invariant we care about here: adaptive cannot have
    # stopped early when it wasn't enabled. The actual round count
    # depends on whether max_rounds OR max_seconds tripped first,
    # which is platform-dependent.
    assert outcome["adaptive_stopped_early"] is False
    assert len(outcome["rounds"]) >= 3  # at least min_rounds


def test_merge_results_across_runs_concatenates_rounds():
    """--n-runs N: per-scenario rounds[] must be concatenated, not lost."""
    from py4j.tests.perf.__main__ import _merge_results_across_runs
    run1 = [build_scenario_entry(
        scenario_id="M1", name="m1", runner="macro",
        rounds=[1.0, 2.0, 3.0], warmup_rounds=3,
        iterations_per_round=1, budget_triggered=False)]
    run2 = [build_scenario_entry(
        scenario_id="M1", name="m1", runner="macro",
        rounds=[4.0, 5.0, 6.0], warmup_rounds=3,
        iterations_per_round=1, budget_triggered=False)]
    merged = _merge_results_across_runs([run1, run2])
    assert len(merged) == 1
    assert merged[0]["id"] == "M1"
    assert merged[0]["rounds"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert merged[0]["measured_rounds"] == 6
    # Stats must be recomputed over the pooled distribution.
    assert merged[0]["stats"]["median"] == 3.5
    assert merged[0]["stats"]["min"] == 1.0
    assert merged[0]["stats"]["max"] == 6.0


def test_merge_results_across_runs_propagates_budget_triggered():
    """If any single run hit the budget cap, the merged entry must flag
    it - a reviewer should know at least one run was truncated."""
    from py4j.tests.perf.__main__ import _merge_results_across_runs
    run1 = [build_scenario_entry(
        scenario_id="X2", name="x2", runner="macro",
        rounds=[1.0, 2.0], warmup_rounds=3,
        iterations_per_round=1, budget_triggered=False)]
    run2 = [build_scenario_entry(
        scenario_id="X2", name="x2", runner="macro",
        rounds=[3.0, 4.0], warmup_rounds=3,
        iterations_per_round=1, budget_triggered=True)]
    merged = _merge_results_across_runs([run1, run2])
    assert merged[0]["budget_triggered"] is True


def test_merge_results_across_runs_keeps_distinct_scenarios():
    """Different scenario IDs across runs must each appear in the
    merged output (no accidental collisions)."""
    from py4j.tests.perf.__main__ import _merge_results_across_runs
    run1 = [
        build_scenario_entry(
            scenario_id="M1", name="m1", runner="macro",
            rounds=[1.0], warmup_rounds=3,
            iterations_per_round=1, budget_triggered=False),
        build_scenario_entry(
            scenario_id="M2", name="m2", runner="macro",
            rounds=[2.0], warmup_rounds=3,
            iterations_per_round=1, budget_triggered=False),
    ]
    run2 = [
        build_scenario_entry(
            scenario_id="M1", name="m1", runner="macro",
            rounds=[3.0], warmup_rounds=3,
            iterations_per_round=1, budget_triggered=False),
        build_scenario_entry(
            scenario_id="M2", name="m2", runner="macro",
            rounds=[4.0], warmup_rounds=3,
            iterations_per_round=1, budget_triggered=False),
    ]
    merged = _merge_results_across_runs([run1, run2])
    ids = [m["id"] for m in merged]
    assert ids == ["M1", "M2"]
    by_id = {m["id"]: m for m in merged}
    assert by_id["M1"]["rounds"] == [1.0, 3.0]
    assert by_id["M2"]["rounds"] == [2.0, 4.0]


def test_compare_report_includes_rope_columns():
    """The ROPE columns (P(better), P(same), P(worse)) must appear in
    the comparison verdict table."""
    a = _gauss_rounds(100, 3, n=80, seed=1)
    b = _gauss_rounds(95, 3, n=80, seed=2)
    base_scen = build_scenario_entry(
        scenario_id="X1", name="x1", runner="macro",
        rounds=a, warmup_rounds=3, iterations_per_round=1)
    curr_scen = build_scenario_entry(
        scenario_id="X1", name="x1", runner="macro",
        rounds=b, warmup_rounds=3, iterations_per_round=1)
    base = build_report({}, [], [base_scen])
    curr = build_report({}, [], [curr_scen])
    r = compare(base, curr)
    assert "P(better)" in r.markdown
    assert "P(same)" in r.markdown
    assert "P(worse)" in r.markdown
