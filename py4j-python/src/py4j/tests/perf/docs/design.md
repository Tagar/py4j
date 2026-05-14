# py4j Performance Framework — Design

## 1. Context and Motivation

py4j is the Python↔Java bridge used heavily by PySpark. Every method call from Python into Java, every argument marshaled, every collection element iterated, traverses this code. A five-agent performance analysis of the current codebase identified several classes of optimization opportunity: text-protocol escape/encode hot paths, missing `TCP_NODELAY`, N-round-trip collection iteration, absent method/class caches, and vestigial Py2 compat shims.

Before changing any of that code, we need a performance framework that can **baseline the current behavior** and produce **defensible before/after comparisons** that committers can review. The existing `benchmark1.py` (≈150 LOC) covers a small subset of scenarios, requires manual Java-side startup, emits only `time.time()` deltas, and performs no statistics. It is inadequate for making a "this change is faster" claim that reviewers can trust.

This spec defines the framework we will build so that every performance-related PR in this line of work carries reproducible evidence.

## 2. Goals and Non-Goals

### Goals

1. **Single-command local tool.** A contributor runs `python -m py4j.tests.perf` and, after **≤6 minutes on commodity hardware**, receives a markdown report pasteable into a PR description and a JSON file for diffing.
2. **Before/after comparison without re-engineering.** `--save baseline.json` on master, then `--compare baseline.json` on a branch produces a diff table with per-scenario median deltas, p95 deltas, and noise bands.
3. **Statistical rigor sufficient to survive a skeptical reviewer.** Warm-up, multiple rounds, fresh JVM per scenario, disabled Python GC during measurement, fixed-size JVM heap, environment metadata in the report.
4. **Coverage of the gaps identified in the perf analysis.** All five gap classes (concurrent calls, callback throughput, large collection iteration, error paths, pool saturation) have scenarios.
5. **Minimal maintenance surface.** Uses `pytest-benchmark` where it fits, adds a small custom runner only for what pytest-benchmark cannot handle cleanly, leans on existing pytest infrastructure.

### Non-Goals

- CI integration. The user has deliberately chosen a local tool over a PR-gating CI check. A future PR can add a `workflow_dispatch` GitHub Actions job that runs this same code; nothing in this design precludes it.
- Historical regression tracking (asv-style dashboards, per-commit plots). Can be added later by consuming the JSON output; out of scope here.
- Java-side benchmark coverage. The Java side already has its own tests; this framework measures the Python↔Java round-trip from the Python perspective.
- Profiling / flamegraphs. This is a benchmarking framework; it measures, it does not profile. A contributor diagnosing a regression will use `cProfile`/`py-spy` separately.

## 3. Approach — Summary

A **hybrid** framework: `pytest-benchmark` drives seven micro-scenarios (per-call latency at steady state), a small custom runner drives six macro-scenarios (aggregate throughput, concurrent workloads, large collections). Both write into a unified JSON schema. A comparison layer merges two runs into a single markdown+JSON diff report. The tool auto-spawns a fresh `ExampleApplication` JVM per scenario, applies environment guards at startup, and captures a metadata snapshot for the report header.

## 4. Scenarios

Thirteen scenarios, split by runner. Each scenario has a stated purpose tied to a specific finding from the prior analysis.

### 4.1 Micro (pytest-benchmark) — per-call latency after warm-up

| ID | Scenario | Purpose |
|----|----------|---------|
| M1 | `System.currentTimeMillis()` (no-arg static call) | Raw round-trip latency floor |
| M2 | `StringBuilder.append(int)` / `append(str)` (instance method, small args) | Common hot path, small-arg marshaling |
| M3 | Repeated `jvm.java.lang.String` resolution on a fresh `JVMView` | Dispatch caching (or lack thereof) |
| M4 | Constructor + memory-managed release of a `StringBuilder` | Finalizer lock cost per object |
| M5 | Protocol-only: `get_command_part(x)` over a fixed sample of ints, floats, strings, bools, and references | Isolates serialization hot path from I/O; direct measurement of the Cython-candidate function |
| M6 | Protocol-only: `get_return_value(answer, …)` over a fixed sample of response shapes | Same, other direction |
| M7 | `escape_new_line` / `unescape_new_line` over realistic strings | Isolates the `str.translate`/Cython target |

M5, M6, and M7 operate on the protocol module directly — no JVM round-trip involved. They are micro-benchmarks in the truest sense and serve as the ceiling against which CPU-side optimizations (Cython, `str.translate`) are measured.

### 4.2 Macro (custom runner) — aggregate throughput, workload-sized inputs

| ID | Scenario | Purpose |
|----|----------|---------|
| X1 | N-thread concurrent calls (N ∈ {1, 4, 16}, **10,000 total calls** distributed across threads) | Connection-pool contention. Total work held constant across N so per-thread count scales down (10k/1, 10k/4, 10k/16); we measure scaling efficiency, not raw throughput. |
| X2 | `JavaList` iteration at sizes {1k, 10k, 100k} | O(N) round-trip gap — the single largest optimization headroom |
| X3 | `ListConverter.convert(python_list)` at sizes {100, 1k, 10k} | Per-element `add()` churn |
| X4 | Callback round-trip throughput: `Collections.sort` with a Python `Comparable` wrapper over a list of 100 elements, 100 iterations | Callback path |
| X5 | Error-path latency: a method that throws, 1,000 iterations | Exception parsing and recovery cost |
| X6 | Pool saturation: 50 concurrent requests on a pool sized 4, measuring p95 and p99 latency | Tail-latency characteristic under contention |

Macro scenarios always emit both aggregate throughput (ops/sec) and per-round timing distribution (so p50/p95/p99 are available for every scenario). X1 and X6 additionally emit per-thread distributions.

## 5. Framework Choice — Hybrid

`pytest-benchmark` is the primary runner. It provides auto-calibrated iterations per round, built-in warm-up, the statistics a reviewer expects (min, max, mean, median, stddev, IQR, p95), JSON output, and a recognizable report format that carries credibility because other projects use it.

The custom runner exists because pytest-benchmark's single-function-call model does not fit scenarios that:

- spawn multiple threads and measure aggregate throughput (X1, X6),
- operate on a large fixture built once (X2, X3),
- depend on a specific connection-pool size (X6),
- measure a specific exception-handling path (X5).

The custom runner is intentionally small: a single file (`runner.py`, target ≤200 LOC) that implements warm-up rounds, measured rounds, per-round timing capture, `gc.disable()` around measurement, and JSON emission that matches pytest-benchmark's schema (so the comparison layer handles both uniformly).

### 5.1 Unified JSON schema

```json
{
  "version": "1.0",
  "environment": { /* see §8 */ },
  "scenarios": [
    {
      "id": "M1",
      "name": "static_call_no_args",
      "runner": "pytest-benchmark",
      "unit": "seconds",
      "warmup_rounds": 5,
      "measured_rounds": 50,
      "iterations_per_round": 128,
      "rounds": [0.0000873, 0.0000881, ...],
      "stats": {
        "min": 0.0000712, "max": 0.0001204,
        "mean": 0.0000879, "median": 0.0000873,
        "stddev": 0.0000041, "iqr": 0.0000062,
        "p5": 0.0000814, "p95": 0.0000946, "p99": 0.0001012
      }
    },
    { "id": "X2-10k", ... }
  ]
}
```

Both runners emit the same shape. `iterations_per_round` is the count of operations in a single timed round (pytest-benchmark calibrates this; the macro runner fixes it at 1 since one scenario body = one round).

## 6. JVM Lifecycle

### 6.1 Auto-spawn

On startup the tool:

1. Checks for the tests jar at `py4j-java/build/libs/py4j-tests-<version>.jar`.
2. If missing or older than the Java source tree, runs `./gradlew testsJar` to build it.
3. On failure, prints a clear error ("tests jar missing and `./gradlew testsJar` failed; run it manually to see the build output") and exits non-zero.

### 6.2 Fresh JVM per scenario

Every scenario gets its own JVM subprocess. The lifecycle is:

```
for scenario in scenarios:
    jvm = spawn_jvm()        # ~1.5s, blocked until listen port accepts
    gateway = JavaGateway(...)
    try:
        if hasattr(scenario, "setup"):
            scenario.setup(gateway)      # optional; omit for scenarios that need no fixture data
        for _ in range(warmup_rounds):
            scenario.measure(gateway)
        for _ in range(measured_rounds):
            gc.disable()
            t0 = perf_counter()
            scenario.measure(gateway)
            rounds.append(perf_counter() - t0)
            gc.enable()
            gc.collect()
            if time_budget_exceeded(): break
    finally:
        gateway.shutdown()
        jvm.wait(timeout=5)
        if jvm.poll() is None: jvm.kill()
```

Each scenario file exposes a `measure(gateway) -> None` callable. `setup(gateway) -> None` is optional — scenarios that need pre-built fixtures (X2's 10k JavaList, X3's pre-converted maps) define it; M1/M2/M3 do not.

Total overhead of the restart-per-scenario pattern is ≈20 seconds across all thirteen scenarios (~1.5s × 13); the measured portion dominates at 3–4 minutes.

### 6.3 JVM flags

All spawned JVMs receive:

- `-Xms4g -Xmx4g` — heap pinned to prevent heap-growth latency during timed rounds.
- `-XX:+AlwaysPreTouch` — touches every heap page at startup so first-access page faults are out of the measurement window.
- No custom GC collector — we want measurements to reflect real-world py4j behavior under the JVM's default collector.

Heap size is configurable via `--jvm-heap=4g`.

## 7. Statistical Rigor

### 7.1 Micro (pytest-benchmark)

- Warm-up: 5 rounds, not timed.
- Measured: `min_rounds=30`, `max_rounds=100`; pytest-benchmark auto-calibrates `iterations_per_round` so each round takes ≥100 µs (reducing timer-resolution noise).
- `--benchmark-disable-gc` is set globally. Python's GC firing mid-measurement is the single largest noise source on long-running suites.

### 7.2 Macro (custom runner)

- Warm-up: 3 full scenario bodies, not timed.
- Measured: **10 rounds OR 30 seconds of elapsed measurement time, whichever comes first**, with a minimum of 3 measured rounds (so expensive scenarios like X2 at 100k don't block on 10 × 10s = 100s). Every individual round timing is recorded in the JSON output (so downstream tools can compute whichever percentile they want).
- Before each measured round: `gc.disable()` + `gc.collect()`.
- After each measured round: `gc.enable()`.
- The actual round count and whether the budget triggered are recorded in the JSON (`measured_rounds`, `budget_triggered`) so comparisons never silently compare a 10-round run against a 3-round one.

### 7.3 Noise band

The report computes `noise = (p95 − p5) / median` for each scenario. In the comparison view, any |Δ median| < 2 × noise is flagged as **inconclusive** rather than a claim. A contributor showing a 1% improvement on a scenario with 4% noise is not making a credible claim, and the report will say so.

## 8. Environment Controls

### 8.1 Startup guards

At tool startup, before any scenario runs, the environment layer checks:

- **Battery power** (`psutil.sensors_battery()`): warn if on battery (throttled CPU frequencies).
- **Load average** (`os.getloadavg()[0]`): warn if >0.5 × `os.cpu_count()`.
- **macOS thermal state** (`pmset -g therm`): warn if throttling.

Warnings print to stderr. If `--strict` is passed, any warning becomes a hard fail.

### 8.2 Report metadata

Every report (both standalone and comparison) includes a header block:

```
Environment:
  OS:        Darwin 25.4.0 (x86_64)
  CPU:       Intel Core i9-9880H @ 2.30GHz, 8 cores / 16 threads
  RAM:       32 GB
  Python:    3.12.3 (CPython, GCC 11.4.0)
  Java:      OpenJDK 21.0.2+13
  py4j:     0.10.9.9 (rev abc123f, branch perf-framework, dirty=false)
  Warnings: [battery-power, load-avg=4.2]
```

`psutil.cpu_freq()`, `platform.processor()`, and a git-rev lookup provide the data.

### 8.3 Explicitly out of scope

- **CPU pinning / taskset**: platform-specific (no native macOS equivalent), overkill for a local tool.
- **Turbo-boost disable**: requires root, user-specific. Mentioned in README as an advanced option.
- **Thermal-cycle cool-down between scenarios**: would add significant runtime; the fresh-JVM delay already partially absorbs heat.

## 9. Output and Comparison Workflow

### 9.1 Standalone run (no comparison)

`python -m py4j.tests.perf` writes:
- `perf_report.md` — markdown report with environment header + one table per scenario.
- `perf_report.json` — full raw data per §5.1.

Tables show: median, stddev, p5, p95, p99, iterations-per-round, rounds.

### 9.2 Save baseline

`python -m py4j.tests.perf --save baseline.json` writes the same JSON to the given path, skips the markdown report.

### 9.3 Comparison run

`python -m py4j.tests.perf --compare baseline.json` runs all scenarios, then emits a diff report:

```
# py4j perf — perf-framework vs baseline (saved 2026-04-18T14:30:00Z)

Environment (comparison): Darwin 25.4.0, Python 3.12.3, Java 21, rev abc123f
Environment (baseline):   Darwin 25.4.0, Python 3.12.3, Java 21, rev 7890def
Warnings: none

| Scenario       | Baseline median | This branch median | Δ median | Δ p95   | Noise | Verdict |
|----------------|-----------------|--------------------|----------|---------|-------|---------|
| M1 static      | 87.3 µs         | 62.1 µs            | **-29%** | -27%    | ±3%   | faster  |
| M5 encode_int  | 412 ns          | 248 ns             | **-40%** | -38%    | ±1%   | faster  |
| X2 iter_10k    | 3.41 s          | 0.09 s             | **-97%** | -96%    | ±2%   | faster  |
| X4 callbacks/s | 2 140           | 2 155              | +0.7%    | —       | ±4%   | ⚠ inconclusive |
| X5 error_path  | 1.24 ms         | 1.31 ms            | +5.6%    | +7.2%   | ±2%   | **regression** |
```

`Verdict` is derived:
- **faster** — Δ median ≤ −5% AND |Δ| > 2 × noise.
- **regression** — Δ median ≥ +5% AND |Δ| > 2 × noise.
- **inconclusive** — |Δ| ≤ 2 × noise.
- **neutral** — otherwise.

Verdicts are informational. The tool does not exit non-zero on a regression (it's a local tool; the contributor decides). `--fail-on-regression` is available for strict users.

### 9.4 Environment mismatch warning

If `uname`, Python version, Java version, or CPU model differ between baseline and comparison, a loud warning appears above the table. Reviewers need to see this — a comparison between a baseline on a MacBook and a comparison on a desktop Ryzen is nearly meaningless.

## 10. File Layout

```
py4j-python/src/py4j/tests/perf/
├── __init__.py
├── __main__.py          # CLI entry point
├── jvm.py               # auto-spawn, jar check, teardown
├── environment.py       # guards, metadata capture
├── runner.py            # custom macro runner
├── report.py            # markdown + JSON I/O, diff logic
├── conftest.py          # pytest fixture (JVM per scenario)
├── scenarios/
│   ├── __init__.py
│   ├── micro.py         # M1-M7 as pytest-benchmark tests
│   └── macro.py         # X1-X6 using runner.py
└── README.md            # usage

py4j-python/src/py4j/tests/perf/docs/
└── design.md   # this doc
```

Each module has one clear responsibility. No file is expected to exceed 300 LOC.

## 11. Dependencies

Added to `py4j-python/requirements-test.txt`:

```
pytest-benchmark>=4.0.0
psutil>=5.9.0
```

No new runtime dependencies. Both are stable, widely used, and well maintained.

## 12. CLI Surface

```
python -m py4j.tests.perf [OPTIONS]

Options:
  --save FILE              Write JSON to FILE, skip markdown report.
  --compare FILE           Run all scenarios, compare against FILE.
  --only IDS               Run only the listed scenarios (comma-sep, e.g. M1,M5,X2).
  --skip IDS               Skip the listed scenarios.
  --strict                 Turn environment warnings into hard failures.
  --fail-on-regression     Exit non-zero if any scenario regresses (comparison only).
  --jvm-heap SIZE          JVM -Xms/-Xmx value. Default: 4g.
  --output-dir DIR         Where to write perf_report.{md,json}. Default: cwd.
  --quick                  Reduce rounds by 4× for smoke-test runs (not for PRs).
  -h, --help
```

## 13. Migration — Existing `benchmark1.py`

`py4j-python/src/py4j/tests/benchmark1.py` is **not deleted** in this PR. A comment is added at the top pointing to the new tool:

```python
# DEPRECATED: this script is kept for historical reference.
# For reproducible, comparison-capable performance measurements, use:
#   python -m py4j.tests.perf --save baseline.json
#   python -m py4j.tests.perf --compare baseline.json
# See py4j/tests/perf/README.md for details.
```

A follow-up PR can delete it once the new framework has been validated against its scenarios.

## 14. Testing the Framework Itself

The framework is a test tool; it still needs its own tests. In `py4j-python/src/py4j/tests/test_perf_framework.py`:

- Unit tests for `report.py` (given two fixture JSONs, does the diff produce the expected markdown?).
- Unit tests for `environment.py` (mock `psutil` calls; verify warnings trigger).
- Unit tests for `runner.py` (does it call `gc.disable`/`gc.enable` the right number of times?).
- Smoke test: run `--only M1 --quick` end-to-end. Expects exit 0 and the presence of `perf_report.json`. This is the single integration test; it needs a JVM and is marked `@pytest.mark.slow` so it runs only opt-in.

Framework unit tests live in the normal test suite and run in CI. The smoke test does not run in CI (it would need the JVM-side gradle build, which the current `test.yml` workflow does run — so it's cheap to enable later if we want).

## 15. Success Criteria

The framework is done when:

1. A contributor can run `python -m py4j.tests.perf` on a fresh clone and get a report in under 6 minutes, assuming Java is installed.
2. `--save` / `--compare` produce a diff table that includes every scenario, median/p95 deltas, noise bands, and verdicts.
3. Running the same command twice on the same tree produces results where no scenario's median differs by more than its noise band (within-run reproducibility).
4. All 13 scenarios have at least one `measure()` call that executes without raising.
5. The framework's own unit tests pass in CI.
6. `README.md` documents: how to run, how to interpret the report, how to compare branches, what the verdicts mean, and which environment controls are user-responsibility (turbo, CPU pinning).

## 16. Open Questions / Future Work

- **X1 thread counts.** I've fixed {1, 4, 16} as reasonable defaults. A laptop with 4 cores may produce surprising results at 16 threads. Consider making this configurable or scaling to `os.cpu_count()` in a follow-up.
- **X2 collection sizes.** {1k, 10k, 100k} is a wide sweep. If 100k takes >60s on older hardware, drop to {1k, 10k, 50k}. Adjustable at scenario level without framework changes.
- **CI integration.** A `workflow_dispatch` GitHub Actions job that runs `--compare` against master's baseline is a natural follow-up. The JSON schema is stable enough to support it.
- **asv integration.** The JSON schema is designed to be convertible to asv's native format. If long-term tracking becomes valuable, we can add an exporter.
- **Memory measurement.** The framework measures time. Memory impact of optimizations (e.g., cache growth) is not covered. Could be added via `tracemalloc` snapshots in a future iteration; deliberately out of scope here.
