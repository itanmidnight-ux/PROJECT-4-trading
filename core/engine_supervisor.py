"""
Restart-with-backoff supervisor for main.py (the trading engine), run as
its own detached process. This exists because the engine is no longer
started by run.sh - per the dashboard-controlled workflow, `./run.sh
--start` only brings up the bridge and the dashboard; the engine itself is
started and stopped FROM the dashboard (POST /api/engine/start|stop, see
dashboard.py), so it can be turned on only when the operator is ready and
watching, and turned off without having to touch a terminal.

Losing run.sh's own supervise() (restart-on-crash with exponential
backoff) as a side effect of that move would be a real regression - a
crashed engine would just stay dead until someone noticed and reopened
the dashboard. This module reimplements that exact policy in Python
instead of bash, since it's spawned by dashboard.py (a Python process),
not by run.sh:

  - main.py is restarted automatically if it exits on its own (crash,
    unhandled exception outside main.py's own retry loop) - not if
    EngineHalted was the reason (the kill switch), main.py's own exit code
    for that case is handled the same way run.sh's supervise() always
    treated it: as "this process ended", restarted anyway UNLESS this
    supervisor itself was asked to stop (see below). A kill switch halt is
    about STOPPING TRADING, not about whether the supervisor should keep
    trying to run the process - if you want the whole thing off, use
    POST /api/engine/stop (SIGTERM to this process), which is different
    from the kill switch.
  - A run that survives >= 60s resets the backoff, so a generally healthy
    process that flaps occasionally isn't punished with an ever-growing
    wait after one transient failure.
  - SIGTERM/SIGINT to THIS process (dashboard.py sends SIGTERM to stop the
    engine) stops the current child cleanly and exits without restarting -
    this is the intentional "turn it off" path, distinct from the child
    exiting on its own.

Usage: python3 -m core.engine_supervisor
Writes its own stdout/stderr-merged log to data/logs/engine.stdout.log
(the same file run.sh's old supervise() used, so nothing else needs to
change to keep reading engine logs from the same place).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "logs" / "engine.stdout.log"

_stop_requested = False


def _handle_stop_signal(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    global _stop_requested
    _stop_requested = True


def _run_once(log_file) -> float:
    """Runs main.py once to completion (or until a stop is requested), and
    returns how long it ran for in seconds."""
    start = time.time()
    # ENGINE_SUPERVISOR_PID lets main.py tell THIS process, specifically,
    # to stop restarting it when the kill switch (EMERGENCY_STOP) halts
    # the engine - see main.py's EngineHalted handler. Without it main.py
    # would have no safe way to signal "stop, don't respawn me" (sending
    # SIGTERM to os.getppid() unconditionally would be wrong whenever
    # main.py is run directly, e.g. a manual debug session or
    # ./run.sh verify's --synthetic smoke test, where the parent is an
    # unrelated shell/process that has no business receiving it).
    env = dict(os.environ, ENGINE_SUPERVISOR_PID=str(os.getpid()))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    while proc.poll() is None:
        if _stop_requested:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        time.sleep(0.5)
    return time.time() - start


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    backoff = 2.0
    with open(LOG_PATH, "a") as log_file:
        log_file.write(f"\n[engine_supervisor] iniciado (PID {os.getpid()})\n")
        log_file.flush()
        while not _stop_requested:
            ran_for = _run_once(log_file)
            if _stop_requested:
                log_file.write("[engine_supervisor] detenido por pedido explicito (stop), no se reinicia.\n")
                log_file.flush()
                break
            if ran_for >= 60:
                backoff = 2.0
            log_file.write(f"[engine_supervisor] main.py termino tras {ran_for:.0f}s, reintentando en {backoff:.0f}s...\n")
            log_file.flush()
            slept = 0.0
            while slept < backoff and not _stop_requested:
                time.sleep(0.5)
                slept += 0.5
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
