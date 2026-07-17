"""Tests for core/engine_supervisor.py's restart-with-backoff and clean-
stop-on-SIGTERM behavior - the Python reimplementation of what run.sh's
old bash supervise() used to do, now needed because the engine is spawned
by dashboard.py (see POST /api/engine/start), not by run.sh.

These point ROOT at a tmp_path containing a small stand-in main.py rather
than running the real one (which needs a live .env/bridge connection), so
_run_once spawns a real, short-lived subprocess and the tests exercise
actual process/signal semantics instead of mocked ones.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import core.engine_supervisor as sup


def _write_fake_main(tmp_path: Path, body: str) -> None:
    (tmp_path / "main.py").write_text(body)


def test_run_once_reports_the_actual_elapsed_time(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    _write_fake_main(tmp_path, "import time\ntime.sleep(0.3)\n")

    with open(tmp_path / "log.txt", "w") as log_file:
        elapsed = sup._run_once(log_file)

    assert elapsed >= 0.3


def test_run_once_terminates_the_child_promptly_on_stop_request(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    _write_fake_main(tmp_path, "import time\ntime.sleep(30)\n")

    sup._stop_requested = False

    def request_stop_shortly():
        time.sleep(0.7)  # let _run_once's poll loop actually be waiting
        sup._stop_requested = True

    threading.Thread(target=request_stop_shortly).start()

    start = time.time()
    with open(tmp_path / "log.txt", "w") as log_file:
        elapsed = sup._run_once(log_file)
    wall = time.time() - start

    try:
        # The child was sleeping for 30s - a stop request must terminate it
        # promptly (SIGTERM, then a bounded 15s wait before SIGKILL), not
        # let it run to completion.
        assert wall < 5
        assert elapsed < 5
    finally:
        sup._stop_requested = False  # don't leak into other tests


def test_main_stops_immediately_without_restarting_if_already_stop_requested(tmp_path, monkeypatch):
    """If the stop signal arrives before main() ever runs a child (e.g. a
    start immediately followed by a stop), the supervisor must exit clean
    without spawning main.py at all - not restart-loop forever."""
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    monkeypatch.setattr(sup, "LOG_PATH", tmp_path / "engine.stdout.log")
    marker = tmp_path / "ran.txt"
    _write_fake_main(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

    sup._stop_requested = True
    try:
        sup.main()
    finally:
        sup._stop_requested = False

    assert not marker.exists()  # main.py must never have been launched at all
    assert "iniciado" in sup.LOG_PATH.read_text()


def test_run_once_passes_its_own_pid_via_env_for_the_kill_switch_to_use(tmp_path, monkeypatch):
    """Regression test for the fix wiring main.py's EngineHalted (kill
    switch) handler to this supervisor: a child that reads
    ENGINE_SUPERVISOR_PID from its environment and sends that PID SIGTERM
    - exactly what main.py now does when EMERGENCY_STOP trips - must make
    main() stop cleanly without restarting, the same as an explicit
    SIGTERM to the supervisor itself. Before this fix, main.py instead
    touched a file (data/run/stop.flag) that this supervisor never even
    looked at, so the kill switch would just get silently respawned."""
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    monkeypatch.setattr(sup, "LOG_PATH", tmp_path / "engine.stdout.log")
    marker = tmp_path / "run_count.txt"
    _write_fake_main(tmp_path, f"""
import os
import signal
from pathlib import Path
marker = Path({str(marker)!r})
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count + 1))
os.kill(int(os.environ["ENGINE_SUPERVISOR_PID"]), signal.SIGTERM)
""")

    sup._stop_requested = False
    try:
        sup.main()
    finally:
        sup._stop_requested = False

    assert marker.read_text().strip() == "1"  # never restarted


def test_main_gives_up_after_five_consecutive_fast_failures(tmp_path, monkeypatch):
    """A main.py that dies instantly every time (missing pandas on a
    partial Termux install, bad config, syntax error) must NOT be retried
    forever - before this, the supervisor crash-looped it eternally with
    the dashboard reporting 'Motor corriendo' the whole time. It must try
    exactly 5 times, log why it's giving up, and exit on its own."""
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    monkeypatch.setattr(sup, "LOG_PATH", tmp_path / "engine.stdout.log")
    marker = tmp_path / "run_count.txt"
    _write_fake_main(tmp_path, f"""
from pathlib import Path
marker = Path({str(marker)!r})
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count + 1))
raise SystemExit(1)
""")

    sup._stop_requested = False
    try:
        sup.main()  # must terminate by itself - a hang here IS the failure
    finally:
        sup._stop_requested = False

    assert marker.read_text().strip() == "5"
    log_text = sup.LOG_PATH.read_text()
    assert "rindiendome" in log_text


def test_main_restarts_a_child_that_exits_on_its_own(tmp_path, monkeypatch):
    """A crash (non-stop exit) must trigger a restart, not a permanent
    stop - this is the actual crash-resilience this module exists for."""
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    monkeypatch.setattr(sup, "LOG_PATH", tmp_path / "engine.stdout.log")
    marker = tmp_path / "run_count.txt"
    _write_fake_main(tmp_path, f"""
from pathlib import Path
marker = Path({str(marker)!r})
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count + 1))
if count == 0:
    raise SystemExit(1)  # first run "crashes"
""")

    def stop_after_second_run():
        while not marker.exists() or marker.read_text().strip() != "2":
            time.sleep(0.05)
        sup._stop_requested = True

    threading.Thread(target=stop_after_second_run, daemon=True).start()

    sup._stop_requested = False
    try:
        sup.main()  # backoff starts at 2s but the watcher thread stops it right after run 2
    finally:
        sup._stop_requested = False

    assert marker.read_text().strip() == "2"
    log_text = sup.LOG_PATH.read_text()
    assert "reintentando" in log_text
