"""Tests for dashboard.py's engine process-control routes (POST
/api/engine/start, POST /api/engine/stop) and the SIGCHLD reaper backing
_engine_pid()'s "is it actually still alive" check.

These spawn a REAL child process (subprocess.Popen is patched to launch
`sleep 100` instead of the actual `python3 -m core.engine_supervisor`,
which needs a live .env/bridge) rather than mocking os.kill/os.waitpid -
the bug this round found (a zombie process that kill(pid, 0) keeps
reporting as "alive" forever because nobody called waitpid on it) only
shows up with real OS process semantics, so a mock would test the wrong
thing.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import replace

import pytest

import dashboard as dmod
from core.database import Database


def make_client(tmp_path, **settings_overrides):
    db = Database(str(tmp_path / "dash_engine_test.db"))
    dmod.db = db
    # dmod.settings is a module-level object other test files also mutate
    # via replace() (e.g. test_dashboard_api.py's token tests) - default
    # dashboard_auth_token back to blank here so this file's tests aren't
    # order-dependent on what ran before them in the same session.
    overrides = {"dashboard_auth_token": ""}
    overrides.update(settings_overrides)
    dmod.settings = replace(dmod.settings, **overrides)
    dmod.RUN_DIR = tmp_path / "run"
    dmod.ENGINE_PID_FILE = dmod.RUN_DIR / "engine.pid"
    return dmod.app.test_client(), db


# dmod.subprocess IS the stdlib subprocess module (not a copy) - patching
# dmod.subprocess.Popen therefore patches subprocess.Popen everywhere,
# including inside this very function, so the REAL Popen has to be
# captured up front or calling it here would recurse into the fake.
_real_popen = subprocess.Popen


def _fake_popen_sleep(*args, **kwargs):
    # Stand-in for `python3 -m core.engine_supervisor` - a real, short-lived
    # child so os.kill/os.waitpid exercise real OS semantics, without
    # pulling in the actual engine's heavier imports or bridge-wait loop.
    return _real_popen(["sleep", "100"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)


def _kill_and_reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def test_engine_start_spawns_and_tracks_a_real_process(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path)
    monkeypatch.setattr(dmod.subprocess, "Popen", _fake_popen_sleep)

    resp = client.post("/api/engine/start")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "engine_running": True}
    assert dmod.ENGINE_PID_FILE.exists()

    pid = int(dmod.ENGINE_PID_FILE.read_text().strip())
    os.kill(pid, 0)  # still alive - no exception
    assert client.get("/api/status").get_json()["engine_running"] is True

    _kill_and_reap(pid)


def test_engine_start_rejects_a_second_start_while_one_is_running(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path)
    monkeypatch.setattr(dmod.subprocess, "Popen", _fake_popen_sleep)

    client.post("/api/engine/start")
    resp = client.post("/api/engine/start")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False

    _kill_and_reap(int(dmod.ENGINE_PID_FILE.read_text().strip()))


def test_engine_stop_sends_sigterm_and_status_settles_after_reap(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path)
    monkeypatch.setattr(dmod.subprocess, "Popen", _fake_popen_sleep)

    client.post("/api/engine/start")

    resp = client.post("/api/engine/stop")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True, "stopping": True,
        "message": "Señal de parada enviada - puede tardar unos segundos en cerrar del todo.",
    }

    time.sleep(0.3)  # let SIGTERM's default action actually terminate `sleep`

    # _engine_pid() self-heals: reaps the (by now zombie, since nothing
    # else touched it yet) child via waitpid(WNOHANG) before checking
    # kill(pid, 0) - this is exactly the path that was broken before this
    # round's fix, where kill(pid, 0) kept reporting an unreaped zombie as
    # "alive" forever.
    assert client.get("/api/status").get_json()["engine_running"] is False
    assert not dmod.ENGINE_PID_FILE.exists()


def test_engine_stop_when_nothing_running_is_409(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/engine/stop")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_engine_running_false_when_pidfile_missing(tmp_path):
    client, _db = make_client(tmp_path)
    assert client.get("/api/status").get_json()["engine_running"] is False


def test_engine_start_requires_token_when_configured(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path, dashboard_auth_token="secret123")
    monkeypatch.setattr(dmod.subprocess, "Popen", _fake_popen_sleep)
    resp = client.post("/api/engine/start")
    assert resp.status_code == 401


def test_engine_stop_requires_token_when_configured(tmp_path):
    client, _db = make_client(tmp_path, dashboard_auth_token="secret123")
    resp = client.post("/api/engine/stop", headers={"X-Dashboard-Token": "secret123"})
    # nothing running -> 409, but importantly not 401 (the token was accepted)
    assert resp.status_code == 409


def test_reap_children_reaps_an_exited_child_directly(tmp_path):
    """Unit test of the SIGCHLD handler itself (not wired through Flask -
    signal.signal() only takes effect via main(), which app.test_client()
    never runs): spawns a real child, lets it exit at the OS level without
    Python reaping it (Popen.wait()/.poll() would reap it themselves, so
    neither is called here), and confirms _reap_children() actually calls
    waitpid on it instead of leaving it a zombie for _engine_pid() to find
    later."""
    proc = subprocess.Popen(["true"])
    time.sleep(0.3)  # let it exit at the OS level; still unreaped (zombie) so far

    dmod._reap_children(dmod.signal.SIGCHLD, None)

    # Already reaped by _reap_children - a second waitpid must now raise
    # ChildProcessError (errno ECHILD) instead of finding it again.
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    proc.returncode = 0  # satisfy Popen's own bookkeeping - it was reaped externally, not via proc.wait()
