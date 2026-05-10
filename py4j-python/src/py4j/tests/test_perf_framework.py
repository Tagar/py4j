"""Self-tests for the perf framework.

Tests are fully in-process - no JVM spawn, no network. They cover the
statistical helpers, comparison verdicts, filtering logic, and report
I/O round-trips. End-to-end scenarios (which do need a JVM) are
validated by running `python -m py4j.tests.perf smoke` manually.
"""

import json
import os
import platform
import tempfile

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
