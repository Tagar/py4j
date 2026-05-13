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

### Reference baselines

Committed JSON snapshots of full-suite runs live under `baselines/`.
Use one as the baseline for `--compare` when capturing a fresh
on-master baseline isn't practical (e.g. on a contributor's laptop
where idling the machine is expensive). Match the hardware tag in the
filename to your own machine for the cleanest comparison; cross-
hardware comparisons get an environment-mismatch warning. See
[`baselines/README.md`](baselines/README.md) for naming, capture, and
refresh policy.

## Command reference

| Flag | Purpose |
|---|---|
| `--only ID,...` | Run only the listed scenario IDs |
| `--skip ID,...` | Exclude the listed scenario IDs |
| `--quick` | Reduced rounds for smoke runs (not for PRs) |
| `--n-runs N` | Run the whole framework N times in fresh JVMs and pool per-round data. Captures inter-JVM variance. Default 1. |
| `--target-ci-width PCT` | Adaptive sampling: keep adding rounds (batches of 5) until the bootstrap CI half-width on the median drops below `PCT` %. Stops early on clean scenarios. Macro only. |
| `--max-rounds N` | Override macro runner's default `max_rounds` cap (50). |
| `--scenario-time-budget S` | Override per-scenario wall-clock cap (60 s). |
| `--strict` | Environment warnings become hard failures. |
| `--strict-bench` | Run additional Linux noise-floor checks (Turbo, governor, SMT). Combine with `--strict` to fail when any noise source is present. See "strict-bench mode" below. |
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
  the 60-second / 30-round time budget triggered before the target.

### Comparison verdicts

Each scenario in a `--compare` diff is labelled:

- **faster** — Mann-Whitney `p < 0.01`, bootstrap 95 % CI excludes
  zero, AND the Hodges-Lehmann delta ≤ −5 %.
- **regression** — same gates with delta ≥ +5 %.
- **inconclusive** — at least one of the three gates failed.
  Either the CI straddles zero, the Mann-Whitney p is too high, or
  the effect is smaller than the 5 % material-change threshold.
  See the **P(better) / P(same) / P(worse)** ROPE columns to read
  which side the evidence still leans.
- **neutral** — statistically significant but smaller than 5 %.

When `rounds[]` is absent on a baseline (older `--save-compact`),
the framework falls back to a `|Δ median| > 2 × noise` heuristic.

### Reading the ROPE columns

The bootstrap distribution of pairwise medians is partitioned into
three regions of practical equivalence:

- **P(better)** — fraction of resamples where Δ < −5 %
- **P(same)**   — fraction where −5 % ≤ Δ ≤ +5 %
- **P(worse)**  — fraction where Δ > +5 %

These are direct probabilities and complement the Mann-Whitney
p-value (which only tells you *whether* the distributions differ,
not the *direction* or *magnitude*). On an inconclusive verdict,
look at the ROPE columns: if `P(better) = 0.82` and `P(worse) =
0.01`, the evidence leans clearly toward improvement even if it
didn't clear the 5 % gate — running more rounds (`--n-runs 3` or
`--target-ci-width 2.0`) would probably get you a confident
verdict.

### Distribution-shape comparison

The report's **Distribution-shape comparison** table surfaces tail
metrics (p99, p99.9, tail-ratio) and the Kolmogorov-Smirnov
two-sample p-value. The median-based verdict above stays primary;
this table catches regressions where the median is unchanged but
the tail is growing (a perf change that doubles p99 latency without
moving the median is invisible to Mann-Whitney + median tests but
lights up KS).

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

## For maximum rigor — strict-bench mode

`--strict-bench` runs additional Linux noise-floor checks on top of
the default battery / load-avg / thermal guards. Combine with
`--strict` to fail the run when any of these guards trips.

```bash
python -m py4j.tests.perf --strict-bench --strict
```

The additional checks:

| Check | Why it matters |
|---|---|
| Turbo Boost / boost off | Frequency boosting can pulse 30-40 % higher then thermally throttle in 100s of ms — the largest single CV contributor. |
| CPU governor = `performance` | Other governors ramp the clock on sustained load; short benchmark rounds finish before ramp-up and run cold. |
| SMT siblings online (informational) | Two threads share execution resources; if you can, pin to one physical core. |

### Recommended Linux preamble

For the tightest measurements, apply the OS-level mitigations
*before* invoking the framework. The framework doesn't run these
itself — they want sudo and persist beyond a single run, both of
which are user decisions.

```bash
# 1. Disable Turbo Boost (Intel) or core boosting (AMD).
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo  # Intel
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost          # AMD/acpi_cpufreq

# 2. Pin all CPUs to performance governor.
sudo cpupower frequency-set -g performance

# 3. Take core 2 offline's SMT sibling (find via thread_siblings_list).
echo 0 | sudo tee /sys/devices/system/cpu/cpu<sibling>/online

# 4. Run the framework pinned to core 2 with real-time scheduling.
taskset -c 2 sudo chrt -f 50 python -m py4j.tests.perf
```

Result on commodity Linux hardware: per-scenario CV drops from
8-15 % to 1-3 %; most "inconclusive" verdicts on real 5-10 % effects
become "faster" or "regression" with high confidence.

### macOS

There is no equivalent to `taskset` / `chrt` / `no_turbo` on macOS.
The default guards still help (battery, load, `pmset -g therm`).
For maximum stability:

- Plug in the AC adapter.
- Quit Chrome / Docker / Slack / anything in Activity Monitor's
  CPU view above 5 %.
- Let the laptop sit for 60 seconds after closing apps so the
  fans spin down and the system thermal settles.
- Let the framework's auto-`sudo renice -n -15` fire.

### Composing the v2 confidence levers

The Stage-1 v2 changes work together. Suggested combinations:

| Goal | Flags |
|---|---|
| Quick smoke test | `--quick --only M1` |
| Default PR run (~6 min) | (no flags) |
| Squeeze every bit of confidence | `--n-runs 3 --target-ci-width 3 --strict-bench --strict` |
| Long-running daily baseline | `--save baseline.json --n-runs 5` |

## Other ways to reduce inconclusive verdicts

- **Plug the laptop in.**
- **Close other CPU-intensive apps** (Chrome, Docker, Slack).
- **Let the auto-renice fire** (don't pass `--no-renice`).
- **Increase `--n-runs`** — 3 captures inter-JVM variance, 5 is
  defensive against unlucky scheduler runs.
- **Use `--target-ci-width`** — adaptive sampling stops early on
  clean scenarios and runs longer on noisy ones, more efficient
  than just bumping `--max-rounds`.

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
- Up to 30 measured rounds OR 60 seconds of total measurement time,
  whichever comes first (with a minimum of 3 rounds so expensive
  scenarios like X2-100k always produce *some* data). The JSON
  records `budget_triggered: true` when the time cap hit before 30
  rounds, so a comparison never silently treats a 3-round run as
  equivalent to a 30-round one. (Bumped from 10/30s in v1 — more
  samples give the verdict layer enough power to call modest 5-10 %
  effects "faster" / "regression" instead of "inconclusive".)
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
