"""JVM lifecycle helpers for the perf framework.

Spawns a fresh ``py4j.examples.ExampleApplication`` per scenario so no state
bleeds between measurements. Uses ``subprocess.Popen`` (rather than the
``multiprocessing.Process`` wrapper used by the existing test suite) because
we need direct control over JVM flags and teardown semantics.

The classpath resolution and readiness-probe logic is adapted from
``py4j/tests/java_gateway_test.py`` (PY4J_JAVA_PATHS, check_connection,
safe_shutdown). Keeping our own copy avoids importing from the test module.
"""

import os
import subprocess
import time
from contextlib import contextmanager

from py4j.java_gateway import (
    CallbackServerParameters, GatewayParameters, JavaGateway)
from py4j.protocol import Py4JNetworkError


_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.realpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))
_JAVA_BUILD = os.path.join(_REPO_ROOT, "py4j-java")


PY4J_JAVA_PATHS = [
    os.path.join(_JAVA_BUILD, "build", "classes", "main"),
    os.path.join(_JAVA_BUILD, "build", "classes", "test"),
    os.path.join(_JAVA_BUILD, "build", "classes", "java", "main"),
    os.path.join(_JAVA_BUILD, "build", "classes", "java", "test"),
    os.path.join(_JAVA_BUILD, "build", "resources", "main"),
    os.path.join(_JAVA_BUILD, "build", "resources", "test"),
    os.path.join(_JAVA_BUILD, "target", "classes"),
    os.path.join(_JAVA_BUILD, "target", "test-classes"),
    os.path.join(_JAVA_BUILD, "bin"),
]
PY4J_JAVA_PATH = os.pathsep.join(PY4J_JAVA_PATHS)


class JvmNotBuiltError(RuntimeError):
    """Raised when no compiled Java classes can be found on PY4J_JAVA_PATH."""


class JvmStartupError(RuntimeError):
    """Raised when the JVM spawns but does not become ready in time."""


def verify_classpath():
    """Ensure at least one of the PY4J_JAVA_PATHS entries exists and looks built.

    We consider the classpath valid if any entry both exists and contains at
    least one ``.class`` file (top-level or in ``py4j/`` subdir).
    """
    for path in PY4J_JAVA_PATHS:
        if not os.path.isdir(path):
            continue
        for root, _dirs, files in os.walk(path):
            if any(f.endswith(".class") for f in files):
                return path
    raise JvmNotBuiltError(
        "No compiled Java classes found on the classpath. "
        "Build the Java side first:\n"
        "    cd py4j-java && ./gradlew classes testClasses\n"
        "Expected to find .class files under one of:\n  "
        + "\n  ".join(PY4J_JAVA_PATHS))


def check_connection(port=None, retries=1, retry_sleep=2.0):
    """Probe the JVM by attempting one trivial call.

    Adapted from java_gateway_test.check_connection, but with an explicit
    port parameter and configurable retry. Raises Py4JNetworkError after
    exhausting retries.
    """
    params = GatewayParameters(port=port) if port else None
    last_error = None
    for attempt in range(retries + 1):
        gw = JavaGateway(gateway_parameters=params)
        try:
            gw.jvm.System.currentTimeMillis()
            gw.close()
            return
        except Py4JNetworkError as e:
            last_error = e
            gw.close()
            if attempt < retries:
                time.sleep(retry_sleep)
    raise last_error


def safe_shutdown(gateway):
    """Best-effort gateway shutdown; swallow exceptions."""
    if gateway is None:
        return
    try:
        gateway.shutdown()
    except Exception:
        pass


def spawn_jvm(heap="4g", extra_flags=None, stdout=None, stderr=None):
    """Start ``ExampleApplication`` as a subprocess and return the handle.

    JVM flags applied:
        -Xms<heap> -Xmx<heap>     pinned heap, no resize noise during timing
        -XX:+AlwaysPreTouch       page-fault all heap pages at startup

    The caller is responsible for waiting until the JVM is accepting
    connections (use ``check_connection`` or ``fresh_jvm``).
    """
    verify_classpath()
    cmd = [
        "java",
        "-Xms{0}".format(heap), "-Xmx{0}".format(heap),
        "-XX:+AlwaysPreTouch",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.extend([
        "-cp", PY4J_JAVA_PATH,
        "py4j.examples.ExampleApplication",
    ])
    return subprocess.Popen(
        cmd,
        stdout=stdout if stdout is not None else subprocess.DEVNULL,
        stderr=stderr if stderr is not None else subprocess.DEVNULL,
    )


def shutdown_jvm(process, gateway=None, timeout=10):
    """Shut down gateway first, then terminate (and if needed, kill) the JVM.

    Order matters: shutting down the gateway lets the JVM exit cleanly on its
    own. If it doesn't exit within ``timeout``, we terminate. If that also
    fails, we kill. Never leaves a zombie.
    """
    safe_shutdown(gateway)
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


@contextmanager
def fresh_jvm(heap="4g", startup_sleep=0.25, readiness_retries=1,
              enable_callbacks=False):
    """Spawn a JVM, build a gateway, yield it, then tear both down.

    :param enable_callbacks: if True, start a CallbackServer alongside
        the gateway so Java can invoke Python proxies (needed for X4).

    Example:
        with fresh_jvm() as gateway:
            gateway.jvm.java.lang.System.currentTimeMillis()
    """
    process = spawn_jvm(heap=heap)
    # Brief sleep lets the OS reuse the listen port; mirrors the 250ms
    # default used across the existing test suite.
    time.sleep(startup_sleep)
    try:
        check_connection(retries=readiness_retries)
    except Py4JNetworkError:
        shutdown_jvm(process, None)
        raise JvmStartupError(
            "JVM spawned but did not accept connections within "
            "{0}s. Is port 25333 already in use?".format(
                startup_sleep + 2.0 * readiness_retries))
    if enable_callbacks:
        gateway = JavaGateway(
            callback_server_parameters=CallbackServerParameters())
    else:
        gateway = JavaGateway()
    try:
        yield gateway
    finally:
        shutdown_jvm(process, gateway)
