"""Tests for the web-only dashboard entry point."""
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import dashboard as dmod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = sys.executable


def _free_port() -> int:
    """Grabs a port that's free right now (bind to 0, let the kernel pick,
    close immediately). Used to pick test ports instead of hardcoding
    numbers that might already be in use by a stray dashboard.py from
    earlier manual runs in this same worktree."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise AssertionError(f"nada escuchando en 127.0.0.1:{port} tras {timeout}s")


def _wait_until_dead(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Waits for (and reaps) a child process we hold a Popen handle for.

    Uses Popen.wait() instead of polling /proc/<pid> because a signalled
    process lingers as a zombie - still present under /proc - until
    something actually reaps it. Popen.wait() both waits for death and
    reaps the zombie in one call, so this can't be fooled by a lingering
    zombie into thinking the process is still alive.
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"PID {proc.pid} sigue vivo tras {timeout}s")


def test_no_flags_starts_web_dashboard(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py"])
    with patch.object(dmod, "_run_web") as web:
        dmod.main()
    assert web.call_args.args == ("127.0.0.1", 9000)


def test_web_flag_kept_as_compatibility_alias(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--web"])
    with patch.object(dmod, "_run_web") as web:
        dmod.main()
    assert web.call_args.args == ("127.0.0.1", 9000)


def test_explicit_host_and_port(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--host", "0.0.0.0", "--port", "9500"])
    with patch.object(dmod, "_run_web") as web:
        dmod.main()
    assert web.call_args.args == ("0.0.0.0", 9500)


def test_native_option_is_removed(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--native"])
    try:
        dmod.main()
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("--native debe rechazarse: el dashboard es sólo web")


# ---------------------------------------------------------------------------
# _reclaim_stale_dashboard_port
# ---------------------------------------------------------------------------
# These are deliberately real, not mocked: they spawn genuine subprocesses
# and bind genuine sockets, because the whole point of this function is to
# identify a REAL other OS process by inspecting its REAL /proc/<pid>/cmdline
# and to REALLY free a REAL bound port. A mock that just asserts
# "os.kill was called with the right args" would not prove any of that.

def test_reclaim_is_a_noop_when_port_is_free():
    """(a) Nothing listening on the port: reclaim must not raise, must not
    touch anything, and _find_free_port must still return that exact port
    (unchanged behaviour from before this function existed)."""
    port = _free_port()
    dmod._reclaim_stale_dashboard_port("127.0.0.1", port)  # no-op, must not raise
    assert dmod._find_free_port("127.0.0.1", port) == port


def test_reclaim_kills_a_real_dashboard_py_process_and_frees_the_port():
    """(b) A REAL dashboard.py subprocess is actually listening on the
    port. Calling the reclaim function must terminate that real process
    (verified via /proc) and free the port for a fresh bind - not just
    call a mocked os.kill."""
    port = _free_port()
    proc = subprocess.Popen(
        [PYTHON_BIN, str(PROJECT_ROOT / "dashboard.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_listening(port)  # confirm it's genuinely bound before we test reclaiming it

        dmod._reclaim_stale_dashboard_port("127.0.0.1", port)

        _wait_until_dead(proc)  # the real process must actually be gone (and reaped)

        # Port must now be genuinely free - a real bind proves it, not a
        # mock returning what we told it to return.
        assert dmod._find_free_port("127.0.0.1", port) == port
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib_suppress_timeout():
                proc.wait(timeout=5)


def test_reclaim_leaves_non_dashboard_process_alone():
    """(c) Something else entirely (python3 -m http.server, no
    'dashboard.py' anywhere in its cmdline) is on the port: reclaim must
    leave it running untouched, and _find_free_port must fall through to
    its normal auto-increment past it - exactly as before this function
    existed."""
    port = _free_port()
    proc = subprocess.Popen(
        [PYTHON_BIN, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_listening(port)

        dmod._reclaim_stale_dashboard_port("127.0.0.1", port)

        # Give it a beat - if reclaim wrongly signalled it, it would be
        # dead or dying by now. It must still be alive and still listening.
        time.sleep(0.3)
        assert proc.poll() is None, "reclaim mató un proceso que NO es dashboard.py"
        _wait_until_listening(port, timeout=1.0)

        # And the pre-existing auto-increment fallback must still kick in.
        free = dmod._find_free_port("127.0.0.1", port)
        assert free != port
        assert free > port
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib_suppress_timeout():
                proc.wait(timeout=5)


class contextlib_suppress_timeout:
    """Tiny local helper so cleanup in `finally` blocks above never masks
    the real assertion failure with an unrelated TimeoutExpired from a
    slow-to-die child during teardown."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is subprocess.TimeoutExpired
