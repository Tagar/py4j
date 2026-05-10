# py4j performance testing framework

A local, reproducible benchmark suite for py4j. Runs a mix of micro
(per-call latency) and macro (aggregate throughput, concurrency,
large collections) scenarios against a freshly spawned JVM, and
produces pasteable markdown + machine-diffable JSON reports.

Built to make performance claims on py4j PRs defensible:

- one command to capture a baseline on `master`
- one command to compare your branch against it
- a diff table with verdicts (faster / regression / inconclusive)
  and noise bands

## Prerequisites

- Java 8 or later (Temurin 21 or Zulu 21 recommended, matching CI's
  distribution).
- Python 3.9+ (the wider py4j test suite runs 3.9 – 3.13 in CI).
- py4j's Java side compiled. From the repo root:

  ```bash
  cd py4j-java
  ./gradlew classes testClasses
  ```

  The perf framework looks for `.class` files under
  `py4j-java/build/classes/{main,test}` and related paths. It fails
  with a clear error if nothing is built.

- Python test dependencies installed:

  ```bash
  cd py4j-python
  pip install -e . -r requirements-test.txt
  ```

  This pulls in `pytest-benchmark` and `psutil` in addition to the
  existing test deps.

## Quick start

```bash
# Everything, default settings (~5 minutes on commodity hardware):
python -m py4j.tests.perf

# Smoke test (spawn JVM, one round-trip, tear down; no measurement):
python -m py4j.tests.perf smoke

# Environment snapshot (no JVM, no scenarios):
python -m py4j.tests.perf env

# Single scenario, reduced rounds for a fast iteration loop:
python -m py4j.tests.perf --only M1 --quick
```

### Before/after comparison

```bash
# On master: save baseline.
git checkout master
python -m py4j.tests.perf --save baseline.json

# On your branch: compare.
git checkout my-optimization
python -m py4j.tests.perf --compare baseline.json
```

Outputs `perf_report.md` and `perf_report.json` in the current
directory (or `--output-dir`). The markdown is designed to paste
directly into a PR description.

## Command reference

| Flag | Purpose |
|---|---|
| `--only ID,...` | Run only the listed scenario IDs |
| `--skip ID,...` | Exclude the listed scenario IDs |
| `--quick` | Reduced rounds for smoke runs (not for PRs) |
| `--strict` | Environment warnings become hard failures |
| `--save FILE` | Write JSON only to FILE (skip markdown) |
| `--save-compact` | With `--save`, drop per-round arrays (tiny JSON, suitable for committing) |
| `--compare FILE` | Run all scenarios, diff against the baseline at FILE |
| `--fail-on-regression` | Exit non-zero if `--compare` sees any regression |
| `--jvm-heap SIZE` | JVM `-Xms`/`-Xmx` value (default `4g`) |
| `--output-dir DIR` | Where to write `perf_report.{md,json}` |
| `--no-renice` | Skip the default `sudo renice -n -15 <pid>` at startup |
| `--renice-to N` | Target nice value for renice (default `-15`) |

Subcommands (positional, optional):

| Subcommand | Purpose |
|---|---|
| `run` | Run scenarios and emit report (this is the default) |
| `env` | Print captured environment metadata + guard warnings |
| `smoke` | Spawn a JVM, do one round-trip, tear down |

## Scenario catalogue

### Micro (`pytest-benchmark`)

Per-call latency, steady-state after warm-up. Protocol-only scenarios
(M5–M7) run without a JVM and complete in seconds.

| ID | What | Why |
|---|---|---|
| M1 | `System.currentTimeMillis()` | Raw round-trip latency floor |
| M2a | `StringBuilder.append(int)` | Small-arg marshaling, hot path |
| M2b | `StringBuilder.append(str)` | Same, string variant |
| M3 | `jvm.java.lang.String` resolution | Dispatch / caching |
| M4 | `new StringBuilder()` + GC | Finalizer lock cost per object |
| M5a/b/c | `get_command_part(int/str/float)` | Serialization, isolated |
| M6a/b | `get_return_value(int/str)` | Response parsing, isolated |
| M7a | `escape_new_line` | String escape hot path |
| M7b | `unescape_new_line` | String unescape hot path |

### Macro (custom runner)

Aggregate throughput. Scenario IDs with variants reveal scaling curves.

| ID | What | Why |
|---|---|---|
| X1-1 / X1-4 / X1-16 | 10 000 calls across N threads | Connection-pool scaling |
| X2-1k / X2-10k / X2-100k | Iterate a JavaList of N elements | O(N) round-trip pattern |
| X3-100 / X3-1k / X3-10k | `ListConverter.convert(python_list)` | Per-element `add()` churn |
| X4 | `Collections.sort` on Python-`Comparable` proxies | Callback round-trip |
| X5 | 1 000 × `ArrayList.get(-1)` → `Py4JJavaError` | Error-path latency |
| X6 | 50 concurrent threads, 20 calls each | Pool saturation tail latency |

## How to read the report

Every report begins with an environment header:

- OS / CPU / RAM / Python / Java versions
- py4j git revision and branch
- Warnings from the environment guards

The per-scenario table shows:

- **Median**, **p95** — what you want to quote.
- **Stddev** — spread; large compared to median means noisy.
- **Noise** = (p95 − p5) / median within a single run. If Noise > 30 %
  you probably have a thermal / load / battery issue and should
  re-run on a quieter machine.
- **Rounds** — how many measured rounds; `(budget)` suffix means
  the 30-second / 10-round time budget triggered before the target.

### Comparison verdicts

Each scenario in a `--compare` diff is labelled:

- **faster** — Δ median ≤ −5 % AND |Δ median| > 2 × noise.
- **regression** — Δ median ≥ +5 % AND |Δ median| > 2 × noise.
- **inconclusive** — |Δ median| ≤ 2 × noise. The delta is within
  the noise band; re-run on a quieter machine or raise the iteration
  count before claiming an improvement.
- **neutral** — small Δ, outside the noise band.

The noise band uses the *worse* of the two runs to stay conservative.

## Environment guards

Printed to stderr at startup; `--strict` turns warnings into errors.

- Battery power (laptop CPUs often throttle when unplugged).
- 1-minute load average > 0.5 × core count.
- macOS thermal throttling (`pmset -g therm`).

## Process priority (auto `sudo renice`)

On Darwin / Linux, the framework runs `sudo renice -n -15 <pid>` at
startup by default before any scenario begins. Child processes (the
per-scenario JVMs we spawn) inherit the elevated priority, so the
kernel scheduler prefers them over most other userspace work and
scheduler jitter drops noticeably on busy machines.

- **First run** prompts for the sudo password. Subsequent runs in
  the same terminal reuse the cached credential (typically 5 min on
  macOS; configurable per system).
- **Failures are non-fatal.** If sudo isn't installed, the user
  cancels the prompt, or the OS doesn't support it, we warn on
  stderr and continue at the original nice value. The report
  records what happened under `environment.renice` so two
  comparisons can check whether both runs had the same treatment.
- Pass **`--no-renice`** to skip the sudo call entirely — useful for
  CI, or when you've already elevated priority for the whole shell
  (`sudo renice -n -15 $$` then run normally).
- Pass **`--renice-to N`** to target a different nice value (e.g.
  `--renice-to -10` for a milder boost).

Real-time scheduling (`thread_policy_set(TIME_CONSTRAINT)`) on macOS
is deliberately NOT used — it requires Obj-C bindings and can hang
the system if misused. `-15` is the strongest safe priority via the
conventional `nice` interface.

## For maximum rigor

- Plug the laptop in.
- Close other CPU-intensive apps (Chrome, Docker, Slack).
- Let the auto-renice fire (don't pass `--no-renice`).
- Temporarily disable turbo boost (Linux: cpufreq governor; macOS:
  not automatable here, user's choice).
- Optional: pin the process to specific cores (`taskset` on Linux,
  not available on macOS).

## Methodology

### Fresh JVM per scenario

Every scenario gets a brand-new JVM subprocess. No state or GC pressure
bleeds between scenarios. The cost (~1.5 s per scenario for spawn) is
small compared to measurement time.

JVM flags applied to every spawn:

- `-Xms4g -Xmx4g` — pinned heap, no growth noise during timing.
- `-XX:+AlwaysPreTouch` — page-fault all heap pages at startup so
  first-access latency is out of the measurement window.
- Default GC collector — we want to measure the collector users
  actually see.

### Sampling

Micro (`pytest-benchmark`):

- 5 warm-up rounds (not timed).
- 30–100 measured rounds, auto-calibrated iterations per round so
  each round is ≥ 100 µs (reduces timer-resolution noise).
- Python GC disabled during measurement.

Macro (custom runner):

- 3 warm-up rounds.
- Up to 10 measured rounds OR 30 seconds of total measurement time,
  whichever comes first (with a minimum of 3 rounds so expensive
  scenarios like X2-100k always produce *some* data). The JSON
  records `budget_triggered: true` when the time cap hit before 10
  rounds, so a comparison never silently treats a 3-round run as
  equivalent to a 10-round one.
- `gc.disable()` + `gc.collect()` around each measured round.

## Schema

Reports are JSON at schema version 1.0:

```json
{
  "version": "1.0",
  "environment": { "os": "...", "cpu": "...", ... },
  "warnings": ["..."],
  "scenarios": [
    {
      "id": "M1",
      "name": "static_call_no_args",
      "runner": "pytest-benchmark" | "macro",
      "unit": "seconds",
      "warmup_rounds": 5,
      "measured_rounds": 50,
      "iterations_per_round": 128,
      "budget_triggered": false,
      "rounds": [ ... individual round timings ... ],
      "stats": {
        "min", "max", "mean", "median", "stddev", "iqr",
        "p5", "p95", "p99"
      }
    }
  ]
}
```

## Troubleshooting

**"No compiled Java classes found on the classpath."**
Build the Java side: `cd py4j-java && ./gradlew classes testClasses`.

**"JVM spawned but did not accept connections within Xs. Is port
25333 already in use?"**
Stop any other py4j JVM (PySpark worker, another `./gradlew run`, a
previous crashed perf run). Find it with `lsof -i :25333`.

**Java build fails on JDK > 11 with "Source option 6 is no longer
supported."** `py4j-java/build.gradle` sets
`sourceCompatibility = "1.6"` and `targetCompatibility = "1.6"`.
Build with JDK 8 or 11, or temporarily bump those to 1.8 for a
local build (this is outside the scope of this framework).

**Enterprise network blocks Maven Central.**
Configure Gradle to route through your organization's Maven
proxy via an init script in `~/.gradle/init.d/` — the perf framework
itself has no network dependencies at run time (only the one-time
Java build does).

## Design doc

For the full design rationale (why hybrid runner, why fresh JVM per
scenario, what's explicitly out of scope), see
`py4j-python/src/py4j/tests/perf/docs/design.md`.
