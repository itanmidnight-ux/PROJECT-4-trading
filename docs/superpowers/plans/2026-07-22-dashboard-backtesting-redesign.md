# Dashboard Rebuild + Backtesting Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/api/backtest` actually usable (seconds, not 6+ minutes, for the full 100–200000 bar range the UI offers) and rebuild the dashboard's visual design (terminal-green/near-black palette, JetBrains Mono + IBM Plex Sans, redesigned live-status indicator) — without ever touching the live trading engine/bridge session already running against a real account.

**Architecture:** Backend track eliminates redundant indicator recomputation in `core/signals.py`/`core/strategy.py` and adds an opt-in global-precompute fast path to `core/backtest.py`, wired into `dashboard.py`'s `/api/backtest`. Frontend track rebuilds `dashboard/{index.html,app.js,style.css}` in place — same vanilla stack, same API endpoints, same element IDs where `app.js` logic doesn't need to change.

**Tech Stack:** Python 3.13, pandas, Flask (backend). Vanilla HTML/CSS/JS, no build step, no framework (frontend). Playwright driver at `.claude/skills/run-xauusd-scalper/driver.py` for visual verification. pytest for backend tests.

## Global Constraints

- The live engine (real orders, `DRY_RUN=false`, account 106049158 @ FBS-Demo) may be running throughout this work. **Never run `./run.sh --stop`, `./run.sh --start`, `emergency-stop`, or click "Detener motor"/"Pausar entradas"** as part of any task in this plan — see `.claude/skills/run-xauusd-scalper/SKILL.md` for why a second `--start` corrupts shared state.
- Backend changes are validated against local CSVs (`data/*.csv`) first. The only live-bridge interaction allowed is a final **read-only** `curl`/Playwright check against the already-running dashboard — never restart anything to get there.
- `./run.sh verify` (compile + 220 tests + synthetic smoke + dashboard API check, ~90s) must pass after every task that touches Python code.
- The live engine's own trading behavior (`core/engine.py`'s call to `strategy.generate_signal(candles.iloc[:-1], tick.spread_price, lot_hint)`, 3 positional args) must keep working unchanged — all new parameters on `generate_signal` are optional kwargs defaulting to today's exact behavior.
- Codex (`codex` MCP, already connected; backend approach already discussed in thread `019f8be9-838a-7aa3-b281-747e47368137`) reviews the backend diff and the frontend diff as separate checkpoints before either is considered done — see Task 6 and Task 15.
- No fake/placeholder data anywhere in the dashboard — already true today (verified: no `mock`/`fake`/`dummy` in `dashboard.py`/`app.js`), preserve it.

---

## Part 1: Backend — fix `/api/backtest` performance

### Task 1: Regression test for the redundant mean-reversion recomputation

**Files:**
- Test: `tests/test_signals.py` (existing file — check for it with `ls tests/test_signals.py`; if it doesn't exist, create it)
- Modify (in a later task, not this one): `core/signals.py:660-753` (`CompositeStrategy`)

**Interfaces:**
- Consumes: `core.signals.CompositeStrategy`, `core.strategy.ScalpStrategy`, `core.strategy.compute_indicators` (all exist today)
- Produces: nothing new — this is a characterization test that currently FAILS against the bug, and PASSES once Task 2 lands

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py — add this test (import unittest.mock.patch at top if not already imported)
from unittest.mock import patch
import pandas as pd
from core.signals import CompositeStrategy
from core.strategy import ScalpStrategy, compute_indicators


def _flat_candles(n=120):
    # Flat/no-signal series on purpose: mean-reversion won't fire, so
    # generate_signal is forced down the "no signal, check extras, fall
    # through" path where the redundant recompute happens.
    import numpy as np
    close = 2000.0 + np.zeros(n)
    return pd.DataFrame({
        "time": range(n), "open": close, "high": close + 0.1,
        "low": close - 0.1, "close": close,
    })


def test_composite_generate_signal_computes_indicators_at_most_twice():
    """One CompositeStrategy.generate_signal() call, with extra strategies
    configured and mean-reversion NOT firing, must call compute_indicators
    at most twice: once for mean-reversion's own periods, once for the
    composite's own (different) periods for regime/extras — never a third
    time recomputing mean-reversion's indicators again in the fallback
    return. Today it calls 3 times; this fails until Task 2's fix lands."""
    mean_reversion = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)

    class NoOpExtra:
        def check(self, df, ind, spread_price):
            from core.signals import SubSignal
            return SubSignal(side=None, reason="never fires")

    composite = CompositeStrategy(
        mean_reversion=mean_reversion,
        extra_strategies=[("noop", NoOpExtra())],
    )
    df = _flat_candles()

    with patch("core.strategy.compute_indicators", wraps=compute_indicators) as spy:
        composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)

    assert spy.call_count <= 2, f"expected <=2 compute_indicators calls, got {spy.call_count}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_signals.py::test_composite_generate_signal_computes_indicators_at_most_twice -v`
Expected: FAIL — `assert spy.call_count <= 2` with `call_count == 3` (or the test errors if `tests/test_signals.py` doesn't exist yet and needs the imports added — create the file with just this test + imports if it's new)

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_signals.py
git commit -m "test: characterize redundant compute_indicators call in CompositeStrategy"
```

---

### Task 2: Fix the redundant recomputation (cache mean-reversion signal, share regime/composite indicators)

**Files:**
- Modify: `core/signals.py:724-753` (`CompositeStrategy.generate_signal`)
- Test: `tests/test_signals.py` (from Task 1 — this test now passes)

**Interfaces:**
- Consumes: unchanged public API of `ScalpStrategy.generate_signal(df, spread_price, lot_hint)` (Task 3 adds an optional 4th param, backward compatible)
- Produces: `CompositeStrategy.generate_signal` behavior is byte-for-byte identical in output (same `Signal` returned for the same input), only the internal call count changes

- [ ] **Step 1: Implement the fix**

Replace `CompositeStrategy.generate_signal` in `core/signals.py` (currently lines 724-753) with:

```python
    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float) -> Signal:
        mr_signal = self._mean_reversion.generate_signal(df, spread_price, lot_hint)
        if not self._prefer_quantum_queen and mr_signal.side is not None:
            return mr_signal
        if not self._extra:
            return mr_signal

        if len(df) < self._warmup_bars:
            return mr_signal

        ind = compute_indicators(df, rsi_period=self._indicator_rsi_period, atr_period=self._indicator_atr_period)
        regime = detect_regime(df, **self._regime_kwargs) if self._regime_filter_enabled else None
        last = ind.iloc[-1]
        vol_ratio = compute_vol_ratio(last["atr"], last["atr_baseline"])

        ordered = self._extra
        if self._prefer_quantum_queen:
            ordered = sorted(self._extra, key=lambda item: 0 if item[0] == "quantum_queen" else 1)
        for name, sub in ordered:
            if regime and regime.name == "volatile" and name in {"ma_grid", "quantum_queen"}:
                continue
            sub_signal = sub.check(df, ind, spread_price)
            if sub_signal.side is None:
                continue
            sl_distance = max(sub_signal.sl_distance_price, self._mean_reversion.min_tp_distance_for_lot(lot_hint) * 1.5)
            tp_levels = self._mean_reversion.build_tp_ladder(lot_hint, spread_price, vol_ratio=vol_ratio)
            return Signal(side=sub_signal.side, sl_distance_price=sl_distance, tp_levels=tp_levels,
                           reason=f"{name}: {sub_signal.reason}", vol_ratio=vol_ratio)

        return mr_signal
```

The only change from today: the final `return self._mean_reversion.generate_signal(df, spread_price, lot_hint)` becomes `return mr_signal` (the value already computed at the top of the function, reused instead of recomputed).

- [ ] **Step 2: Run the Task 1 test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_signals.py::test_composite_generate_signal_computes_indicators_at_most_twice -v`
Expected: PASS

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (this change is behavior-preserving — same signal returned, fewer calls to get there)

- [ ] **Step 4: Commit**

```bash
git add core/signals.py
git commit -m "fix: stop recomputing mean-reversion signal a 3rd time in CompositeStrategy fallback"
```

---

### Task 3: `ScalpStrategy.generate_signal` accepts precomputed indicators (opt-in, backward compatible)

**Files:**
- Modify: `core/strategy.py:268-307` (`ScalpStrategy.generate_signal`)
- Test: `tests/test_strategy.py` (check with `ls tests/test_strategy.py`; add to it or create it)

**Interfaces:**
- Consumes: `core.strategy.compute_indicators(df, bb_period, bb_std, rsi_period, atr_period, adx_period) -> pd.DataFrame` (existing, unchanged)
- Produces: `ScalpStrategy.generate_signal(self, df, spread_price, lot_hint, precomputed_indicators: pd.DataFrame | None = None) -> Signal` — when `precomputed_indicators` is given, it MUST be a DataFrame whose **last row** is the indicator row for `df`'s last bar (same columns `compute_indicators` produces); used directly instead of calling `compute_indicators(df, ...)`. When `None` (the default — this is what `core/engine.py`'s live call path uses, unchanged), behavior is byte-for-byte identical to today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_strategy.py — add this test
from core.strategy import ScalpStrategy, compute_indicators
import pandas as pd
import numpy as np


def _trending_down_candles(n=60):
    close = 2000.0 - np.arange(n) * 0.5
    return pd.DataFrame({
        "time": range(n), "open": close + 0.2, "high": close + 0.3,
        "low": close - 0.3, "close": close,
    })


def test_generate_signal_uses_precomputed_indicators_when_given():
    """When precomputed_indicators is passed, generate_signal must use it
    verbatim (not recompute) - proven by feeding a precomputed frame with
    deliberately doctored indicator values that would flip the decision
    (force rsi/bb into an oversold BUY setup) even though the real
    (uncomputed-from-df) values wouldn't produce that signal."""
    strat = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)
    df = _trending_down_candles()

    real_ind = compute_indicators(df, bb_period=strat.bb_period, bb_std=strat.bb_std,
                                   rsi_period=strat.rsi_period, atr_period=strat.atr_period,
                                   adx_period=strat.adx_period)
    doctored = real_ind.copy()
    last_idx = doctored.index[-1]
    doctored.loc[last_idx, "bb_lower"] = df["close"].iloc[-1] + 10  # close is now "below" bb_lower
    doctored.loc[last_idx, "rsi"] = 5.0  # deeply oversold
    doctored.loc[last_idx, "adx"] = 0.0  # no trend filter block
    doctored.loc[last_idx, "atr"] = 5.0  # well above min_atr_price

    result = strat.generate_signal(df, spread_price=0.2, lot_hint=0.01, precomputed_indicators=doctored)
    assert result.side == "BUY", f"expected doctored precomputed indicators to force a BUY, got {result}"


def test_generate_signal_without_precomputed_indicators_unchanged():
    """precomputed_indicators=None (the default) must behave exactly like
    calling generate_signal with the old 3-arg signature."""
    strat = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)
    df = _trending_down_candles()
    old_style = strat.generate_signal(df, 0.2, 0.01)
    new_style = strat.generate_signal(df, 0.2, 0.01, precomputed_indicators=None)
    assert old_style == new_style
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_strategy.py::test_generate_signal_uses_precomputed_indicators_when_given -v`
Expected: FAIL with `TypeError: generate_signal() got an unexpected keyword argument 'precomputed_indicators'`

- [ ] **Step 3: Implement**

In `core/strategy.py`, replace the start of `ScalpStrategy.generate_signal` (currently lines 268-278):

```python
    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float,
                         precomputed_indicators: pd.DataFrame | None = None) -> Signal:
        if self._bars_since_last_trade < self.cooldown_bars:
            return Signal(side=None, reason="cooldown")

        if len(df) < self._warmup_bars:
            return Signal(side=None, reason="not enough history")

        if precomputed_indicators is not None:
            ind = precomputed_indicators
        else:
            ind = compute_indicators(df, bb_period=self.bb_period, bb_std=self.bb_std,
                                      rsi_period=self.rsi_period, atr_period=self.atr_period,
                                      adx_period=self.adx_period)
        last = ind.iloc[-1]
```

(the rest of the method, from `if any(pd.isna(last[c]) ...` onward, is unchanged)

- [ ] **Step 4: Run both new tests**

Run: `.venv/bin/python -m pytest tests/test_strategy.py -v -k precomputed`
Expected: both PASS

- [ ] **Step 5: Run the Signal dataclass equality check works**

Run: `.venv/bin/python -c "from core.strategy import Signal; import dataclasses; print(dataclasses.is_dataclass(Signal))"`
Expected: `True` (confirms `==` comparison in `test_generate_signal_without_precomputed_indicators_unchanged` works structurally — if `Signal` is not a dataclass, check `core/strategy.py` for how it's defined and adjust the test to compare fields individually instead)

- [ ] **Step 6: Full suite + commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add core/strategy.py tests/test_strategy.py
git commit -m "feat: ScalpStrategy.generate_signal accepts optional precomputed indicators"
```

---

### Task 4: `CompositeStrategy.generate_signal` forwards precomputed indicators too

**Files:**
- Modify: `core/signals.py` (the `generate_signal` from Task 2)
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `ScalpStrategy.generate_signal(..., precomputed_indicators=...)` from Task 3
- Produces: `CompositeStrategy.generate_signal(self, df, spread_price, lot_hint, precomputed_mr_indicators: pd.DataFrame | None = None, precomputed_composite_indicators: pd.DataFrame | None = None) -> Signal`. Both default to `None` (today's exact behavior). `precomputed_mr_indicators` is forwarded to the wrapped `ScalpStrategy`; `precomputed_composite_indicators` is used for the extras/regime `ind` instead of recomputing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signals.py
def test_composite_forwards_precomputed_indicators():
    from core.strategy import compute_indicators
    from unittest.mock import patch

    mean_reversion = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)

    class NoOpExtra:
        def check(self, df, ind, spread_price):
            from core.signals import SubSignal
            return SubSignal(side=None, reason="never fires")

    composite = CompositeStrategy(mean_reversion=mean_reversion, extra_strategies=[("noop", NoOpExtra())])
    df = _flat_candles()
    mr_ind = compute_indicators(df, bb_period=mean_reversion.bb_period, bb_std=mean_reversion.bb_std,
                                 rsi_period=mean_reversion.rsi_period, atr_period=mean_reversion.atr_period,
                                 adx_period=mean_reversion.adx_period)
    composite_ind = compute_indicators(df, rsi_period=composite._indicator_rsi_period,
                                        atr_period=composite._indicator_atr_period)

    with patch("core.strategy.compute_indicators", wraps=compute_indicators) as spy, \
         patch("core.signals.compute_indicators", wraps=compute_indicators) as spy2:
        composite.generate_signal(df, 0.2, 0.01,
                                   precomputed_mr_indicators=mr_ind,
                                   precomputed_composite_indicators=composite_ind)
    assert spy.call_count == 0
    assert spy2.call_count == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_signals.py::test_composite_forwards_precomputed_indicators -v`
Expected: FAIL (`TypeError: unexpected keyword argument`)

- [ ] **Step 3: Implement**

Update `CompositeStrategy.generate_signal` (from Task 2) to:

```python
    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float,
                         precomputed_mr_indicators: pd.DataFrame | None = None,
                         precomputed_composite_indicators: pd.DataFrame | None = None) -> Signal:
        mr_signal = self._mean_reversion.generate_signal(df, spread_price, lot_hint,
                                                           precomputed_indicators=precomputed_mr_indicators)
        if not self._prefer_quantum_queen and mr_signal.side is not None:
            return mr_signal
        if not self._extra:
            return mr_signal

        if len(df) < self._warmup_bars:
            return mr_signal

        if precomputed_composite_indicators is not None:
            ind = precomputed_composite_indicators
        else:
            ind = compute_indicators(df, rsi_period=self._indicator_rsi_period, atr_period=self._indicator_atr_period)
        regime = detect_regime(df, **self._regime_kwargs) if self._regime_filter_enabled else None
        last = ind.iloc[-1]
        vol_ratio = compute_vol_ratio(last["atr"], last["atr_baseline"])

        ordered = self._extra
        if self._prefer_quantum_queen:
            ordered = sorted(self._extra, key=lambda item: 0 if item[0] == "quantum_queen" else 1)
        for name, sub in ordered:
            if regime and regime.name == "volatile" and name in {"ma_grid", "quantum_queen"}:
                continue
            sub_signal = sub.check(df, ind, spread_price)
            if sub_signal.side is None:
                continue
            sl_distance = max(sub_signal.sl_distance_price, self._mean_reversion.min_tp_distance_for_lot(lot_hint) * 1.5)
            tp_levels = self._mean_reversion.build_tp_ladder(lot_hint, spread_price, vol_ratio=vol_ratio)
            return Signal(side=sub_signal.side, sl_distance_price=sl_distance, tp_levels=tp_levels,
                           reason=f"{name}: {sub_signal.reason}", vol_ratio=vol_ratio)

        return mr_signal
```

Note `patch("core.signals.compute_indicators", ...)` in the test requires `compute_indicators` to be imported by name into `core/signals.py` (`from core.strategy import compute_indicators` or similar) — check the existing import at the top of `core/signals.py`; if it's imported differently (e.g. `from core import strategy` then used as `strategy.compute_indicators`), adjust the patch target string to match (`"core.signals.strategy.compute_indicators"` or whatever the actual reference path is) — run `grep -n "^from core.strategy import\|^import core.strategy" core/signals.py` to confirm before writing the patch target.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_signals.py -v`
Expected: all PASS, including the new test and Task 1's test

- [ ] **Step 5: Full suite + commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add core/signals.py tests/test_signals.py
git commit -m "feat: CompositeStrategy.generate_signal forwards precomputed indicators to mean-reversion and extras"
```

---

### Task 5: `run_backtest` global-precompute fast path

**Files:**
- Modify: `core/backtest.py:30-92` (signature + pre-loop setup), `core/backtest.py:211-214` (the per-bar `generate_signal` call site)
- Test: `tests/test_backtest.py` (existing file)

**Interfaces:**
- Consumes: `ScalpStrategy.generate_signal(..., precomputed_indicators=...)` (Task 3), `CompositeStrategy.generate_signal(..., precomputed_mr_indicators=..., precomputed_composite_indicators=...)` (Task 4)
- Produces: `run_backtest(..., precompute_indicators: bool = False)` — new keyword, default `False` preserves today's exact per-bar-window behavior (`live_parity`, used by every existing caller/test unless they opt in). `True` computes indicators once over the full `candles` series and slices a 3-row tail per bar instead of recomputing from a windowed slice.

- [ ] **Step 1: Write the failing test — bounded time**

```python
# tests/test_backtest.py — add these two tests
import time
import pandas as pd
from pathlib import Path
from core.backtest import run_backtest
from core.risk_manager import SymbolSpec
from core.signals import build_strategy_from_settings
from core.config import Settings


def _real_gold_csv(n=3000):
    path = Path(__file__).resolve().parent.parent / "data" / "gold_m1_7d.csv"
    if not path.exists():
        import pytest
        pytest.skip("data/gold_m1_7d.csv not present in this checkout")
    return pd.read_csv(path).tail(n).reset_index(drop=True)


def _test_spec():
    return SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=100, volume_step=0.01,
                       point=0.01, trade_tick_value=1.0, trade_tick_size=0.01, margin_initial=None)


def _test_settings():
    return Settings(
        mt5_login="1", mt5_password="x", mt5_server="s", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1", risk_per_trade_usd=6.0,
        max_daily_loss_usd=40.0, max_daily_drawdown_pct=20.0, max_trades_per_day=1000,
        min_tp_usd=0.5, tp_levels=3, dry_run=True, db_path=":memory:",
        strat_enable_ma_grid=True,
    )


def test_backtest_with_precompute_is_fast():
    """3000 real M1 candles must complete in well under the ~180s the
    windowed default takes today - generous 10s bound, actual should be
    low single digits."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()
    settings = _test_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)
    strategy = build_strategy_from_settings(settings, value_per_point)

    t0 = time.time()
    result = run_backtest(candles=candles, spec=spec, starting_balance=50, leverage=500,
                           risk_per_trade_usd=6, min_tp_usd=settings.min_tp_usd,
                           tp_levels=settings.tp_levels, assumed_spread_price=0.25,
                           max_trades_per_day=settings.max_trades_per_day,
                           strategy=strategy, precompute_indicators=True)
    elapsed = time.time() - t0
    assert elapsed < 10.0, f"precompute_indicators=True took {elapsed:.1f}s for 3000 bars, expected <10s"
    assert result.trades >= 0  # sanity: it actually ran, not a silent no-op
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_backtest.py::test_backtest_with_precompute_is_fast -v`
Expected: FAIL with `TypeError: run_backtest() got an unexpected keyword argument 'precompute_indicators'`

- [ ] **Step 3: Implement `run_backtest`'s precompute wiring**

In `core/backtest.py`, change the signature (currently line 30-44):

```python
def run_backtest(
    candles: pd.DataFrame,
    spec: SymbolSpec,
    starting_balance: float,
    leverage: int,
    risk_per_trade_usd: float,
    min_tp_usd: float,
    tp_levels: int,
    assumed_spread_price: float,
    max_trades_per_day: int = 1000,
    strategy_overrides: dict | None = None,
    strategy: object | None = None,
    max_lookback_bars: int = 600,
    max_hold_bars: int = 0,
    ticks: pd.DataFrame | None = None,
    precompute_indicators: bool = False,
) -> BacktestResult:
```

Add to the docstring (after the existing `max_lookback_bars` paragraph):

```
    precompute_indicators (default False = today's exact "live_parity"
    behavior, unchanged): when True, computes core/strategy.py's
    compute_indicators() ONCE over the full `candles` series before the
    loop (O(n) total) instead of recomputing it from scratch on a
    max_lookback_bars-sized window every single bar (O(n * 600)) - this is
    the fix for the ~120ms/bar cost that made a 3000-bar backtest take 6+
    minutes. Mathematically safe for compute_indicators' rolling/ewm
    columns (ewm(adjust=False) is causal: value at i depends only on data
    <= i - verified with Codex, EMA50's memory of a seed 600 bars back is
    ~1e-11, negligible). Does NOT change: detect_regime() (still
    window-computed, ~11% of the original cost, left as a known follow-up
    - see spec doc's "Riesgos conocidos"), or any extra strategy's own
    internal M5/M15 resample logic (MACrossGridStrategy etc. still see the
    same windowed `df` as before - this is what keeps this change free of
    look-ahead risk on the resample-based strategies).
```

Add before the main loop (after the `if strategy is None: ...` block, currently around line 76-78):

```python
    precomputed_mr = precomputed_composite = None
    if precompute_indicators:
        mr_strategy = getattr(strategy, "_mean_reversion", strategy)
        precomputed_mr = compute_indicators(
            candles, bb_period=mr_strategy.bb_period, bb_std=mr_strategy.bb_std,
            rsi_period=mr_strategy.rsi_period, atr_period=mr_strategy.atr_period,
            adx_period=mr_strategy.adx_period)
        extra = getattr(strategy, "_extra", None)
        if extra:
            precomputed_composite = compute_indicators(
                candles, rsi_period=strategy._indicator_rsi_period,
                atr_period=strategy._indicator_atr_period)
```

Add the import at the top of `core/backtest.py` (alongside the existing `from core.strategy import ScalpStrategy`):

```python
from core.strategy import ScalpStrategy, compute_indicators
```

Change the per-bar signal-generation call (currently lines 213-214):

```python
        can_trade, _ = risk.can_open_new_trade(balance)
        if open_pos is None and can_trade:
            if precompute_indicators:
                mr_slice = precomputed_mr.iloc[max(0, i - 2): i + 1]
                if precomputed_composite is not None:
                    composite_slice = precomputed_composite.iloc[max(0, i - 2): i + 1]
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                       precomputed_mr_indicators=mr_slice,
                                                       precomputed_composite_indicators=composite_slice)
                elif hasattr(strategy, "_mean_reversion"):
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                       precomputed_mr_indicators=mr_slice)
                else:
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                        precomputed_indicators=mr_slice)
            else:
                signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min)
```

(this replaces the existing single-line `signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min)` at line 214 — everything below it, from `if signal.side:` onward, is unchanged)

- [ ] **Step 4: Run the bounded-time test**

Run: `.venv/bin/python -m pytest tests/test_backtest.py::test_backtest_with_precompute_is_fast -v`
Expected: PASS, printed test duration well under 10s

- [ ] **Step 5: Write the divergence test (compare against today's default behavior)**

```python
# tests/test_backtest.py
def test_precompute_matches_live_parity_trade_count():
    """The fast path must produce the same (or near-identical) trades as
    today's windowed default on real data - if this diverges by more than
    a handful of trades, something is wrong with the precompute wiring,
    not an acceptable floating-point difference."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()
    settings = _test_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)

    kwargs = dict(candles=candles, spec=spec, starting_balance=50, leverage=500,
                  risk_per_trade_usd=6, min_tp_usd=settings.min_tp_usd,
                  tp_levels=settings.tp_levels, assumed_spread_price=0.25,
                  max_trades_per_day=settings.max_trades_per_day)

    baseline = run_backtest(**kwargs, strategy=build_strategy_from_settings(settings, value_per_point),
                             precompute_indicators=False)
    fast = run_backtest(**kwargs, strategy=build_strategy_from_settings(settings, value_per_point),
                         precompute_indicators=True)

    assert abs(fast.trades - baseline.trades) <= 2, (
        f"trade count diverged too much: baseline={baseline.trades} fast={fast.trades}")
```

- [ ] **Step 6: Run it — this test is allowed to take a while (baseline uses the slow path)**

Run: `.venv/bin/python -m pytest tests/test_backtest.py::test_precompute_matches_live_parity_trade_count -v -s`
Expected: PASS. If `baseline` (the slow windowed path, `precompute_indicators=False`) makes this test take multiple minutes, that's expected — it's exercising the OLD slow behavior on purpose as the comparison point. If it diverges by more than 2 trades, do not "fix the test" — investigate `core/backtest.py`'s precompute wiring for an off-by-one in the `iloc[max(0, i-2):i+1]` slicing before proceeding.

- [ ] **Step 7: Full suite + commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add core/backtest.py tests/test_backtest.py
git commit -m "feat: run_backtest precompute_indicators=True fast path (O(n) instead of O(n*600))"
```

---

### Task 6: Codex review checkpoint — backend diff

**Files:** none (review only)

- [ ] **Step 1: Get the backend diff**

```bash
git log --oneline -5
git diff a411529..HEAD -- core/signals.py core/strategy.py core/backtest.py tests/test_signals.py tests/test_strategy.py tests/test_backtest.py
```

- [ ] **Step 2: Send it to Codex for review**

Use `mcp__codex__codex-reply` with `threadId: "019f8be9-838a-7aa3-b281-747e47368137"` (the same thread that already validated this approach) and a prompt that pastes the diff from Step 1, asking specifically: (a) does the `iloc[max(0, i-2):i+1]` tail-slicing correctly avoid look-ahead (row `i` never sees data beyond `i`), (b) does the `getattr(strategy, "_mean_reversion", strategy)` / `getattr(strategy, "_extra", None)` duck-typing in `core/backtest.py` correctly handle both a bare `ScalpStrategy` and a `CompositeStrategy` being passed as `strategy`, (c) any other correctness issue with the diff.

- [ ] **Step 3: Address findings**

If Codex flags anything, fix it, re-run `.venv/bin/python -m pytest tests/ -q`, and commit the fix as a new commit (don't amend). If Codex has no findings, proceed.

- [ ] **Step 4: Wire the fast path into the dashboard's `/api/backtest`**

**Files:** Modify: `dashboard.py:479-483`

Change the `run_backtest(...)` call inside `api_backtest()` (currently lines 479-483) to add `precompute_indicators=True`:

```python
        result = run_backtest(candles=candles, spec=spec, starting_balance=balance,
            leverage=leverage, risk_per_trade_usd=risk_usd, min_tp_usd=effective.min_tp_usd,
            tp_levels=effective.tp_levels, assumed_spread_price=spread,
            max_trades_per_day=effective.max_trades_per_day, max_hold_bars=max_hold, ticks=ticks,
            strategy=strategy, precompute_indicators=True)
```

- [ ] **Step 5: `./run.sh verify`**

Run: `./run.sh verify`
Expected: `220 passed` (or more, with the new tests) `in ~90s`, plus the new `test_backtest_with_precompute_is_fast`/`test_precompute_matches_live_parity_trade_count` tests among them — note `test_precompute_matches_live_parity_trade_count` will add real time to the suite (it runs the slow baseline once); if this pushes total suite time uncomfortably high, that's expected and acceptable — it's exercising the actual old slow path once as a regression guard, not a bug.

- [ ] **Step 6: Read-only smoke test against the worktree's throwaway dashboard**

Start the throwaway dashboard per "Execution note" above if it isn't already running, then:

```bash
PORT=$(cat data/run/dashboard.port)
time curl -s -X POST http://127.0.0.1:$PORT/api/backtest \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XAUUSD","timeframe":"M1","bars":3000,"balance":50,"leverage":500,"risk_usd":6,"spread":0.25,"tick_mode":false}' \
  | python3 -m json.tool
```

This hits the real MT5 bridge (read-only, same bridge the live engine uses, `threaded=True` so concurrent requests are fine) through the worktree's own throwaway dashboard process — never the main checkout's live dashboard. Expected: responds in single-digit seconds (not minutes), `"ok": true`, with `trades`/`win_rate`/`total_pnl` fields populated. Do **not** retry this repeatedly if it's slow/fails — if it doesn't come back quickly, stop and re-check the CSV-based tests instead of hammering the shared bridge.

- [ ] **Step 7: Commit the dashboard wiring**

```bash
git add dashboard.py
git commit -m "fix: /api/backtest uses the fast precompute path (was 6+ min, now single-digit seconds)"
```

---

## Execution note: worktree + throwaway verification dashboard

This plan executes in an isolated git worktree
(`.worktrees/dashboard-backtesting-redesign`, branch
`dashboard-backtesting-redesign`), not the main checkout — the main
checkout has a LIVE dashboard/bridge/engine session running and
`dashboard.py` unconditionally overwrites `data/run/dashboard.port` on
every launch, so a second instance started from the same directory tree
would corrupt the live session's port tracking (this happened once
already this session). The worktree has its own `data/run/` (empty,
created fresh — never shared with the main checkout), plus symlinked
`.venv`/`.env`/`data/*.csv` and a **snapshot copy** (not a symlink) of
`data/trades.db`, so a dashboard instance started from inside the
worktree has real data and real bridge access, but can never write
through to the live database or collide with the live session's files.

**Every "read `data/run/dashboard.port`" / "against the live dashboard"
step in Part 1 (Task 6) and Part 2 (Tasks 8-14) below means: from inside
the worktree, start a throwaway `dashboard.py` instance first if one
isn't already running there:**

```bash
cd .worktrees/dashboard-backtesting-redesign
.venv/bin/python3 dashboard.py --web --host 127.0.0.1 --port 9100 > /tmp/worktree-dashboard.log 2>&1 &
echo $! > /tmp/worktree-dashboard.pid
timeout 20 bash -c 'until curl -sf http://127.0.0.1:9100/ >/dev/null 2>&1; do sleep 1; done'
PORT=$(cat data/run/dashboard.port)  # confirms the actual bound port (9100 unless taken)
```

Leave it running across tasks within the same work session (don't
restart it between every task — Python doesn't hot-reload code changes
either way in this project, `debug=False, use_reloader=False`, so **kill
and restart it after each task that changed `dashboard.py`/`core/*.py`**
to pick up the new code before screenshotting/curling it:

```bash
kill "$(cat /tmp/worktree-dashboard.pid)" 2>/dev/null; sleep 1
cd .worktrees/dashboard-backtesting-redesign && .venv/bin/python3 dashboard.py --web --host 127.0.0.1 --port 9100 > /tmp/worktree-dashboard.log 2>&1 &
echo $! > /tmp/worktree-dashboard.pid
timeout 20 bash -c 'until curl -sf http://127.0.0.1:9100/ >/dev/null 2>&1; do sleep 1; done'
```

At the end of the whole plan (Task 14), kill this throwaway process —
it's isolated, safe to stop freely, and is **not** the live session
(never run `./run.sh --stop` — that command must never be run from the
worktree or the main checkout during this work).

## Part 2: Frontend — dashboard visual rebuild

Design system (confirmed with the project owner, see the spec doc):

```css
/* Dark (default) */
--bg: #0D1117;
--card: #182424;
--primary: #00FF41;       /* terminal green - actions, gains, live pulse */
--primary-dim: #008F11;   /* secondary green / hover */
--destructive: #FF3333;   /* losses, dangerous actions */
--foreground: #E6EDF3;
--muted: #94A3B8;
--border: #30363D;
--gold-accent: #F59E0B;   /* ONLY the logo/brand mark, nowhere else */

/* Light */
--bg: #F7F9F8;
--card: #FFFFFF;
--primary: #008F11;       /* darker green for AA contrast on light bg */
--primary-dim: #00C22C;
--destructive: #C81E1E;
--foreground: #0D1117;
--muted: #57606A;
--border: #D0D7DE;
--gold-accent: #B45309;
```

```css
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
--font-data: 'JetBrains Mono', ui-monospace, monospace;   /* prices, PnL, equity, table numbers */
--font-ui: 'IBM Plex Sans', system-ui, sans-serif;         /* labels, buttons, body text, headings */
```

All existing element IDs in `dashboard/index.html` (e.g. `#tile-equity`, `#chart-equity`, `#trades-table`, `#pill-conn`, `#bt-progress`) are preserved exactly — `app.js`'s `document.getElementById(...)` calls keep working. Verification for every task in this part is a real screenshot via the driver, not a unit test.

### Task 7: Design tokens + typography foundation in `style.css`

**Files:**
- Modify: `dashboard/style.css` (full pass over `:root` / `:root[data-theme="dark"]` / `:root[data-theme="light"]` custom-property blocks — check the current top of the file with `grep -n ":root" dashboard/style.css` first to see the existing variable names being replaced, so every consumer further down the file that references e.g. `var(--series-1)` gets updated to the new token names or kept as an alias)
- Modify: `dashboard/index.html:1-9` (add the Google Fonts `<link>`/`@import` — prefer a `<link rel="preconnect">` + `<link rel="stylesheet" href="https://fonts.googleapis.com/...">` pair in `<head>`, before `style.css`, so the font loads in parallel)

- [ ] **Step 1: Inventory current tokens**

```bash
grep -n "^\s*--" dashboard/style.css | head -60
```

Note every `--variable-name` currently used (e.g. `--series-1`, `--good`, `--bad`, `--text-muted`) and where `var(--old-name)` appears elsewhere in the file (`grep -n "var(--old-name)" dashboard/style.css`), so the replacement pass doesn't leave a dangling reference to a removed token.

- [ ] **Step 2: Replace the token block**

Replace the `:root`/theme variable declarations with the palette from this task's header (dark as default `:root`, light under `:root[data-theme="light"]` — match whatever mechanism the existing file already uses for the light/dark toggle, found via `grep -n "data-theme" dashboard/style.css dashboard/app.js`). Map every old semantic role to its new token (e.g. old `--good`/`--bad` → new `--primary`/`--destructive`; old `--series-1` → `--primary`) rather than introducing parallel unused names.

- [ ] **Step 3: Add font tokens and apply globally**

```css
:root {
  --font-ui: 'IBM Plex Sans', system-ui, sans-serif;
  --font-data: 'JetBrains Mono', ui-monospace, monospace;
}
body { font-family: var(--font-ui); }
.value, .tile .value, #trades-table td.num, #chart-equity, .backtest-results .num {
  font-family: var(--font-data);
  font-variant-numeric: tabular-nums;
}
```

(adjust the selector list to match whatever classes actually wrap numeric output in the current `app.js` render functions — check with `grep -n "innerHTML\|textContent" dashboard/app.js | grep -i "value\|pnl\|equity\|balance"` to find them precisely before finalizing the selector list)

- [ ] **Step 4: Add the font link to `index.html`**

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="style.css">
```

- [ ] **Step 5: Verify with a real screenshot**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
screenshot 01-tokens-dark
console --errors
EOF
```

Read the resulting PNG (`.claude/skills/run-xauusd-scalper/sessions/redesign/screenshots/`) — confirm: near-black background, green accents visible on pills/buttons, numeric tiles render in a visibly monospaced font, no console errors, no `var(--undefined-token)` producing broken (transparent/black-on-black) elements.

- [ ] **Step 6: Commit**

```bash
git add dashboard/style.css dashboard/index.html
git commit -m "redesign: terminal-green/near-black design tokens + JetBrains Mono/IBM Plex Sans"
```

---

### Task 8: Redesign the live-status indicator (fix the jarring 3s animation)

**Files:**
- Modify: `dashboard/index.html:38-42` (`.live-strip` markup)
- Modify: `dashboard/app.js:1-20` (`setLiveState`), `dashboard/app.js:590` (the `refresh()` call site)
- Modify: `dashboard/style.css` (`.live-progress-wrap` rules, currently around lines 236-240)

**Interfaces:**
- Consumes: nothing new
- Produces: `setLiveState(kind, message, stamp)` keeps its exact signature (so every call site in `app.js` stays unchanged) but its DOM/animation behavior changes: routine polls (<400ms round-trip, the normal case) show **no** sliding-bar animation, only the updated timestamp text; a genuinely slow round-trip (≥400ms) shows a subtle pulse, not a sliding bar

- [ ] **Step 1: Read the current implementation precisely**

```bash
sed -n '1,20p' dashboard/app.js
sed -n '585,625p' dashboard/app.js
```

Confirm the exact call pattern: `setLiveState('loading', ...)` before the fetch, `setLiveState('connected'|'disconnected', ..., elapsedMs)` after. The fix needs the elapsed time to decide whether to show any motion at all — that value already exists at the `setLiveState(status.connected ? ... , \`${Math.round(performance.now() - started)} ms\`)` call site (currently `app.js:623`).

- [ ] **Step 2: Change the HTML — replace the sliding bar with a status dot**

Replace (currently `index.html:40`):
```html
<div class="live-progress-wrap" aria-hidden="true"><span id="live-progress"></span></div>
```
with:
```html
<span class="live-pulse" id="live-pulse" aria-hidden="true"></span>
```

- [ ] **Step 3: Change the CSS**

Replace the `.live-progress-wrap` rules (`dashboard/style.css`, currently lines 236-240) with:

```css
.live-pulse {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--primary);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 60%, transparent);
  transition: background-color .2s ease;
}
.live-pulse.slow {
  animation: live-pulse-ring 900ms ease-out infinite;
}
.live-pulse.stale { background: var(--destructive); }
@keyframes live-pulse-ring {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 55%, transparent); }
  100% { box-shadow: 0 0 0 8px color-mix(in srgb, var(--primary) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  .live-pulse.slow { animation: none; }
}
```

- [ ] **Step 4: Change `setLiveState` in `app.js`**

Replace the current `setLiveState` function (`app.js:7-16` roughly) — read the exact current body first with `sed -n '1,20p' dashboard/app.js` before writing the replacement, since it also toggles `#live-indicator` classes that must be preserved. The new version adds pulse-only-when-slow logic:

```javascript
function setLiveState(kind, message, stamp = '', elapsedMs = null) {
  const indicator = document.getElementById('live-indicator');
  const text = document.getElementById('live-strip-text');
  const time = document.getElementById('live-strip-time');
  const pulse = document.getElementById('live-pulse');
  if (indicator) indicator.className = `live-indicator ${kind}`;
  if (text) text.textContent = message;
  if (time) time.textContent = stamp;
  if (pulse) {
    pulse.classList.toggle('stale', kind === 'disconnected');
    pulse.classList.toggle('slow', elapsedMs !== null && elapsedMs >= 400);
  }
}
```

(preserve any other DOM updates the current function does — e.g. if it also sets `progress.classList.toggle('complete', ...)`, remove that reference since `#live-progress` no longer exists, don't leave a dangling `getElementById` call that silently no-ops)

- [ ] **Step 5: Update the call site to pass elapsed time**

At the call site currently around `app.js:623`:

```javascript
setLiveState(status.connected ? 'connected' : 'disconnected',
  status.connected ? (status.engine_running ? 'Motor activo · datos en vivo' : 'Dashboard conectado · motor detenido') : 'Motor sin datos recientes',
  `${Math.round(performance.now() - started)} ms`,
  Math.round(performance.now() - started));
```

And the `'loading'` call before the fetch (currently `app.js:590`) stays as `setLiveState('loading', 'Actualizando datos en vivo…')` (no `elapsedMs` — `null` default means no pulse while genuinely loading is fine since this state is normally sub-second).

- [ ] **Step 6: Verify with a screenshot + a timing check**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
sleep 4000
screenshot 02-live-pulse
eval document.getElementById('live-pulse').className
console --errors
EOF
```

Expected: `eval` prints `live-pulse` (no `slow` class, since the real round-trip is ~80-180ms per the status endpoint's own historical timing) — confirming the routine 3s poll no longer shows the sliding-animation class. No console errors.

- [ ] **Step 7: Commit**

```bash
git add dashboard/index.html dashboard/app.js dashboard/style.css
git commit -m "redesign: replace always-animating progress bar with a pulse shown only on slow polls"
```

---

### Task 9: Redesign the Dashboard tab (stat tiles, equity chart, trade history, events)

**Files:**
- Modify: `dashboard/style.css` (`.tiles`, `.tile`, `.panel`, `.grid` rules)
- Modify: `dashboard/index.html:52-116` (class names only if needed for new layout hooks — keep every `id=` unchanged)

- [ ] **Step 1: Restyle stat tiles**

Update `.tile`/`.tile.hero` rules in `style.css` to use `--card` background, `--border` 1px border, `--font-data` for `.value`, `--primary`/`--destructive` for `.delta` based on sign (check `app.js` for how `.delta`'s positive/negative class is currently set — likely already conditional, just repoint the CSS class names to the new tokens). Density: 8/10 (dashboard-dense) means tighter padding than a marketing page — use `12-16px` tile padding, `8px` gaps in `.tiles` grid, not `24px+`.

- [ ] **Step 2: Restyle panels/charts containers**

`.panel` (equity curve, donut, daily/monthly P&L, trade history, events) get `--card` background, `--border`, consistent `16px` padding, `h2` in `--font-ui` weight 600, `.panel-sub` in `--muted`.

- [ ] **Step 3: Verify with a screenshot**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
screenshot 03-dashboard-tab
console --errors
EOF
```

Read the screenshot. Confirm: tiles/panels visually distinct from the page background (not blending into it — check `color-accessible-pairs`: card vs background must have enough contrast to read as a separate surface), numeric tiles in monospace, no layout overflow/clipping, no console errors.

- [ ] **Step 4: Commit**

```bash
git add dashboard/style.css dashboard/index.html
git commit -m "redesign: dashboard tab stat tiles and panels on the new design system"
```

---

### Task 10: Redesign the Settings tab

**Files:**
- Modify: `dashboard/style.css` (`.field`, `.field-grid`, `.field-checkbox`, `.btn-primary`, `.settings-message`, `.settings-note` rules)

- [ ] **Step 1: Restyle form fields**

Inputs get `--card` background, `--border`, `--foreground` text, `--primary` focus ring (`:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }` — required by the a11y checklist's `focus-states` rule). Labels stay visible above each input (already true in the current markup — preserve, don't switch to placeholder-only).

- [ ] **Step 2: Restyle the primary button**

`.btn-primary` (Guardar configuración): `--primary` background, dark text for contrast (`#0D1117` on `#00FF41` — verify 4.5:1, it comfortably exceeds it), `:hover`/`:active` states with the 150-300ms transition rule, `cursor: pointer`.

- [ ] **Step 3: Verify with a screenshot**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
click text=Settings
wait-for text=Riesgo
screenshot 04-settings-tab
console --errors
EOF
```

Confirm no secrets rendered (password/API key fields must still show masked placeholders, never real values — this was already true before the redesign, verify it's still true), readable contrast, focus ring visible when tabbing (can check via `eval document.activeElement` after a `press Tab` command).

- [ ] **Step 4: Commit**

```bash
git add dashboard/style.css
git commit -m "redesign: settings tab form fields on the new design system"
```

---

### Task 11: Redesign the Backtesting tab

**Files:**
- Modify: `dashboard/style.css` (`.task-progress`, `.progress-track` rules — same jarring-animation issue as Task 8's live strip, but here the animation is legitimately useful since a real backtest run does take a few seconds even with the Part 1 fast path, so keep motion here, just restyle its colors to the new tokens)
- Modify: `dashboard/index.html:214-237` only if new layout hooks are needed (keep `id=` values unchanged — `app.js`'s backtest submit handler depends on them)

- [ ] **Step 1: Restyle the backtest form and results panel**

Same `.field`/`.field-grid` treatment as Task 10. `.backtest-results` (rendered by `app.js` after a successful run) gets `--card` surface, numeric results (`trades`, `win_rate`, `total_pnl`, `final_balance`) in `--font-data`, win-rate/pnl colored `--primary` (positive) or `--destructive` (negative) — check how `app.js` currently renders `.backtest-results` (`grep -n "bt-results\|backtest-results" dashboard/app.js`) to match the actual class names it generates.

- [ ] **Step 2: Restyle `.progress-track`/`.task-progress`**

Repoint the gradient in `dashboard/style.css`'s `.progress-track span` rule (currently `linear-gradient(90deg, var(--series-1), #7c5cff)`) to `var(--primary)` → `var(--primary-dim)`.

- [ ] **Step 3: Verify with a real (fast, thanks to Part 1) backtest run end-to-end**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
click text=Backtesting MT5
wait-for text=Símbolo
screenshot 05-backtest-form
click text=Ejecutar backtest MT5
wait-for text=trades
screenshot 06-backtest-results
console --errors
EOF
```

This is the real end-to-end proof that Part 1's fix works from the actual UI, not just curl: `wait-for text=trades` has a 10s timeout in `driver.py` — if the backtest genuinely takes longer than 10s even with the fast path, increase this specific `wait-for`'s timeout in a one-off edit to `driver.py`'s `wait-for` command (or add an optional per-command timeout argument to it) rather than declaring the run.sh verify performance test misleading. Read the resulting screenshot to confirm real, non-fake numbers matching what the API returned in Task 6 Step 6.

- [ ] **Step 4: Commit**

```bash
git add dashboard/style.css
git commit -m "redesign: backtesting tab on the new design system, verified end-to-end via a real fast backtest run"
```

---

### Task 12: Light mode pass

**Files:**
- Modify: `dashboard/style.css` (verify every token from Task 7's light block is actually consumed correctly across all components touched in Tasks 8-11)

- [ ] **Step 1: Toggle to light mode and screenshot every tab**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session redesign <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
click "#theme-toggle"
sleep 300
screenshot 07-light-dashboard
click text=Settings
screenshot 08-light-settings
click text=Backtesting MT5
screenshot 09-light-backtest
console --errors
EOF
```

- [ ] **Step 2: Review all three screenshots for contrast issues**

Check specifically: body text on the light `--bg` (`#F7F9F8`) meets 4.5:1, `--primary` green on light background is the darker `#008F11` variant (not the neon `#00FF41`, which would fail contrast on a light surface — confirm Task 7 actually branched the token by theme, not just swapped `--bg`/`--foreground` while leaving `--primary` constant), borders/dividers visible (not invisible on white cards).

- [ ] **Step 3: Fix any contrast issue found, re-screenshot, commit**

```bash
git add dashboard/style.css
git commit -m "redesign: fix light-mode contrast issues found in visual review"
```

(only commit if Step 2 found something to fix — if light mode already looks correct, skip this commit)

---

### Task 13: Codex review checkpoint — full frontend diff

**Files:** none (review only)

- [ ] **Step 1: Get the frontend diff**

```bash
git diff <commit-before-task-7>..HEAD -- dashboard/
```

- [ ] **Step 2: Send it to Codex for review**

Use `mcp__codex__codex-reply` with the same thread ID as Task 6, or start a fresh `mcp__codex__codex` call if the thread has gone stale — paste the diff, ask specifically: (a) any accessibility regression (contrast, focus states, `prefers-reduced-motion`), (b) any place `app.js` might now reference a removed DOM id/class (`#live-progress` in particular — Task 8 removed it), (c) general visual-code-quality read of the CSS.

- [ ] **Step 3: Address findings, re-verify with screenshots, commit fixes**

---

### Task 14: Final full verification

**Files:** none (verification only)

- [ ] **Step 1: `./run.sh verify`**

Run: `./run.sh verify`
Expected: all tests pass (including Part 1's new backend tests), synthetic smoke test passes, dashboard API check passes.

- [ ] **Step 2: `./run.sh doctor`**

Run: `./run.sh doctor`
Expected: same "OK" counts as before this work started (bridge active, dashboard active, engine state whatever it naturally is by now — do not expect/require the engine to be in any particular state, just confirm nothing broke: no new `[FALTA]` lines that weren't there before this plan started).

- [ ] **Step 3: Full click-through screenshot pass, both themes, both dashboard ports if relevant**

```bash
PORT=$(cat data/run/dashboard.port)
python3 .claude/skills/run-xauusd-scalper/driver.py --session final <<EOF
nav http://127.0.0.1:$PORT
wait-for text=XAUUSD
screenshot 01-dashboard-dark
click text=Settings
screenshot 02-settings-dark
click text=Backtesting MT5
screenshot 03-backtest-dark
click "#theme-toggle"
sleep 300
screenshot 04-backtest-light
click text=Dashboard
screenshot 05-dashboard-light
console --errors
EOF
```

Review every screenshot. This is the final proof-of-done — if anything looks broken, fix it before considering this plan complete.

- [ ] **Step 4: Update the `run-xauusd-scalper` skill's screenshot references if stale**

If `.claude/skills/run-xauusd-scalper/SKILL.md` describes the old visual state anywhere (it currently doesn't — it only documents the driver's commands, not the UI's appearance — confirm with `grep -n "Motor activo\|progress" .claude/skills/run-xauusd-scalper/SKILL.md`), update it. Otherwise no change needed there.

- [ ] **Step 5: Final commit**

```bash
git add -A
git status  # confirm only expected files are staged - no stray screenshot/session files from .claude/skills/*/sessions/
git commit -m "chore: final verification pass for dashboard rebuild + backtesting fix"
```

Note: `.claude/skills/run-xauusd-scalper/sessions/` (screenshots taken during this plan) should NOT be committed — check `.gitignore` covers it (`grep -n "sessions" .gitignore`); if it doesn't, add `.claude/skills/*/sessions/` to `.gitignore` in this same commit.
