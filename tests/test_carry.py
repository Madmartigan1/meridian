"""The carry strategy, and the lookahead guard it depends on.

The single most dangerous bug available in a carry backtest is not an
arithmetic slip -- it is using today's policy rate on a bar from two years ago.
That produces a smooth, confident equity curve built entirely on information
that did not exist at the time, and nothing in the output looks wrong. So the
first half of this file is about dates, not about trading.

The second half pins the behaviours that stop carry quietly degenerating back
into a trend strategy: no RSI/MACD gating, a mandatory re-entry cooldown, and
an exit profile that does not close positions for the very behaviour that
defines them.

Run directly:

    python tests/test_carry.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402

from engine.config import EngineConfig, apply_carry_preset, validate  # noqa: E402
from engine.indicators import IndicatorSet  # noqa: E402
from engine.instruments import get_instrument  # noqa: E402
from engine.macro import MacroSettings, MacroView, PolicyRate, PolicyRateSeries  # noqa: E402
from engine.position import FLAT, LONG, SHORT  # noqa: E402
from engine.strategy import (  # noqa: E402
    AdvisorSettings, CarrySettings, CarrySignal, SignalContext, TrendSignal, build_signal,
)

EUR = get_instrument("EUR/USD")
JPY = get_instrument("USD/JPY")


def utc(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


def dated_view():
    """USD cutting through the window; JPY near zero throughout."""
    view = MacroView(MacroSettings(carry_min_differential=1.0, carry_max_differential=5.0))
    for eff, rate, infl in (("2024-01-01", 5.00, 3.0), ("2025-01-01", 4.00, 2.5),
                            ("2026-01-01", 3.00, 2.0)):
        view.set_policy_rate("USD", rate, inflation=infl, effective=eff)
    for eff, rate, infl in (("2024-01-01", 0.10, 2.0), ("2025-06-01", 0.50, 2.5)):
        view.set_policy_rate("JPY", rate, inflation=infl, effective=eff)
    for eff, rate, infl in (("2024-01-01", 4.00, 2.4), ("2025-03-01", 2.50, 2.0)):
        view.set_policy_rate("EUR", rate, inflation=infl, effective=eff)
    return view


# --- the lookahead guard ----------------------------------------------------

def test_lookup_returns_the_rate_in_force_not_the_latest():
    """The whole point. A 2024 bar must see the 2024 rate."""
    view = dated_view()
    assert close(view.rate_asof("USD", utc(2024, 6, 1)).rate, 5.00)
    assert close(view.rate_asof("USD", utc(2025, 6, 1)).rate, 4.00)
    assert close(view.rate_asof("USD", utc(2026, 6, 1)).rate, 3.00)


def test_a_bar_predating_the_history_refuses():
    """No rate yet means no decision, not the earliest rate stretched backwards."""
    view = dated_view()
    assert view.rate_asof("USD", utc(2023, 6, 1)) is None
    assert view.differential(EUR, asof=utc(2023, 6, 1)) is None

    side, strength, reason = view.carry_bias(EUR, asof=utc(2023, 6, 1))
    assert side == FLAT and close(strength, 0.0)
    assert reason.startswith("no_rate_asof:"), reason


def test_an_undated_rate_cannot_answer_a_dated_query():
    """The guard that makes the default table safe rather than merely wrong.

    Undated placeholder rates must refuse every dated query. If they answered,
    every historical bar would silently see today's rate.
    """
    view = MacroView()
    view.set_policy_rate("USD", 4.00, inflation=2.5)      # no effective date
    view.set_policy_rate("JPY", 0.50, inflation=2.8)

    assert view.rate_asof("USD", utc(2025, 6, 1)) is None, \
        "an undated rate must not satisfy a dated lookup"
    assert view.differential(JPY, asof=utc(2025, 6, 1)) is None

    # It remains readable for inspection, where no date is claimed.
    assert view.rate_asof("USD", None) is not None
    assert view.differential(JPY, asof=None) is not None


def test_dated_and_undated_points_coexist_without_the_undated_winning():
    """A newer undated point must never shadow the dated history."""
    series = PolicyRateSeries("USD")
    series.add(PolicyRate("USD", 5.0, effective="2024-01-01"))
    series.add(PolicyRate("USD", 99.0))                   # undated, absurd value

    asof = series.asof(utc(2025, 1, 1))
    assert asof is not None and close(asof.rate, 5.0), \
        "the dated point must answer a dated query, not the undated one"


def test_coverage_reports_undated_currencies():
    """Startup must be able to say which currencies will refuse, and from when."""
    view = dated_view()
    view.set_policy_rate("GBP", 4.0, inflation=3.0)       # undated on purpose

    coverage = view.coverage(["EUR/USD", "GBP/USD"])
    assert coverage["USD"] is not None
    assert coverage["EUR"] is not None
    assert coverage["GBP"] is None, "an undated currency must report no coverage"


def test_loading_a_rate_file_round_trips(tmp_path=None):
    """The shipped example file must actually load and be queryable."""
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "policy_rates.example.json"
    view = MacroView()
    count = view.load_policy_rates(path)

    assert count > 0, "no rate observations loaded"
    assert "_README" not in view.policy, "underscore keys must be skipped"
    point = view.rate_asof("USD", utc(2025, 6, 1))
    assert point is not None and point.effective_dt is not None


# --- direction --------------------------------------------------------------

def test_carry_points_at_the_higher_real_yielder_as_of_the_bar():
    """Direction follows the differential in force at that moment."""
    view = dated_view()

    # 2024: USD real 2.0 vs JPY real -1.9 -> long USD/JPY.
    side, _s, reason = view.carry_bias(JPY, asof=utc(2024, 6, 1))
    assert side == LONG, reason

    # EUR/USD 2024: EUR real 1.6 vs USD real 2.0 -> -0.4, inside the threshold.
    side, _s, reason = view.carry_bias(EUR, asof=utc(2024, 6, 1))
    assert side == FLAT, reason

    # EUR/USD 2025: EUR real 0.5 vs USD real 1.5 -> -1.0 -> short EUR/USD.
    side, _s, reason = view.carry_bias(EUR, asof=utc(2025, 6, 1))
    assert side == SHORT, reason


def test_the_differential_changing_flips_the_direction():
    """A rate decision mid-window must change the signal after it, not before."""
    view = MacroView(MacroSettings(carry_min_differential=0.5))
    view.set_policy_rate("USD", 5.0, inflation=2.0, effective="2024-01-01")
    view.set_policy_rate("JPY", 0.0, inflation=1.0, effective="2024-01-01")
    view.set_policy_rate("JPY", 8.0, inflation=1.0, effective="2025-01-01")

    assert view.carry_bias(JPY, asof=utc(2024, 6, 1))[0] == LONG
    assert view.carry_bias(JPY, asof=utc(2025, 6, 1))[0] == SHORT


# --- the signal -------------------------------------------------------------

def warm_indicators(bars=60, price=150.0, inst=JPY):
    ind = IndicatorSet(fast_ema=5, slow_ema=10, rsi_period=5,
                       macd_fast=3, macd_slow=6, macd_signal=3, atr_period=5)
    for i in range(bars):
        p = price + i * inst.pip_size * 10
        ind.update(p + inst.pip_size * 20, p - inst.pip_size * 20, p)
    return ind


def context(inst=JPY, now=None, macro=None, last_exit=None, advisors=None):
    return SignalContext(
        inst=inst,
        indicators=warm_indicators(inst=inst),
        now=now or utc(2024, 6, 3),
        advisors=advisors or AdvisorSettings(min_atr_pips=0.1),
        macro=macro or dated_view(),
        last_exit_utc=last_exit,
    )


def test_carry_signal_emits_the_differential_direction():
    signal = CarrySignal(CarrySettings(reentry_cooldown_hours=0.0))
    sig = signal.evaluate_gated(context())

    assert sig.actionable and sig.side == LONG, sig
    assert "carry" in sig.reason


def test_carry_signal_refuses_when_the_bar_predates_the_rates():
    signal = CarrySignal(CarrySettings(reentry_cooldown_hours=0.0))
    sig = signal.evaluate_gated(context(now=utc(2023, 1, 5)))

    assert sig.side == FLAT
    assert sig.reason.startswith("no_rate_asof:"), sig.reason


def test_reentry_cooldown_blocks_immediately_reopening_after_a_stop():
    """Without this the stop becomes a fee paid every bar.

    A carry signal is persistent: being stopped out does not change the rate
    differential, so the next bar would re-open the identical position.
    """
    signal = CarrySignal(CarrySettings(reentry_cooldown_hours=72.0))
    now = utc(2024, 6, 3)

    just_stopped = signal.evaluate_gated(context(now=now, last_exit=now - timedelta(hours=1)))
    assert just_stopped.side == FLAT
    assert just_stopped.reason.startswith("cooldown:"), just_stopped.reason

    later = signal.evaluate_gated(context(now=now, last_exit=now - timedelta(hours=100)))
    assert later.actionable, later


def test_cooldown_default_is_not_zero():
    """A zero default would make the protection opt-in, which is backwards."""
    assert CarrySettings().reentry_cooldown_hours > 0


def test_carry_ignores_rsi_and_macd():
    """Applying trend gates would turn carry into a trend strategy in disguise.

    The advisors here are set to veto everything a trend signal would offer;
    carry must be unaffected.
    """
    hostile = AdvisorSettings(enable_rsi=True, rsi_long_max=0.0, rsi_short_min=100.0,
                              enable_macd=True, macd_long_min=9e9, macd_short_max=-9e9,
                              min_atr_pips=0.1)
    signal = CarrySignal(CarrySettings(reentry_cooldown_hours=0.0))
    sig = signal.evaluate_gated(context(advisors=hostile))

    assert sig.actionable, "RSI/MACD must not gate a carry entry: %r" % (sig,)

    # The same advisors do veto the trend signal, proving they were hostile.
    ctx = context(advisors=hostile)
    trend = TrendSignal()
    trend_sig = trend.evaluate_gated(ctx)
    trend_sig = trend.evaluate_gated(ctx)      # second bar confirms the streak
    assert trend_sig.vetoed_by, "the advisors should have vetoed the trend signal"


def test_carry_still_respects_the_atr_floor():
    """The one gate that applies, because stop distance is derived from ATR."""
    signal = CarrySignal(CarrySettings(reentry_cooldown_hours=0.0, require_atr_floor=True))
    sig = signal.evaluate_gated(context(advisors=AdvisorSettings(min_atr_pips=10_000.0)))

    assert sig.vetoed_by == "atr_below_floor", sig


# --- wiring -----------------------------------------------------------------

def test_build_signal_selects_the_strategy():
    assert isinstance(build_signal("carry", None, CarrySettings()), CarrySignal)
    assert isinstance(build_signal("trend", None, None), TrendSignal)
    assert isinstance(build_signal(None, None, None), TrendSignal)
    try:
        build_signal("momentum", None, None)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown strategy must raise, not silently default")


def test_carry_preset_disables_the_exits_that_would_destroy_carry():
    """Running carry with trend exits looks like a strategy failure.

    Each of these closes a carry position for doing exactly what carry does:
    holding, slowly, through price going nowhere, across the rollovers that
    pay it.
    """
    cfg = EngineConfig()
    cfg.strategy = "carry"
    apply_carry_preset(cfg)
    validate(cfg)

    assert cfg.exits.enable_max_hold is False, "carry pays out over weeks"
    assert cfg.exits.enable_stagnation is False, "a flat carry position is the thesis"
    assert cfg.exits.close_before_weekend is False, "flattening forfeits the roll"
    assert cfg.exits.enable_trailing is False
    assert cfg.exits.atr_stop_mult >= 4.0, "carry needs room to breathe"
    assert cfg.advisors.enable_rsi is False and cfg.advisors.enable_macd is False


def test_the_preset_never_removes_the_stop():
    """Carry's real risk is the violent unwind. That is what a stop is for."""
    cfg = EngineConfig()
    apply_carry_preset(cfg)
    validate(cfg)

    assert cfg.exits.min_stop_pips > 0
    assert cfg.exits.max_stop_pips > cfg.exits.min_stop_pips
    assert not hasattr(cfg.exits, "enable_stop"), "no stop toggle may exist"


def test_carry_config_round_trips_through_json(tmp_path=None):
    import tempfile, pathlib

    cfg = EngineConfig()
    cfg.strategy = "carry"
    cfg.carry.reentry_cooldown_hours = 48.0
    apply_carry_preset(cfg)
    validate(cfg)

    path = pathlib.Path(tempfile.mkdtemp()) / "carry.json"
    cfg.save(str(path))
    restored = EngineConfig.load(str(path))

    assert restored.strategy == "carry"
    assert close(restored.carry.reentry_cooldown_hours, 48.0)
    assert restored.exits.enable_stagnation is False


if __name__ == "__main__":
    raise SystemExit(run(globals()))
