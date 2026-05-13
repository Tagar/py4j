"""Custom runner for macro scenarios.

Macro scenarios don't fit pytest-benchmark's model cleanly: some spawn
multiple threads, some operate on a large fixture built once per JVM,
all produce aggregate-throughput measurements. This runner spins each
scenario's ``measure()`` method in a controlled loop with warm-up,
gc-disabled timing, and a 60-second / 30-round budget cap so expensive
scenarios (X2 at 100k) don't block for minutes on slow machines.

Defaults bumped from 10/30s to 30/60s in v2 to give Mann-Whitney and the
bootstrap CI enough samples to reach a confident verdict on real-but-
modest (5-10%) effects without depending solely on --target-ci-width
adaptive sampling.
"""

import gc
import math
import random
import time

# Adaptive-sampling internals. Imported lazily to avoid circular import
# (report.py also imports from this package indirectly).
_ADAPTIVE_BOOTSTRAP_RESAMPLES = 500
_ADAPTIVE_BOOTSTRAP_SEED = 42


def _single_sample_median_ci(rounds, ci=0.95):
    """Bootstrap CI of the median of one sample.

    Used by adaptive sampling to decide when "enough" rounds have been
    collected: stop adding rounds once the CI half-width drops below
    the user-specified relative target.

    Cheap by design — 500 resamples in pure Python is ~10 ms on a few
    dozen rounds, so we can re-evaluate after every batch.
    """
    n = len(rounds)
    if n < 2:
        return None, None
    rng = random.Random(_ADAPTIVE_BOOTSTRAP_SEED)
    medians = []
    for _ in range(_ADAPTIVE_BOOTSTRAP_RESAMPLES):
        sample = sorted(rounds[rng.randrange(n)] for _ in range(n))
        m = sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2.0
        medians.append(m)
    medians.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = int(alpha * len(medians))
    hi_idx = max(lo_idx, int((1.0 - alpha) * len(medians)) - 1)
    return medians[lo_idx], medians[hi_idx]


def _adaptive_should_stop(rounds, target_ci_width):
    """Return True if the median CI half-width is below the target.

    ``target_ci_width`` is expressed as a fraction (e.g. 0.03 = ±3 %),
    i.e. the half-width of the bootstrap CI divided by the median.
    """
    if target_ci_width is None or target_ci_width <= 0:
        return False
    lo, hi = _single_sample_median_ci(rounds)
    if lo is None or hi is None:
        return False
    n = len(rounds)
    sorted_rounds = sorted(rounds)
    median = (sorted_rounds[n // 2] if n % 2
              else (sorted_rounds[n // 2 - 1] + sorted_rounds[n // 2]) / 2.0)
    if median <= 0:
        return False
    half_width = (hi - lo) / 2.0
    return (half_width / median) <= target_ci_width


_TARGET_ROUND_DURATION_S = 0.1  # 100 ms: comfortably above scheduler jitter
_MAX_AUTO_REPEATS = 10000  # safety cap so a near-no-op measure() doesn't
                            # try to repeat itself a million times


def _estimate_rounds_needed(observed_cv, target_ci_width):
    """Rough rounds-needed estimate for a target CI half-width.

    The bootstrap CI half-width on the median is approximately
    1.96 * CV / sqrt(n) for normally-distributed-ish data with
    moderate n. Solving for n:

        n ≈ (1.96 * CV / target_ci_width)^2

    Returns an integer >= 3 (the framework's minimum) and capped at
    1000 (anything more probably indicates the scenario itself is
    unstable, not a sample-size problem).
    """
    if observed_cv <= 0 or target_ci_width <= 0:
        return 3
    z = 1.96  # 95 % CI on a normal approximation
    n = (z * observed_cv / target_ci_width) ** 2
    return max(3, min(1000, int(n) + 1))


def power_analysis_warmup(scenario, gateway, n_warmup_rounds=3,
                          auto_scale_repeats_flag=True):
    """Run a short warmup, return (per_round_s, observed_cv, repeats).

    Used by ``--analyze-only`` and by the up-front ETA report to predict
    how long the full measurement phase will take and how many rounds
    each scenario likely needs to reach a target CI width.

    The returned numbers are noisy (only 3 rounds!) — they're an
    estimate, not a measurement. The caller should treat the ETA as
    "within 2x" rather than precise.
    """
    if hasattr(scenario, "setup"):
        scenario.setup(gateway)
    if auto_scale_repeats_flag:
        repeats = _auto_scale_repeats(scenario, gateway, n_warmup_rounds)
    else:
        repeats = max(1, getattr(scenario, "repeats_per_round", 1))

    timings = []
    for _ in range(n_warmup_rounds):
        t0 = time.perf_counter()
        for _ in range(repeats):
            scenario.measure(gateway)
        timings.append(time.perf_counter() - t0)

    if len(timings) < 2:
        return timings[0] if timings else 0.0, 0.0, repeats
    mean_t = sum(timings) / len(timings)
    if mean_t <= 0:
        return mean_t, 0.0, repeats
    var = sum((t - mean_t) ** 2 for t in timings) / (len(timings) - 1)
    stddev = math.sqrt(var) if var > 0 else 0.0
    cv = stddev / mean_t
    return mean_t, cv, repeats


def _auto_scale_repeats(scenario, gateway, warmup_rounds):
    """Auto-scale ``repeats_per_round`` so each timed round is >= 100 ms.

    Returns the chosen repeat count.

    Why: OS scheduler-tick + ``perf_counter`` resolution is in the 1-10 ms
    range on commodity hardware. A round shorter than ~100 ms has a
    relative noise floor of 1-10 % just from those, regardless of the
    underlying code. Scaling the repeat count so each round is at least
    100 ms drops that contribution below 1 %, leaving the inherent
    scenario variance as the dominant signal.

    Per-scenario, computed once at warmup time. Skipped if the scenario
    already declares ``repeats_per_round > 1`` (assume the author tuned
    it) or if ``measure()`` is already >= 100 ms (X2-100k etc.).
    """
    declared = max(1, getattr(scenario, "repeats_per_round", 1))
    if declared > 1:
        # Author tuned this explicitly; respect it.
        return declared
    if warmup_rounds < 1:
        return declared

    # Time a single warmup call to estimate measure() cost.
    t0 = time.perf_counter()
    scenario.measure(gateway)
    one_call_s = time.perf_counter() - t0

    if one_call_s <= 0 or one_call_s >= _TARGET_ROUND_DURATION_S:
        return declared

    needed = int(_TARGET_ROUND_DURATION_S / one_call_s) + 1
    # Cap at _MAX_AUTO_REPEATS so we never spend forever on a no-op
    # scenario (~50 ns × 10000 = 0.5 ms, still under target, but we
    # bail rather than escalate further).
    return min(needed, _MAX_AUTO_REPEATS)


def run_macro(scenario, gateway,
              warmup_rounds=3,
              max_rounds=50,
              min_rounds=3,
              max_seconds=60.0,
              target_ci_width=None,
              adaptive_batch_size=5,
              auto_scale_repeats=True,
              sprt_callback=None):
    """Run one macro scenario and return timing results.

    :param scenario: an instance of a class extending ``MacroScenario``
        (see scenarios/macro.py). Must implement ``measure(gateway)``
        and may implement ``setup(gateway)``.
    :param gateway: a fresh ``JavaGateway`` connected to an isolated JVM.

    The budget cap stops the round loop once both conditions hold:
    (1) we've completed at least ``min_rounds`` rounds, AND
    (2) total measured time >= ``max_seconds``. So we always get some
    data even on pathological scenarios, but we don't run forever.

    Adaptive sampling (``target_ci_width`` set to a fraction like 0.03
    for ±3 %) re-evaluates after each batch of ``adaptive_batch_size``
    rounds: if the bootstrap CI half-width on the median falls below
    the target (relative to the median), the loop stops early. Cheap
    scenarios converge in 10-15 rounds; noisy ones run to ``max_rounds``
    or the time budget — whichever comes first. ``target_ci_width=None``
    keeps fixed-N behavior.

    Returns a dict:
        {
            "rounds": list of per-round durations (seconds),
            "warmup_rounds": int,
            "budget_triggered": bool,
            "adaptive_stopped_early": bool,
            "iterations_per_round": int (scenario-reported),
        }
    """
    if hasattr(scenario, "setup"):
        scenario.setup(gateway)

    if auto_scale_repeats:
        # Burns one warmup call inside _auto_scale_repeats; remaining
        # warmup rounds compensate for that.
        repeats = _auto_scale_repeats(scenario, gateway, warmup_rounds)
    else:
        repeats = max(1, getattr(scenario, "repeats_per_round", 1))

    for _ in range(warmup_rounds):
        for _ in range(repeats):
            scenario.measure(gateway)

    rounds = []
    cpu_rounds = []      # process_time() per round, same shape as rounds[]
    errors = 0           # measure() calls that raised
    budget_triggered = False
    adaptive_stopped_early = False
    sprt_decision = None
    elapsed = 0.0

    for i in range(max_rounds):
        if i >= min_rounds and elapsed >= max_seconds:
            budget_triggered = True
            break

        gc.disable()
        cpu0 = time.process_time()
        t0 = time.perf_counter()
        for _ in range(repeats):
            try:
                scenario.measure(gateway)
            except Exception:
                # Count the failure but keep the round going; an
                # exploded measurement here still produces a usable
                # round duration for the remaining iterations.
                errors += 1
        dt = time.perf_counter() - t0
        cpu_dt = time.process_time() - cpu0
        gc.enable()
        gc.collect()

        rounds.append(dt)
        cpu_rounds.append(cpu_dt)
        elapsed += dt

        # Adaptive stopping: only after we hit min_rounds and only at
        # batch boundaries (re-bootstrapping after every round wastes
        # cycles for no resolution gain).
        if (target_ci_width is not None
                and len(rounds) >= min_rounds
                and len(rounds) % adaptive_batch_size == 0
                and _adaptive_should_stop(rounds, target_ci_width)):
            adaptive_stopped_early = True
            break

        # SPRT (sequential probability ratio): when --compare is in
        # scope and the user enabled SPRT stopping, check after each
        # batch whether the bootstrap CI vs baseline rounds has fully
        # cleared the practical-equivalence band or fully entered it.
        # Either decision stops the loop early; an undecided result
        # means keep collecting.
        if (sprt_callback is not None
                and len(rounds) >= min_rounds
                and len(rounds) % adaptive_batch_size == 0):
            decision = sprt_callback(rounds)
            if decision and decision != "undecided":
                sprt_decision = decision
                break

    base_iters = getattr(scenario, "iterations_per_round", 1)
    iters = base_iters * repeats
    return {
        "rounds": rounds,
        "cpu_rounds": cpu_rounds,
        "errors": errors,
        "warmup_rounds": warmup_rounds,
        "budget_triggered": budget_triggered,
        "adaptive_stopped_early": adaptive_stopped_early,
        "sprt_decision": sprt_decision,
        "iterations_per_round": iters,
    }


class MacroScenario(object):
    """Base class for macro scenarios.

    Attributes subclasses should override:
        id         : scenario ID used for filtering and reporting
        name       : short human-readable name
        enable_callbacks : True if the JVM gateway needs a callback server
        iterations_per_round : the count of logical ops per measure() call
                               (e.g., X1 with 10,000 calls distributed ->
                               iterations_per_round = 10_000). Used by
                               consumers to derive throughput.
        repeats_per_round    : how many times to call measure() inside a
                               single timed round. Defaults to 1. Bump
                               this on scenarios where one measure() call
                               is short enough that scheduler jitter
                               dominates the per-round timing - e.g. X4
                               at 22 ms had 47% noise; with
                               repeats_per_round=10 each timed round is
                               ~220 ms and noise drops below 5%. The
                               total iterations_per_round reported is
                               (base iterations_per_round) x repeats.
                               (Note: the auto-scaler in run_macro will
                               compute this for you if it's left at 1
                               and measure() takes < 100 ms — explicit
                               values are respected when set > 1.)
        expected_cv : float, the noise budget for this scenario. After
                      measurement, if the observed CV (stddev / mean
                      of per-round timings) exceeds 2 * expected_cv,
                      the report layer emits a "noisy beyond budget"
                      warning. This catches both (a) scenarios whose
                      behavior is inherently unstable and (b) cases
                      where the runner environment is too noisy for
                      the verdict to be trustworthy. Default 0.10
                      (10 % CV is a reasonable ceiling for most macro
                      benchmarks under modest load).

    Methods:
        setup(gateway)   : optional, called once per fresh JVM.
        measure(gateway) : required, called per round. Return value ignored.
    """
    id = None
    name = None
    enable_callbacks = False
    iterations_per_round = 1
    repeats_per_round = 1
    expected_cv = 0.10
    # Optional: per-iteration data transfer size. Set on scenarios
    # that move bytes (large-collection iteration, byte-array round-
    # trips); the framework derives bandwidth (bytes/sec) from it.
    # Leave as None when not applicable.
    bytes_per_iteration = None

    def measure(self, gateway):
        raise NotImplementedError("Subclass must implement measure()")
