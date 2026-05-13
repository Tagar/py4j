"""Scenario registry.

Each scenario carries an ID (used for ``--only`` / ``--skip`` filtering),
a short human-readable name, a runner type, and a handle that points to
the implementing test function (pytest-benchmark) or module (macro).
"""

from collections import namedtuple


Scenario = namedtuple(
    "Scenario",
    ["id", "name", "runner", "handle", "needs_jvm", "description"],
)


# Micro scenarios (pytest-benchmark). The handle is the pytest test-function
# name. _pytest_micro.py translates IDs to pytest -k filters.
MICRO_SCENARIOS = [
    Scenario("M1", "static_call_no_args", "pytest-benchmark",
             "test_m1_static_call_no_args", True,
             "Raw round-trip latency: System.currentTimeMillis()"),
    Scenario("M2a", "instance_append_int", "pytest-benchmark",
             "test_m2a_instance_append_int", True,
             "Instance method with small int arg"),
    Scenario("M2b", "instance_append_str", "pytest-benchmark",
             "test_m2b_instance_append_str", True,
             "Instance method with small string arg"),
    Scenario("M3", "jvmview_class_resolution", "pytest-benchmark",
             "test_m3_jvmview_class_resolution", True,
             "JavaPackage navigation + JavaClass resolution"),
    Scenario("M4", "constructor_and_finalize", "pytest-benchmark",
             "test_m4_constructor_and_finalize", True,
             "Constructor + finalizer registration per object"),
    Scenario("M5a", "encode_int", "pytest-benchmark",
             "test_m5a_encode_int", False,
             "Protocol-only: get_command_part(int)"),
    Scenario("M5b", "encode_string", "pytest-benchmark",
             "test_m5b_encode_string", False,
             "Protocol-only: get_command_part(str)"),
    Scenario("M5c", "encode_float", "pytest-benchmark",
             "test_m5c_encode_float", False,
             "Protocol-only: get_command_part(float)"),
    Scenario("M6a", "decode_int", "pytest-benchmark",
             "test_m6a_decode_int", False,
             "Protocol-only: get_return_value(int response)"),
    Scenario("M6b", "decode_string", "pytest-benchmark",
             "test_m6b_decode_string", False,
             "Protocol-only: get_return_value(string response)"),
    Scenario("M7a", "escape_newlines", "pytest-benchmark",
             "test_m7a_escape", False,
             "Protocol-only: escape_new_line()"),
    Scenario("M7b", "unescape_newlines", "pytest-benchmark",
             "test_m7b_unescape", False,
             "Protocol-only: unescape_new_line()"),
]

# Macro scenarios: one registry entry per class, ``handle`` is the class.
from py4j.tests.perf.scenarios.macro import ALL_MACRO_CLASSES

MACRO_SCENARIOS = [
    Scenario(
        id=cls.id,
        name=cls.name,
        runner="macro",
        handle=cls,
        needs_jvm=True,
        description=cls.__doc__ or cls.__name__,
    )
    for cls in ALL_MACRO_CLASSES
]


ALL_SCENARIOS = MICRO_SCENARIOS + MACRO_SCENARIOS


def filter_scenarios(scenarios, only=None, skip=None):
    """Apply --only / --skip filters. Both accept a set of scenario IDs."""
    result = list(scenarios)
    if only:
        only_set = set(only)
        result = [s for s in result if s.id in only_set]
    if skip:
        skip_set = set(skip)
        result = [s for s in result if s.id not in skip_set]
    return result
