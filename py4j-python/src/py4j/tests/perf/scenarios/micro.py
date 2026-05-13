"""Micro scenarios (M1-M7) driven by pytest-benchmark.

Each ``test_*`` function maps to one scenario in the registry at
``py4j/tests/perf/scenarios/__init__.py``. Scenarios that request the
``gateway`` fixture cause a fresh JVM to be spawned for that function;
protocol-only scenarios do not.

JVM-bound micro scenarios (M1-M4) use ``benchmark.pedantic`` with an
explicit ``iterations`` count rather than the auto-calibrated
``benchmark(fn)``. With auto-calibration each timed round was a single
20-130 us call, putting OS scheduler jitter in direct competition with
the measurement (we saw 25-40% noise on macOS even with renice -15).
By batching N calls into one timed round we average jitter inside each
sample, dropping per-round variance into the low single digits.
Protocol-only scenarios (M5-M7) keep auto-calibration since each call
is sub-microsecond and pytest-benchmark already calibrates iterations
to a stable timer-resolution range.
"""

from py4j.protocol import (
    escape_new_line,
    get_command_part,
    get_return_value,
    unescape_new_line,
)


# Pedantic-mode tuning: pytest-benchmark calls the target function
# ``iterations`` times per timed round and divides the round duration
# by ``iterations`` to produce a per-call sample, then reports
# ``rounds`` such samples. Each per-round sample is therefore the
# per-call average of a batch of ``iterations`` calls - jitter that
# would dominate a single 22 us call gets averaged out across the
# batch, dropping per-round variance into the low single digits.

# ------------------------------------------------------------------- M1
def test_m1_static_call_no_args(benchmark, gateway):
    """Raw round-trip latency floor: no-arg static method call."""
    fn = gateway.jvm.java.lang.System.currentTimeMillis
    benchmark.pedantic(fn, rounds=50, iterations=1000, warmup_rounds=2)


# ------------------------------------------------------------------- M2
def test_m2a_instance_append_int(benchmark, gateway):
    """Instance method with a small int argument (append(int))."""
    sb = gateway.jvm.java.lang.StringBuilder()
    benchmark.pedantic(sb.append, args=(42,),
                       rounds=50, iterations=1000, warmup_rounds=2)


def test_m2b_instance_append_str(benchmark, gateway):
    """Instance method with a short string argument (append(String))."""
    sb = gateway.jvm.java.lang.StringBuilder()
    benchmark.pedantic(sb.append, args=("hello",),
                       rounds=50, iterations=1000, warmup_rounds=2)


# ------------------------------------------------------------------- M3
def test_m3_jvmview_class_resolution(benchmark, gateway):
    """Navigation + class resolution via ``jvm.java.lang.String``.

    Each call walks the JavaPackage chain and resolves the class. The
    JVMView has no resolution cache, so this exercises the reflection
    round-trip on every access.
    """
    def resolve():
        return gateway.jvm.java.lang.String
    benchmark.pedantic(resolve, rounds=50, iterations=500, warmup_rounds=2)


# ------------------------------------------------------------------- M4
def test_m4_constructor_and_finalize(benchmark, gateway):
    """Constructor + finalizer registration cost per object.

    Memory management is on by default; every ``new StringBuilder()``
    returns a JavaObject that registers a weakref finalizer.
    """
    def make():
        return gateway.jvm.java.lang.StringBuilder()
    benchmark.pedantic(make, rounds=50, iterations=500, warmup_rounds=2)


# ------------------------------------------------------------------- M5
def test_m5a_encode_int(benchmark):
    """Protocol-only: serialize a small int via ``get_command_part``."""
    benchmark(get_command_part, 42)


def test_m5b_encode_string(benchmark):
    """Protocol-only: serialize a short string via ``get_command_part``."""
    benchmark(get_command_part, "hello world")


def test_m5c_encode_float(benchmark):
    """Protocol-only: serialize a float via ``get_command_part``."""
    benchmark(get_command_part, 3.14159)


# ------------------------------------------------------------------- M6
def test_m6a_decode_int(benchmark, stub_client):
    """Protocol-only: parse an integer success response.

    The RETURN_MESSAGE '!' prefix is stripped before get_return_value
    is called in real use (see java_gateway.py:1257-1258), so the answer
    starts with the status char directly: 'y' + type 'i' + value.
    """
    answer = "yi42"
    benchmark(get_return_value, answer, stub_client)


def test_m6b_decode_string(benchmark, stub_client):
    """Protocol-only: parse a string success response."""
    answer = "yshello world"
    benchmark(get_return_value, answer, stub_client)


# ------------------------------------------------------------------- M7
_ESCAPE_SAMPLE = ("hello\nworld\r\\test with some unicode \u00e9\n" * 10)
_UNESCAPE_SAMPLE = escape_new_line(_ESCAPE_SAMPLE)


def test_m7a_escape(benchmark):
    """Protocol-only: escape_new_line on a realistic multi-line string."""
    benchmark(escape_new_line, _ESCAPE_SAMPLE)


def test_m7b_unescape(benchmark):
    """Protocol-only: unescape_new_line on a pre-escaped string."""
    benchmark(unescape_new_line, _UNESCAPE_SAMPLE)
