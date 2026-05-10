"""CLI entry: ``python -m py4j.tests.perf``.

Supported in phase 2:
    python -m py4j.tests.perf              Run all scenarios, write report
    python -m py4j.tests.perf --only M1,M5 Run only listed scenarios
    python -m py4j.tests.perf env          Print env metadata + guards
    python -m py4j.tests.perf smoke        Spawn+teardown a JVM (sanity)

Coming in later phases: --save, --compare, --fail-on-regression, macro
scenarios. Phase 2 handles micro scenarios only; macro scenarios are
silently skipped (the MACRO_SCENARIOS list is empty until phase 3).
"""

import argparse
import json
import os
import sys
import time

from py4j.tests.perf._pytest_micro import run_micro
from py4j.tests.perf.environment import (
    capture_metadata, check_guards, try_renice)
from py4j.tests.perf.jvm import JvmNotBuiltError, JvmStartupError, fresh_jvm
from py4j.tests.perf.report import (
    build_report, build_scenario_entry, compact_report, compare,
    read_json, write_json, write_markdown)
from py4j.tests.perf.runner import run_macro
from py4j.tests.perf.scenarios import ALL_SCENARIOS, filter_scenarios


def _parse_id_list(value):
    if not value:
        return None
    return [p.strip() for p in value.split(",") if p.strip()]


def _cmd_env(args):
    meta = capture_metadata()
    warnings = check_guards()
    print(json.dumps({"environment": meta, "warnings": warnings}, indent=2))
    return 1 if (args.strict and warnings) else 0


def _cmd_smoke(args):
    warnings = check_guards()
    for w in warnings:
        print("warning: {0}".format(w), file=sys.stderr)
    if args.strict and warnings:
        return 1

    print("Spawning JVM...")
    t0 = time.perf_counter()
    try:
        with fresh_jvm(heap=args.jvm_heap) as gateway:
            spawn_time = time.perf_counter() - t0
            print("JVM ready in {0:.2f}s".format(spawn_time))
            ct = gateway.jvm.java.lang.System.currentTimeMillis()
            print("Round-trip OK (currentTimeMillis -> {0})".format(ct))
    except JvmNotBuiltError as e:
        print(str(e), file=sys.stderr)
        return 2
    except JvmStartupError as e:
        print(str(e), file=sys.stderr)
        return 3
    print("Shutdown OK.")
    return 0


def _run_macro_scenarios(selected, args):
    """Spawn a fresh JVM per macro scenario, run it, return result dicts."""
    results = []
    for s in selected:
        scenario_cls = s.handle
        scenario = scenario_cls()
        try:
            with fresh_jvm(
                heap=args.jvm_heap,
                enable_callbacks=scenario.enable_callbacks,
            ) as gateway:
                run_kwargs = {}
                if args.quick:
                    run_kwargs["max_rounds"] = 4
                    run_kwargs["max_seconds"] = 10.0
                    run_kwargs["warmup_rounds"] = 1
                outcome = run_macro(scenario, gateway, **run_kwargs)
        except (JvmNotBuiltError, JvmStartupError) as e:
            print("skipping {0}: {1}".format(s.id, e), file=sys.stderr)
            continue
        except Exception as e:
            print("error in {0}: {1}".format(s.id, e), file=sys.stderr)
            continue

        results.append(build_scenario_entry(
            scenario_id=s.id,
            name=s.name,
            runner="macro",
            rounds=outcome["rounds"],
            warmup_rounds=outcome["warmup_rounds"],
            iterations_per_round=outcome["iterations_per_round"],
            budget_triggered=outcome["budget_triggered"],
        ))
    return results


def _cmd_run(args):
    warnings = check_guards()
    for w in warnings:
        print("warning: {0}".format(w), file=sys.stderr)
    if args.strict and warnings:
        return 1

    if args.no_renice:
        from py4j.tests.perf.environment import current_nice
        renice_result = {
            "attempted": False, "succeeded": False,
            "before": current_nice(), "after": current_nice(),
            "target": args.renice_to, "reason": "--no-renice",
        }
    else:
        renice_result = try_renice(target_nice=args.renice_to)

    only = _parse_id_list(args.only)
    skip = _parse_id_list(args.skip)
    selected = filter_scenarios(ALL_SCENARIOS, only=only, skip=skip)
    if not selected:
        print("No scenarios match the selection.", file=sys.stderr)
        return 64

    micro_ids = [s.id for s in selected if s.runner == "pytest-benchmark"]
    macro_scenarios = [s for s in selected if s.runner == "macro"]

    results = []
    if micro_ids:
        print("Running {0} micro scenarios: {1}".format(
            len(micro_ids), ", ".join(micro_ids)))
        results.extend(run_micro(micro_ids, quick=args.quick))
    if macro_scenarios:
        macro_ids = [s.id for s in macro_scenarios]
        print("Running {0} macro scenarios: {1}".format(
            len(macro_ids), ", ".join(macro_ids)))
        results.extend(_run_macro_scenarios(macro_scenarios, args))

    if not results:
        print("No measurements produced.", file=sys.stderr)
        return 65

    metadata = capture_metadata()
    metadata["renice"] = renice_result
    report = build_report(
        environment=metadata,
        warnings=warnings,
        scenarios=results,
    )

    if args.save:
        out = compact_report(report) if args.save_compact else report
        write_json(out, args.save)
        print("Wrote {0}{1}".format(
            args.save, " (compact)" if args.save_compact else ""))
        return 0

    output_dir = args.output_dir or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "perf_report.json")
    md_path = os.path.join(output_dir, "perf_report.md")
    write_json(report, json_path)

    if args.compare:
        try:
            baseline = read_json(args.compare)
        except (OSError, ValueError) as e:
            print("Failed to load baseline {0}: {1}".format(args.compare, e),
                  file=sys.stderr)
            return 66
        result = compare(baseline, report)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(result.markdown)
        print("Wrote {0}".format(json_path))
        print("Wrote {0}".format(md_path))
        print("Summary: {0} faster, {1} regressed, {2} inconclusive, "
              "{3} neutral".format(
                  len(result.faster_ids), len(result.regressed_ids),
                  len(result.inconclusive_ids), len(result.neutral_ids)))
        if result.regressed_ids and args.fail_on_regression:
            return 1
        return 0

    write_markdown(report, md_path)
    print("Wrote {0}".format(json_path))
    print("Wrote {0}".format(md_path))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m py4j.tests.perf",
        description="py4j performance testing framework.")
    parser.add_argument("--strict", action="store_true",
                        help="Turn environment warnings into hard failures.")
    parser.add_argument("--jvm-heap", default="4g",
                        help="JVM -Xms/-Xmx value. Default: 4g.")
    parser.add_argument("--only",
                        help="Comma-separated scenario IDs to include.")
    parser.add_argument("--skip",
                        help="Comma-separated scenario IDs to exclude.")
    parser.add_argument("--quick", action="store_true",
                        help="Reduce rounds for smoke runs (not for PRs).")
    parser.add_argument("--output-dir",
                        help="Directory for perf_report.{md,json}.")
    parser.add_argument("--save", metavar="FILE",
                        help="Write run JSON to FILE (skip markdown).")
    parser.add_argument("--save-compact", action="store_true",
                        help="With --save, drop per-round arrays to keep the "
                             "JSON small (suitable for committing a baseline).")
    parser.add_argument("--compare", metavar="FILE",
                        help="Diff this run against a baseline JSON at FILE.")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="Exit non-zero if --compare sees any regression.")
    parser.add_argument("--no-renice", action="store_true",
                        help="Skip the sudo renice at startup.")
    parser.add_argument("--renice-to", type=int, default=-15,
                        help="Target nice value for renice (default: -15).")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run scenarios (default if no command given).")
    sub.add_parser("env", help="Print environment metadata + guard warnings.")
    sub.add_parser("smoke", help="Spawn a JVM, do one round-trip, tear down.")

    args = parser.parse_args(argv)

    if args.command == "env":
        return _cmd_env(args)
    if args.command == "smoke":
        return _cmd_smoke(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
