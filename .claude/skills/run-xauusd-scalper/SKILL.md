---
name: run-xauusd-scalper
description: Build, start, and drive the XAUUSD Scalper (MT5 bridge + Flask web dashboard). Use when asked to run, start, restart, or screenshot the dashboard, check bridge/engine status, run the backtest API, or run the test suite (./run.sh verify).
---

Trading bot: MT5 bridge (Wine) + Flask web dashboard at `dashboard.py`,
orchestrated by `./run.sh`. Drive the dashboard with the Playwright
driver at `.claude/skills/run-xauusd-scalper/driver.py` (no `chromium-cli`
in this container — this is the fallback driver built for that case).
All paths below are relative to the repo root.

**Read this before touching anything:** `./run.sh --status` may show a
LIVE engine already running against a real broker account (demo or
not — check `DRY_RUN` in `.env`). Never run `./run.sh --stop` or
`emergency-stop`, and never click "Detener motor" / "Pausar entradas"
in the driver, unless the user explicitly asked you to stop trading.
`verify` / `doctor` / read-only dashboard browsing are safe and don't
touch a running engine.

**Never run `./run.sh --start` if a session is already up** (check
`./run.sh --status` first). `run.sh` doesn't lock against concurrent
invocations — a second `--start` spawns a second bridge process that
fights the first one for port 5001 (infinite crash-restart loop in
`data/logs/run.log`) and a second dashboard process racing the first
over the same `data/run/*.pid` files. Hit this for real in this
session: it corrupted `data/run/engine.pid`, orphaning an already
LIVE engine from the dashboard's tracking (dashboard reported
"detenido" while the real process kept running/trading) — a stale
`engine.pid` at that point means the "Iniciar motor" button would
spawn a **second** engine managing the same account. Fix: confirm the
real supervisor PID (`ps aux | grep engine_supervisor`) and
`echo -n <pid> > data/run/engine.pid` — never kill-and-restart a
LIVE engine to "fix" this unless the user explicitly agrees, since
restarting briefly stops real trading. If you did start a redundant
`--start` by mistake, kill only the tree your own command spawned
(match PID/start-time from `ps -ef --forest`) — never a broad
`pkill -f run.sh` / `pkill -f dashboard.py`, which would just as
happily kill the legitimate session.

## Prerequisites

Already installed and verified working in this container: Python
venv (`.venv`), Wine + MetaTrader5 terminal, and Playwright's Chromium
(`python3 -c "import playwright"` succeeds outside the venv, at
`~/.local/lib/python3.13/site-packages`). For a fresh machine, the
project's own installer handles all of this — not re-verified this
session:

```bash
./install.sh          # Kali/Ubuntu: Python, Wine, MT5, Windows-side Python
```

If Playwright/Chromium isn't present for the driver:

```bash
pip install --user playwright && python3 -m playwright install chromium
```

## Run (agent path)

1. Check first — **don't start a second session on top of one already
   running** (see warning above):

```bash
./run.sh --status
```

If it reports bridge/dashboard already active, skip straight to step 2
and use the port it prints. Only run this if nothing is up yet (does
**not** start the trading engine):

```bash
./run.sh --start
```

2. **Always read the real port** — `dashboard.py` picks its own free
   port starting at 9000 and records the actual one; do not hardcode
   9000:

```bash
PORT=$(cat data/run/dashboard.port)
curl -s http://127.0.0.1:$PORT/api/status   # {"connected":true,"engine_running":...,"mode":"LIVE"|"DRY_RUN",...}
```

3. Drive the dashboard with the Playwright REPL driver (verified this
   session — real MT5 demo session, engine already live, $20.81
   equity):

```bash
python3 .claude/skills/run-xauusd-scalper/driver.py --session app <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
screenshot dashboard
click text=Backtesting MT5
wait-for text=Símbolo
screenshot backtest_tab
click text=Settings
wait-for text=Riesgo
screenshot settings_tab
console --errors
EOF
```

Screenshots land in
`.claude/skills/run-xauusd-scalper/sessions/<session>/screenshots/`,
numbered, with `screenshot.png` symlinked to the latest.

| driver.py command | what it does |
|---|---|
| `nav <url>` | goto, waits for `load` |
| `wait-for text=<substr>` | poll up to 10s for page text |
| `wait-for <css>` | poll up to 10s for a visible selector |
| `screenshot [name]` | full-page PNG |
| `click <css>` / `click text=<substr>` | click |
| `fill <css> <text>` | fill an input |
| `press <key>` | keyboard press |
| `eval <js>` / `get-text <css>` | run JS / read innerText |
| `console --errors` | print console/page errors seen so far |

4. Run the full check (compile + pytest + synthetic-engine smoke +
   in-process dashboard API check — none of this touches a live
   engine or the real port, `main.py --synthetic` and Flask's
   `test_client()` are both isolated):

```bash
./run.sh verify
```

Verified this session: `220 passed, 1 skipped in ~91s`, synthetic
engine starts cleanly, all dashboard routes return 200.

5. Full diagnostic (read-only):

```bash
./run.sh doctor
```

## Run (human path)

```bash
./run.sh --start   # → prints the dashboard URL; open it in a browser
./run.sh --status  # → formatted bridge/dashboard/engine/account status
./run.sh --stop    # → stops EVERYTHING incl. a running engine — see warning above
```

## Test

```bash
./run.sh verify
```

220 passed, 1 skipped (the skipped one needs a historical CSV not in
the repo).

## Gotchas

- **Dashboard port isn't fixed.** It restarts on 9000 and moves up if
  taken; a stale port file or a hardcoded 9000/9001 in a script will
  silently hit the wrong (or no) server. Always read
  `data/run/dashboard.port` fresh right before use — it changed from
  9001 to 9002 mid-session here with no action on my part (supervisor
  auto-restart), confirming the "always read it fresh" rule in `run.sh`'s
  own comments is not paranoia.
- **`chromium-cli` isn't installed in this container** — that's why
  `driver.py` exists as a from-scratch Playwright REPL instead. If a
  future container has `chromium-cli`, prefer it and drop `driver.py`.
- **Spanish UI labels.** `wait-for text=Symbol` times out — the field
  is `Símbolo`. Same for other labels; check the actual rendered text,
  not the English guess.
- **The engine is a separate concern from the dashboard/bridge.**
  `./run.sh --start` never starts it — it's started/stopped only from
  the dashboard UI (`/api/engine/start|stop`) or by a user explicitly
  asking for it. It may already be running (LIVE, real orders) when
  you arrive — `--status`/`doctor` show this before you do anything else.
- **`./run.sh verify`'s smoke test is isolated** (`main.py --synthetic`,
  Flask `test_client()`) — safe to run even with a live engine on the
  real port, it never touches `data/trades.db` writes from the live
  engine or the real dashboard port.

## Troubleshooting

- **`wait-for` times out on `text=Symbol`**: wrong language — the
  backtest tab label is `Símbolo`, not `Symbol`. Use the exact
  on-screen text.
- **Bridge shows `activo (local)` but dashboard `detenido/inalcanzable`
  right after `--start`**: dashboard takes a couple seconds after the
  supervisor reports "activo" — poll `data/run/dashboard.port` +
  `/api/status` rather than assuming it's up immediately.
