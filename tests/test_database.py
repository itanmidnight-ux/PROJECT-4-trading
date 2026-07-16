from datetime import datetime, timedelta, timezone

from core.database import Database


def make_db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_open_and_fully_close_trade(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    tid = db.open_trade(ticket="T1", symbol="XAUUSD", side="BUY", lot=0.02,
                         entry_price=2400.0, sl_price=2399.0, opened_at=now, dry_run=True)
    db.close_trade_partial(tid, exit_price=2400.5, closed_at=now, pnl_usd=0.5,
                            close_fraction=1.0, tp_level=1, fully_closed=True)
    trades = db.recent_trades(limit=10)
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["pnl_usd"] == 0.5


def test_partial_closes_accumulate_pnl_and_fraction(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    tid = db.open_trade(ticket="T2", symbol="XAUUSD", side="BUY", lot=0.03,
                         entry_price=2400.0, sl_price=2399.0, opened_at=now, dry_run=True)
    db.close_trade_partial(tid, exit_price=2400.3, closed_at=now, pnl_usd=0.3,
                            close_fraction=0.5, tp_level=1, fully_closed=False)
    db.close_trade_partial(tid, exit_price=2400.6, closed_at=now, pnl_usd=0.4,
                            close_fraction=0.5, tp_level=2, fully_closed=True)
    trades = db.recent_trades(limit=10)
    assert len(trades) == 1
    assert abs(trades[0]["pnl_usd"] - 0.7) < 1e-9
    assert abs(trades[0]["close_fraction"] - 1.0) < 1e-9
    assert trades[0]["status"] == "closed"


def test_pnl_by_day_aggregates_correctly(tmp_path):
    db = make_db(tmp_path)
    day1 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    day0 = datetime.now(timezone.utc).isoformat()
    for i, (ts, pnl) in enumerate([(day1, 0.3), (day1, -0.9), (day0, 0.5)]):
        tid = db.open_trade(ticket=f"D{i}", symbol="XAUUSD", side="BUY", lot=0.01,
                             entry_price=2400.0, sl_price=2399.0, opened_at=ts, dry_run=True)
        db.close_trade_partial(tid, exit_price=2400.1, closed_at=ts, pnl_usd=pnl,
                                close_fraction=1.0, tp_level=1, fully_closed=True)
    rows = db.pnl_by_day(days=30)
    by_day = {r["day"]: r for r in rows}
    assert len(by_day) == 2
    day1_key = day1[:10]
    assert abs(by_day[day1_key]["pnl"] - (0.3 - 0.9)) < 1e-9
    assert by_day[day1_key]["trades"] == 2
    assert by_day[day1_key]["wins"] == 1


def test_summary_counts_wins_and_losses(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    for i, pnl in enumerate([0.3, -0.5, 0.4, -0.2]):
        tid = db.open_trade(ticket=f"S{i}", symbol="XAUUSD", side="BUY", lot=0.01,
                             entry_price=2400.0, sl_price=2399.0, opened_at=now, dry_run=True)
        db.close_trade_partial(tid, exit_price=2400.1, closed_at=now, pnl_usd=pnl,
                                close_fraction=1.0, tp_level=1, fully_closed=True)
    summary = db.summary()
    assert summary["total_trades"] == 4
    assert summary["wins"] == 2
    assert summary["losses"] == 2
    assert abs(summary["total_pnl"] - 0.0) < 1e-9


def test_open_trades_excluded_from_closed_aggregates(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    db.open_trade(ticket="OPEN1", symbol="XAUUSD", side="BUY", lot=0.01,
                   entry_price=2400.0, sl_price=2399.0, opened_at=now, dry_run=True)
    summary = db.summary()
    assert summary["total_trades"] == 0  # still open, closed_at IS NULL


def test_snapshots_and_events_roundtrip(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    db.record_snapshot(ts=now, balance=50.0, equity=50.2, free_margin=48.0)
    db.log_event(ts=now, level="WARN", message="margen insuficiente")
    curve = db.equity_curve(limit=5)
    events = db.recent_events(limit=5)
    assert len(curve) == 1
    assert curve[0]["equity"] == 50.2
    assert len(events) == 1
    assert events[0]["level"] == "WARN"


def test_prune_old_snapshots_deletes_only_rows_past_retention(tmp_path):
    """account_snapshots gets a row every engine poll (unlike trades/events,
    which only grow with real activity) - unbounded on a long-running
    deployment unless something prunes it. Old rows go, recent ones stay."""
    db = make_db(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.record_snapshot(ts=old_ts, balance=50.0, equity=50.0, free_margin=50.0)
    db.record_snapshot(ts=recent_ts, balance=51.0, equity=51.0, free_margin=51.0)

    deleted = db.prune_old_snapshots(keep_days=30)

    assert deleted == 1
    remaining = db.equity_curve(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["ts"] == recent_ts


def test_prune_old_snapshots_is_a_noop_when_nothing_is_old_enough(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    db.record_snapshot(ts=now, balance=50.0, equity=50.0, free_margin=50.0)

    deleted = db.prune_old_snapshots(keep_days=30)

    assert deleted == 0
    assert len(db.equity_curve(limit=10)) == 1


def test_settings_roundtrip_and_empty_by_default(tmp_path):
    db = make_db(tmp_path)
    assert db.get_all_settings() == {}

    db.set_settings({"mt5_login": "12345", "mt5_server": "FBS-Demo"})
    assert db.get_all_settings() == {"mt5_login": "12345", "mt5_server": "FBS-Demo"}


def test_settings_upsert_overwrites_existing_key_and_leaves_others(tmp_path):
    db = make_db(tmp_path)
    db.set_settings({"mt5_login": "12345", "mt5_server": "FBS-Demo"})

    db.set_settings({"mt5_login": "99999"})

    assert db.get_all_settings() == {"mt5_login": "99999", "mt5_server": "FBS-Demo"}


def test_settings_persist_across_database_instances(tmp_path):
    """Same file, a new Database() object (simulating a process restart) -
    settings must survive, that's the entire point of storing them here."""
    path = str(tmp_path / "persist.db")
    Database(path).set_settings({"mt5_server": "ICMarkets-Live"})

    reopened = Database(path)
    assert reopened.get_all_settings() == {"mt5_server": "ICMarkets-Live"}
