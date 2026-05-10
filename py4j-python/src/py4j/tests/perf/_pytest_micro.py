"""Programmatic pytest invoker for the micro-scenario suite.

pytest-benchmark already implements warm-up, iteration calibration, and
JSON output. We wrap it so the perf framework can drive it and translate
the result into the unified schema.
"""

import json
import os
import re
import tempfile

import pytest

from py4j.tests.perf.report import compute_stats


_MICRO_DIR = os.path.join(os.path.dirname(__file__), "scenarios")
_ID_FROM_NAME = re.compile(r"^test_([mM]\d+[a-z]?)_")


def run_micro(scenario_ids=None, quick=False, extra_args=None):
    """Run pytest-benchmark over ``scenarios/micro.py``.

    :param scenario_ids: optional iterable of scenario IDs (e.g. ['M1', 'M5a'])
        to filter. ``None`` runs all micro scenarios.
    :param quick: if True, use reduced round counts for smoke runs.
    :returns: list of scenario result dicts matching the unified schema.
    """
    bench_dir = tempfile.mkdtemp(prefix="py4j-perf-micro-")
    bench_json = os.path.join(bench_dir, "bench.json")

    args = [
        os.path.join(_MICRO_DIR, "micro.py"),
        "--benchmark-only",
        "--benchmark-disable-gc",
        "--benchmark-warmup=on",
        "--benchmark-warmup-iterations=5",
        "--benchmark-json=" + bench_json,
        "-q",
        "-p", "no:cacheprovider",
    ]
    if quick:
        args.extend([
            "--benchmark-min-rounds=10",
            "--benchmark-max-time=2",
        ])
    else:
        args.extend([
            "--benchmark-min-rounds=30",
            "--benchmark-max-time=10",
        ])

    if scenario_ids:
        # pytest -k supports 'or'-joined substrings; we match test names
        # like 'test_m1_' so the id 'M1' becomes the filter 'test_m1_'.
        terms = [_id_to_k_term(sid) for sid in scenario_ids]
        args.extend(["-k", " or ".join(terms)])

    if extra_args:
        args.extend(extra_args)

    pytest.main(args)

    if not os.path.exists(bench_json):
        return []

    return _translate(bench_json)


def _id_to_k_term(scenario_id):
    """Map 'M1' to 'test_m1_' so pytest -k hits exactly one function."""
    return "test_{0}_".format(scenario_id.lower())


def _id_from_test_name(name):
    """'test_m1_static_call_no_args' -> 'M1'; 'test_m5a_encode_int' -> 'M5a'.

    Only the first character (M / X prefix) is upper-cased so variant
    suffixes like 'a'/'b' stay lower-case and match the registry.
    """
    m = _ID_FROM_NAME.match(name)
    if not m:
        return name
    raw = m.group(1)
    return raw[0].upper() + raw[1:]


def _translate(bench_json_path):
    """Convert pytest-benchmark JSON to the unified schema."""
    with open(bench_json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    scenarios = []
    for entry in data.get("benchmarks", []):
        name = entry.get("name", "")
        stats = entry.get("stats", {})
        rounds = stats.get("data", [])
        scenario_id = _id_from_test_name(name)

        # pytest-benchmark 'rounds' field = measured-round count;
        # 'iterations' field = function-call count within one round
        # (which it auto-calibrates so each round takes ~min_time seconds).
        iterations_per_round = int(stats.get("iterations", 1))

        scenarios.append({
            "id": scenario_id,
            "name": name[len("test_"):] if name.startswith("test_") else name,
            "runner": "pytest-benchmark",
            "unit": "seconds",
            "warmup_rounds": 5,
            "measured_rounds": len(rounds),
            "iterations_per_round": iterations_per_round,
            "budget_triggered": False,
            "rounds": list(rounds),
            "stats": compute_stats(rounds),
        })
    return scenarios
