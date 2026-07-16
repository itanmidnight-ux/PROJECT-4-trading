"""SQLite storage for trades + account snapshots, and the aggregation
queries the dashboard reads from."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    lot REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    sl_price REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pnl_usd REAL,
    close_fraction REAL,
    tp_level INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    dry_run INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
-- pnl_daily/pnl_monthly aggregate WHERE closed_at IS NOT NULL, GROUP BY a
-- substring of closed_at - this index is what keeps that from becoming a
-- full table scan as trade history grows over months of live operation.
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    free_margin REAL NOT NULL
);
-- account_snapshots gets a row every engine poll (every poll_seconds, so a
-- few tens of thousands of rows/day) with no natural cap - unlike trades,
-- which only grow with real activity. Without this index, equity_curve(),
-- the latest-snapshot lookup, AND the hourly retention prune (see
-- prune_old_snapshots) all degrade to a full table scan as this table
-- grows, right when a live deployment has been running longest.
CREATE INDEX IF NOT EXISTS idx_account_snapshots_ts ON account_snapshots(ts);

CREATE TABLE IF NOT EXISTS engine_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

-- Simple key/value store for settings edited from the dashboard's Settings
-- tab (broker login/password/server, risk parameters). Read at engine
-- startup as an override layer on top of .env (see
-- core/config.py::apply_db_overrides) - deliberately NOT hot-reloaded by a
-- running engine process, since swapping broker credentials mid-session
-- while a position might be open is a correctness/safety risk, not just an
-- engineering convenience. A value here only takes effect the next time the
-- engine starts.
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class TradeRecord:
    id: int
    ticket: str
    symbol: str
    side: str
    lot: float
    entry_price: float
    exit_price: Optional[float]
    sl_price: Optional[float]
    opened_at: str
    closed_at: Optional[str]
    pnl_usd: Optional[float]
    close_fraction: Optional[float]
    tp_level: Optional[int]
    status: str
    dry_run: bool


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # WAL + a busy timeout let the engine (writer) and the dashboard
        # (reader, polling every few seconds) hit the same file concurrently
        # without "database is locked" errors, even at high trade volume.
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # WAL's own documentation recommends NORMAL over the default FULL:
        # under WAL, NORMAL still guarantees no corruption on a crash/power
        # loss, it only risks losing the most recent commit(s) that hadn't
        # been checkpointed yet - an acceptable trade for account_snapshots
        # writing every poll_seconds, where fsync-per-commit is otherwise
        # the dominant cost of every engine step.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --------------------------------------------------------------- write
    def open_trade(self, *, ticket: str, symbol: str, side: str, lot: float,
                    entry_price: float, sl_price: float, opened_at: str,
                    dry_run: bool) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (ticket, symbol, side, lot, entry_price, sl_price, opened_at, status, dry_run)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                (ticket, symbol, side, lot, entry_price, sl_price, opened_at, int(dry_run)),
            )
            return cur.lastrowid

    def close_trade_partial(self, trade_id: int, *, exit_price: float, closed_at: str,
                             pnl_usd: float, close_fraction: float, tp_level: int,
                             fully_closed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE trades SET exit_price = ?, closed_at = ?, pnl_usd = COALESCE(pnl_usd, 0) + ?,
                   close_fraction = COALESCE(close_fraction, 0) + ?, tp_level = ?,
                   status = ? WHERE id = ?""",
                (exit_price, closed_at, pnl_usd, close_fraction, tp_level,
                 'closed' if fully_closed else 'partial', trade_id),
            )

    def record_snapshot(self, *, ts: str, balance: float, equity: float, free_margin: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO account_snapshots (ts, balance, equity, free_margin) VALUES (?, ?, ?, ?)",
                (ts, balance, equity, free_margin),
            )

    def log_event(self, *, ts: str, level: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO engine_events (ts, level, message) VALUES (?, ?, ?)",
                (ts, level, message),
            )

    def set_settings(self, values: dict[str, str]) -> None:
        """Upserts multiple key/value pairs in one transaction - used by the
        dashboard's Settings tab save button so a partial write (e.g. a crash
        mid-save) can't leave some keys updated and others stale."""
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                list(values.items()),
            )

    def get_all_settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def prune_old_snapshots(self, keep_days: int = 30) -> int:
        """account_snapshots gets a row every engine poll (as often as every
        0.25s) with no natural cap, unlike trades/events which only grow
        with real activity - left alone this table grows unbounded on a
        long-running deployment (millions of rows/month at the default poll
        rate). ts is an ISO8601 UTC string, so a lexicographic comparison
        against another ISO8601 cutoff is a correct chronological
        comparison without needing to parse every row. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM account_snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ---------------------------------------------------------------- read
    def recent_trades(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM engine_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def pnl_by_day(self, days: int = 30) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT substr(closed_at, 1, 10) AS day,
                          SUM(pnl_usd) AS pnl,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins
                   FROM trades
                   WHERE closed_at IS NOT NULL
                   GROUP BY day ORDER BY day DESC LIMIT ?""",
                (days,),
            ).fetchall()

    def pnl_by_month(self, months: int = 12) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT substr(closed_at, 1, 7) AS month,
                          SUM(pnl_usd) AS pnl,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins
                   FROM trades
                   WHERE closed_at IS NOT NULL
                   GROUP BY month ORDER BY month DESC LIMIT ?""",
                (months,),
            ).fetchall()

    def summary(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total_trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(pnl_usd), 0) AS total_pnl
                   FROM trades WHERE closed_at IS NOT NULL"""
            ).fetchone()
            latest = conn.execute(
                "SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return {
                "total_trades": row["total_trades"] or 0,
                "wins": row["wins"] or 0,
                "losses": row["losses"] or 0,
                "total_pnl": row["total_pnl"] or 0.0,
                "balance": latest["balance"] if latest else None,
                "equity": latest["equity"] if latest else None,
                "free_margin": latest["free_margin"] if latest else None,
            }

    def equity_curve(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT ts, balance, equity FROM account_snapshots ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()[::-1]
