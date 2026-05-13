"""pytest fixtures for the perf framework's micro scenarios.

Only tests in ``scenarios/micro.py`` that request the ``gateway`` fixture
trigger a JVM spawn. Protocol-only tests (M5/M6/M7) skip it and run pure
Python-level timings in microseconds.

``scenarios/`` is excluded from normal ``pytest`` collection. The scenarios
there are intended to run only through the perf framework's harness
(``python -m py4j.tests.perf``) which invokes pytest-benchmark with
targeted paths and the right iteration counts. Letting the main test
suite auto-discover them would spawn 12 fresh JVMs on every CI matrix
cell and blow past the 20-minute timeout on slower platforms (saw this
as a timeout cancellation on Python 3.9 / Java 17 / macos-latest). The
harness's explicit ``pytest.main([scenarios/micro.py, ...])`` call
bypasses ``collect_ignore`` because the path is passed directly.
"""

import pytest

from py4j.tests.perf.jvm import (
    JvmNotBuiltError,
    JvmStartupError,
    fresh_jvm,
)


collect_ignore = ["scenarios"]


@pytest.fixture(scope="function")
def gateway():
    """Fresh JVM + JavaGateway, scoped per test function.

    ``pytest-benchmark`` runs the benchmarked callable many times (iterations
    x rounds) within a single test function invocation, so one fixture per
    function is the correct granularity: each scenario gets a fresh JVM, and
    all iterations within that scenario share it.
    """
    try:
        with fresh_jvm() as gw:
            yield gw
    except JvmNotBuiltError as e:
        pytest.skip(str(e))
    except JvmStartupError as e:
        pytest.skip(str(e))


class _StubGatewayClient:
    """Minimal stub sufficient for primitive-type OUTPUT_CONVERTER paths in
    ``get_return_value``. No real client is needed to benchmark parsing of
    primitive integer / float / string / boolean responses.
    """
    converters = None


@pytest.fixture(scope="session")
def stub_client():
    """Shared stub gateway_client for protocol-decode micro benchmarks."""
    return _StubGatewayClient()
