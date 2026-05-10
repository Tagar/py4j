"""Custom runner for macro scenarios.

Macro scenarios don't fit pytest-benchmark's model cleanly: some spawn
multiple threads, some operate on a large fixture built once per JVM,
all produce aggregate-throughput measurements. This runner spins each
scenario's ``measure()`` method in a controlled loop with warm-up,
gc-disabled timing, and a 30-second / 10-round budget cap so expensive
scenarios (X2 at 100k) don't block for minutes on slow machines.
"""

import gc
import time


def run_macro(scenario, gateway,
              warmup_rounds=3,
              max_rounds=10,
              min_rounds=3,
              max_seconds=30.0):
    """Run one macro scenario and return timing results.

    :param scenario: an instance of a class extending ``MacroScenario``
        (see scenarios/macro.py). Must implement ``measure(gateway)``
        and may implement ``setup(gateway)``.
    :param gateway: a fresh ``JavaGateway`` connected to an isolated JVM.

    The budget cap stops the round loop once both conditions hold:
    (1) we've completed at least ``min_rounds`` rounds, AND
    (2) total measured time >= ``max_seconds``. So we always get some
    data even on pathological scenarios, but we don't run forever.

    Returns a dict:
        {
            "rounds": list of per-round durations (seconds),
            "warmup_rounds": int,
            "budget_triggered": bool,
            "iterations_per_round": int (scenario-reported),
        }
    """
    if hasattr(scenario, "setup"):
        scenario.setup(gateway)

    repeats = max(1, getattr(scenario, "repeats_per_round", 1))

    for _ in range(warmup_rounds):
        for _ in range(repeats):
            scenario.measure(gateway)

    rounds = []
    budget_triggered = False
    elapsed = 0.0

    for i in range(max_rounds):
        if i >= min_rounds and elapsed >= max_seconds:
            budget_triggered = True
            break

        gc.disable()
        t0 = time.perf_counter()
        for _ in range(repeats):
            scenario.measure(gateway)
        dt = time.perf_counter() - t0
        gc.enable()
        gc.collect()

        rounds.append(dt)
        elapsed += dt

    base_iters = getattr(scenario, "iterations_per_round", 1)
    iters = base_iters * repeats
    return {
        "rounds": rounds,
        "warmup_rounds": warmup_rounds,
        "budget_triggered": budget_triggered,
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

    Methods:
        setup(gateway)   : optional, called once per fresh JVM.
        measure(gateway) : required, called per round. Return value ignored.
    """
    id = None
    name = None
    enable_callbacks = False
    iterations_per_round = 1
    repeats_per_round = 1

    def measure(self, gateway):
        raise NotImplementedError("Subclass must implement measure()")
