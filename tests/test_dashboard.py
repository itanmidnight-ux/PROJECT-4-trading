"""Tests for the web-only dashboard entry point."""
from unittest.mock import patch

import dashboard as dmod


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
