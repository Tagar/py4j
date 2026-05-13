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
    compute_stats, read_json, sprt_decide, write_json, write_markdown)
from py4j.tests.perf.runner import (
    _estimate_rounds_needed, power_analysis_warmup, run_macro)
from py4j.tests.perf.scenarios import ALL_SCENARIOS, filter_scenarios


def _parse_id_list(value):
    if not value:
        return None
    return [p.strip() for p in value.split(",") if p.strip()]


def _cmd_env(args):
    meta = capture_metadata()
    warnings = check_guards(strict_bench=args.strict_bench)
    print(json.dumps({"environment": meta, "warnings": warnings}, indent=2))
    return 1 if (args.strict and warnings) else 0


def _cmd_smoke(args):
    warnings = check_guards(strict_bench=getattr(args, "strict_bench", False))
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


def _run_macro_scenarios(selected, args, baseline_rounds_by_id=None):
    """Spawn a fresh JVM per macro scenario, run it, return result dicts.

    ``baseline_rounds_by_id`` (optional): map of scenario_id ->
    baseline rounds list, used by SPRT stopping when --sprt-stopping
    is set.
    """
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
                    # Skip auto-scaling on --quick so smoke runs stay fast.
                    run_kwargs["auto_scale_repeats"] = False
                if args.no_auto_scale_repeats:
                    run_kwargs["auto_scale_repeats"] = False
                if args.target_ci_width is not None:
                    # CLI value is a percentage (3.0 = ±3 %); convert to
                    # fraction for runner.
                    run_kwargs["target_ci_width"] = args.target_ci_width / 100.0
                if args.max_rounds is not None:
                    run_kwargs["max_rounds"] = args.max_rounds
                if args.scenario_time_budget is not None:
                    run_kwargs["max_seconds"] = args.scenario_time_budget
                if (args.sprt_stopping and baseline_rounds_by_id
                        and baseline_rounds_by_id.get(s.id)):
                    base_rounds = baseline_rounds_by_id[s.id]
                    run_kwargs["sprt_callback"] = (
                        lambda curr, br=base_rounds: sprt_decide(br, curr))
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
            adaptive_stopped_early=outcome.get(
                "adaptive_stopped_early", False),
            sprt_decision=outcome.get("sprt_decision"),
            expected_cv=getattr(scenario, "expected_cv", None),
            cpu_rounds=outcome.get("cpu_rounds"),
            errors=outcome.get("errors", 0),
            bytes_per_iteration=getattr(scenario, "bytes_per_iteration", None),
        ))
    return results


def _run_power_analysis(macro_scenarios, args):
    """Run a 3-round warmup per macro scenario; print ETA + rounds needed.

    Quick warmup measures per-round time and CV. Combined with
    ``--target-ci-width`` (if set), we predict how many rounds each
    scenario needs and how long the full run will take. Lets the user
    decide whether to bump --n-runs / --target-ci-width / --quick
    before committing the wall-clock.

    Returns a list of dicts (one per scenario) for the caller to use.
    """
    if not macro_scenarios:
        return []

    target = (args.target_ci_width / 100.0
              if args.target_ci_width is not None else None)
    n_runs = max(1, args.n_runs)
    max_rounds = args.max_rounds if args.max_rounds is not None else 50

    print("--- Power analysis (3-round warmup per scenario) ---")
    analyses = []
    total_eta_s = 0.0
    for s in macro_scenarios:
        scenario_cls = s.handle
        scenario = scenario_cls()
        try:
            with fresh_jvm(
                heap=args.jvm_heap,
                enable_callbacks=scenario.enable_callbacks,
            ) as gateway:
                per_round_s, cv, repeats = power_analysis_warmup(
                    scenario, gateway,
                    n_warmup_rounds=3,
                    auto_scale_repeats_flag=not args.no_auto_scale_repeats)
        except (JvmNotBuiltError, JvmStartupError) as e:
            print("  {0}: skipping ({1})".format(s.id, e), file=sys.stderr)
            continue
        except Exception as e:
            print("  {0}: error ({1})".format(s.id, e), file=sys.stderr)
            continue

        # Predicted rounds: if target_ci_width set, use the CV-based
        # estimate; otherwise just use max_rounds.
        if target is not None:
            predicted = _estimate_rounds_needed(cv, target)
            predicted = min(predicted, max_rounds)
        else:
            predicted = max_rounds

        scenario_eta = per_round_s * predicted * n_runs
        total_eta_s += scenario_eta

        analyses.append({
            "id": s.id,
            "name": s.name,
            "per_round_s": per_round_s,
            "cv": cv,
            "repeats": repeats,
            "predicted_rounds": predicted,
            "scenario_eta_s": scenario_eta,
        })

        print("  {0:<24} cv={1:5.1%}  rounds~{2:<3d}  eta~{3:>6.1f}s"
              .format(s.id, cv, predicted, scenario_eta))

    print("--- Full-run ETA: ~{0:.0f}s ({1:.1f} min){2} ---".format(
        total_eta_s, total_eta_s / 60.0,
        ", x{0} runs".format(n_runs) if n_runs > 1 else ""))
    return analyses


def _merge_results_across_runs(results_per_run):
    """Pool per-round measurements across N independent framework runs.

    Each entry in ``results_per_run`` is a list of scenario_entries
    produced by one complete framework iteration (fresh JVM per
    scenario). The returned single list has, per scenario ID, the
    ``rounds`` arrays concatenated across runs and ``stats`` recomputed
    over the pool.

    Why this helps: when ``--n-runs`` > 1 the framework re-spawns the
    JVM and re-runs every scenario in a different process. JIT
    decisions, GC schedules, OS scheduler state, and thermal conditions
    all vary between runs. Concatenating per-round data folds *inter-
    run* variance into the sampled distribution, so the bootstrap CI
    and Mann-Whitney p-value reflect a more honest noise floor and
    are less likely to under-report uncertainty on a lucky single
    short run.
    """
    by_id = {}
    for run_results in results_per_run:
        for entry in run_results:
            sid = entry["id"]
            if sid not in by_id:
                merged = dict(entry)
                merged["rounds"] = []
                merged["measured_rounds"] = 0
                merged["budget_triggered"] = False
                by_id[sid] = merged
            by_id[sid]["rounds"].extend(entry.get("rounds", []) or [])
            # Keep the largest warmup_rounds we saw across runs (purely
            # informational; warmup samples are never included in stats).
            wr = entry.get("warmup_rounds", 0)
            if wr > by_id[sid].get("warmup_rounds", 0):
                by_id[sid]["warmup_rounds"] = wr
            # If any run hit the budget cap, surface that on the merged
            # entry — a reviewer should know at least one run was
            # truncated.
            if entry.get("budget_triggered"):
                by_id[sid]["budget_triggered"] = True
    for sid, merged in by_id.items():
        merged["measured_rounds"] = len(merged["rounds"])
        merged["stats"] = compute_stats(merged["rounds"])
    # Preserve the order they first appeared.
    seen = set()
    out = []
    for run_results in results_per_run:
        for entry in run_results:
            sid = entry["id"]
            if sid not in seen:
                seen.add(sid)
                out.append(by_id[sid])
    return out


def _cmd_run(args):
    warnings = check_guards(strict_bench=getattr(args, "strict_bench", False))
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

    if args.analyze or args.analyze_only:
        _run_power_analysis(macro_scenarios, args)
        if args.analyze_only:
            return 0

    # SPRT stopping needs the baseline rounds at measurement time, not
    # at compare time. Load early when --sprt-stopping + --compare are
    # both set.
    baseline_rounds_by_id = None
    if args.sprt_stopping:
        if not args.compare:
            print("warning: --sprt-stopping requires --compare; ignoring",
                  file=sys.stderr)
        else:
            try:
                baseline = read_json(args.compare)
                baseline_rounds_by_id = {
                    s["id"]: (s.get("rounds") or [])
                    for s in baseline.get("scenarios", [])
                }
            except (OSError, ValueError) as e:
                print("warning: --sprt-stopping could not load "
                      "baseline {0}: {1} (continuing without SPRT)".format(
                          args.compare, e), file=sys.stderr)

    n_runs = max(1, args.n_runs)
    results_per_run = []
    for run_idx in range(n_runs):
        if n_runs > 1:
            print("=== Run {0}/{1} ===".format(run_idx + 1, n_runs))
        one_run = []
        if micro_ids:
            if run_idx == 0:
                print("Running {0} micro scenarios: {1}".format(
                    len(micro_ids), ", ".join(micro_ids)))
            one_run.extend(run_micro(micro_ids, quick=args.quick))
        if macro_scenarios:
            if run_idx == 0:
                macro_ids = [s.id for s in macro_scenarios]
                print("Running {0} macro scenarios: {1}".format(
                    len(macro_ids), ", ".join(macro_ids)))
            one_run.extend(_run_macro_scenarios(
                macro_scenarios, args, baseline_rounds_by_id))
        results_per_run.append(one_run)

    if n_runs == 1:
        results = results_per_run[0]
    else:
        results = _merge_results_across_runs(results_per_run)

    if not results:
        print("No measurements produced.", file=sys.stderr)
        return 65

    metadata = capture_metadata()
    metadata["renice"] = renice_result
    metadata["n_runs"] = n_runs
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
    parser.add_argument("--strict-bench", action="store_true",
                        help="Run the extended Linux noise-floor checks "
                             "(Turbo Boost / CPU governor / SMT siblings) "
                             "in addition to the default guards (battery, "
                             "load avg, macOS thermal). These guard "
                             "against the largest single sources of "
                             "per-round noise. Combine with --strict to "
                             "fail the run when any noise source is "
                             "detected. See README \"strict-bench mode\" "
                             "for the OS-tuning recipe (taskset, no_turbo, "
                             "chrt) you may want to apply alongside.")
    parser.add_argument("--jvm-heap", default="4g",
                        help="JVM -Xms/-Xmx value. Default: 4g.")
    parser.add_argument("--only",
                        help="Comma-separated scenario IDs to include.")
    parser.add_argument("--skip",
                        help="Comma-separated scenario IDs to exclude.")
    parser.add_argument("--quick", action="store_true",
                        help="Reduce rounds for smoke runs (not for PRs).")
    parser.add_argument("--sprt-stopping", action="store_true",
                        help="Sequential stopping: with --compare, after "
                             "each batch of macro rounds check the "
                             "bootstrap CI vs the baseline. Stop the "
                             "scenario early when the CI fully clears "
                             "the practical-equivalence band [-5%%, +5%%] "
                             "(declares 'faster' / 'regression') OR is "
                             "entirely inside it ('neutral'). Reduces "
                             "wall-clock by 2-5x for scenarios with "
                             "clear effects; close calls still run to "
                             "max_rounds. No-op without --compare.")
    parser.add_argument("--analyze", action="store_true",
                        help="Run a 3-round warmup per macro scenario "
                             "BEFORE the long measurement phase, then "
                             "print per-scenario CV + predicted rounds "
                             "needed + full-run ETA. Combine with "
                             "--target-ci-width to predict adaptive "
                             "stopping behavior. Macro only.")
    parser.add_argument("--analyze-only", action="store_true",
                        help="Like --analyze, but exit immediately after "
                             "the analysis. Useful for deciding whether "
                             "to commit the wall-clock for a full run.")
    parser.add_argument("--no-auto-scale-repeats", action="store_true",
                        help="Disable auto-scaling of repeats_per_round. "
                             "By default each timed round is auto-scaled "
                             "to >= 100 ms to keep scheduler-jitter "
                             "relative noise below 1%%. Disable when "
                             "investigating per-call timing in isolation "
                             "(e.g. profiling a single measure()). "
                             "Macro only.")
    parser.add_argument("--target-ci-width", type=float, default=None,
                        metavar="PCT",
                        help="Adaptive sampling: keep adding rounds (in "
                             "batches of 5) until the bootstrap CI "
                             "half-width on the median drops below this "
                             "many percent (e.g. 3.0 = ±3%%). Stops "
                             "early on clean scenarios, runs to "
                             "--max-rounds / --scenario-time-budget on "
                             "noisy ones. Composes with --n-runs (applied "
                             "per run, before pooling). Macro only.")
    parser.add_argument("--max-rounds", type=int, default=None, metavar="N",
                        help="Override the runner's default max_rounds "
                             "cap (30). Macro only.")
    parser.add_argument("--scenario-time-budget", type=float,
                        default=None, metavar="SECONDS",
                        help="Override the per-scenario wall-clock cap "
                             "(60 s default). Macro only.")
    parser.add_argument("--n-runs", type=int, default=1, metavar="N",
                        help="Run the whole framework N times in fresh "
                             "JVMs and pool per-round data across runs. "
                             "Captures inter-JVM variance (different "
                             "JIT decisions, GC schedules, scheduler "
                             "states). With N=3 the effective sample "
                             "size per scenario is 3x; the bootstrap "
                             "CI and Mann-Whitney p-value reflect a "
                             "more honest noise floor and you get more "
                             "confident verdicts on real-but-modest "
                             "5-10%% effects. Default: 1.")
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
