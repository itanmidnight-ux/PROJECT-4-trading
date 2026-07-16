"""Tests for dashboard.py's mode dispatch (native window vs web server).

Does NOT exercise the actual browser-rendered UI or a real pywebview
window - that was verified manually with a headless Chromium session via
Playwright (see the commit history), not part of this fast, Wine/GUI-free
test suite. These tests cover the CLI/prompt logic that decides which
mode runs, mocking out the two (blocking) entry points.
"""
from unittest.mock import patch

import pytest

import dashboard as dmod


def test_non_interactive_with_no_flags_defaults_to_native(monkeypatch):
    """Backward compatibility: running dashboard.py from a non-interactive
    context (no TTY, no flags - e.g. launched by another script) must
    behave exactly like before this feature existed."""
    monkeypatch.setattr("sys.argv", ["dashboard.py"])
    with patch("sys.stdin.isatty", return_value=False), \
         patch.object(dmod, "_run_native") as m_native, \
         patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_native.called
    assert not m_web.called


def test_web_flag_runs_web_mode_on_default_port_9000(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--web"])
    with patch.object(dmod, "_run_native") as m_native, \
         patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_web.called
    assert not m_native.called
    assert m_web.call_args.args == ("127.0.0.1", 9000)


def test_web_flag_with_explicit_host_and_port(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--web", "--host", "0.0.0.0", "--port", "9500"])
    with patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_web.call_args.args == ("0.0.0.0", 9500)


def test_native_flag_forces_native_even_from_a_tty_and_skips_the_prompt(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--native"])
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input") as m_input, \
         patch.object(dmod, "_run_native") as m_native, \
         patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_native.called
    assert not m_web.called
    m_input.assert_not_called()


def test_web_and_native_together_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py", "--web", "--native"])
    with pytest.raises(SystemExit):
        dmod.main()


def test_interactive_prompt_choosing_2_selects_web(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py"])
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="2"), \
         patch.object(dmod, "_run_native") as m_native, \
         patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_web.called
    assert not m_native.called


def test_interactive_prompt_default_choice_selects_native(monkeypatch):
    monkeypatch.setattr("sys.argv", ["dashboard.py"])
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value=""), \
         patch.object(dmod, "_run_native") as m_native, \
         patch.object(dmod, "_run_web") as m_web:
        dmod.main()
    assert m_native.called
    assert not m_web.called
