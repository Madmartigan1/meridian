"""Carry bias, data surprise, and the release blackout.

This layer holds the drivers that actually move a currency pair -- rate
differentials, inflation, growth -- and the one thing about them that is
routinely misunderstood: price responds to the gap between the actual print
and the forecast already in the price, not to the level of the number.

The tests pin the two properties that keep the layer honest:

  * the blackout gate blocks entries around high-impact releases for *both*
    legs of a pair, not only the base;
  * macro can shrink a trade or refuse one, but has no path to enlarge one. A
    macro view is a prior, not an edge.

Run directly:

    python tests/test_macro.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.instruments import get_instrument  # noqa: E402
from engine.macro import (  # noqa: E402
    HIGH, LOW, EconomicEvent, MacroSettings, MacroView, default_macro_view,
)
from engine.position import LONG, SHORT  # noqa: E402
from engine.strategy import FLAT  # noqa: E402

EUR = get_instrument("EUR/USD")
JPY = get_instrument("USD/JPY")
NOW = datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)


def view_with_rates(dated=True):
    """Rates effective well before NOW.

    They are dated because `MacroView.evaluate` queries as of the bar being
    processed, and an undated rate is refused there by design -- see
    `test_undated_rates_report_no_carry_bias` for that path.
    """
    effective = "2020-01-01" if dated else None
    view = MacroView(MacroSettings(carry_min_differential=1.0))
    view.set_policy_rate("USD", 4.00, inflation=2.5, effective=effective)
    view.set_policy_rate("EUR", 2.00, inflation=2.0, effective=effective)
    view.set_policy_rate("JPY", 0.50, inflation=2.5, effective=effective)
    return view


def test_undated_rates_report_no_carry_bias():
    """An undated table must not influence a dated decision.

    `evaluate` asks as of the bar's timestamp, so placeholder rates with no
    effective date contribute nothing rather than silently applying today's
    differential to a historical bar.
    """
    view = view_with_rates(dated=False)
    allowed, bias, reason = view.evaluate(JPY, LONG, NOW)

    assert allowed, "an undated rate is a missing opinion, not a veto"
    assert close(bias, 0.0), "undated rates must contribute no bias: %r" % reason


def test_differential_is_base_minus_quote():
    view = view_with_rates()
    # Real rates: USD 1.5, EUR 0.0 -> EUR/USD differential is -1.5.
    assert close(view.differential(EUR, asof=NOW), -1.5, tol=1e-9)
    # USD/JPY: USD 1.5, JPY -2.0 -> +3.5.
    assert close(view.differential(JPY, asof=NOW), 3.5, tol=1e-9)


def test_real_rate_prefers_purchasing_power_over_headline_yield():
    """A high nominal rate with higher inflation is debasement, not yield."""
    view = MacroView()
    view.set_policy_rate("AAA", 10.0, inflation=12.0, effective="2020-01-01")  # real -2.0
    view.set_policy_rate("BBB", 3.0, inflation=1.0, effective="2020-01-01")    # real +2.0
    pair = type(EUR)(symbol="AAA/BBB", pip_size=0.0001, price_step=0.00001)

    assert view.differential(pair, asof=NOW, use_real=True) < 0, "real rates favour BBB"
    assert view.differential(pair, asof=NOW, use_real=False) > 0, "nominal rates mislead"


def test_carry_points_at_the_higher_real_yielder():
    view = view_with_rates()
    side, strength, reason = view.carry_bias(JPY, asof=NOW)
    assert side == LONG, reason           # long USD/JPY earns the differential
    assert 0.0 < strength <= 1.0

    side, _, reason = view.carry_bias(EUR, asof=NOW)
    assert side == SHORT, reason          # long EUR/USD pays it


def test_a_small_differential_produces_no_bias():
    view = MacroView(MacroSettings(carry_min_differential=1.0))
    view.set_policy_rate("USD", 3.00, inflation=2.0, effective="2020-01-01")
    view.set_policy_rate("EUR", 2.75, inflation=2.0, effective="2020-01-01")

    side, strength, _ = view.carry_bias(EUR, asof=NOW)
    assert side == FLAT and close(strength, 0.0)


def test_surprise_is_actual_against_forecast_not_the_level():
    """A hot print against a hot forecast is not news."""
    priced_in = EconomicEvent(when_utc=NOW.isoformat(), currency="USD",
                              name="CPI", actual=3.5, forecast=3.5)
    assert close(priced_in.surprise, 0.0)

    upside = EconomicEvent(when_utc=NOW.isoformat(), currency="USD",
                           name="CPI", actual=3.5, forecast=3.0)
    assert close(upside.surprise, 0.5)

    # For unemployment, a higher number is weaker, so the sign flips.
    jobless = EconomicEvent(when_utc=NOW.isoformat(), currency="USD", name="Unemployment",
                            actual=4.5, forecast=4.0, higher_is_stronger=False)
    assert close(jobless.surprise, -0.5)


def test_surprise_sign_depends_on_which_leg_the_currency_is():
    """A strong dollar print is bearish EUR/USD and bullish USD/JPY."""
    view = MacroView(MacroSettings(surprise_decay_hours=12.0))
    view.add_event(EconomicEvent(
        when_utc=(NOW - timedelta(hours=1)).isoformat(), currency="USD",
        name="Payrolls", actual=250.0, forecast=180.0))

    eur_side, _, _ = view.surprise_bias(EUR, NOW)
    jpy_side, _, _ = view.surprise_bias(JPY, NOW)
    assert eur_side == SHORT, "USD is the quote leg of EUR/USD"
    assert jpy_side == LONG, "USD is the base leg of USD/JPY"


def test_stale_surprises_decay_out():
    view = MacroView(MacroSettings(surprise_decay_hours=12.0))
    view.add_event(EconomicEvent(
        when_utc=(NOW - timedelta(hours=48)).isoformat(), currency="USD",
        name="Payrolls", actual=250.0, forecast=180.0))

    side, strength, _ = view.surprise_bias(EUR, NOW)
    assert side == FLAT and close(strength, 0.0), "two-day-old data is priced in"


def test_blackout_blocks_entries_around_a_high_impact_release():
    """Spreads widen and the first move often reverses; the engine stands aside."""
    settings = MacroSettings(block_before_minutes=30, block_after_minutes=15)
    view = MacroView(settings)
    release = NOW + timedelta(minutes=10)
    view.add_event(EconomicEvent(when_utc=release.isoformat(), currency="USD",
                                 name="FOMC", impact=HIGH))

    allowed, _, reason = view.evaluate(EUR, LONG, NOW)
    assert not allowed and "blackout" in reason, reason

    # Well before the window, trading is fine.
    allowed, _, _ = view.evaluate(EUR, LONG, NOW - timedelta(hours=3))
    assert allowed

    # And after it has passed.
    allowed, _, _ = view.evaluate(EUR, LONG, release + timedelta(minutes=30))
    assert allowed


def test_blackout_applies_to_both_legs_and_only_relevant_pairs():
    settings = MacroSettings(block_before_minutes=30, block_after_minutes=15)
    view = MacroView(settings)
    view.add_event(EconomicEvent(when_utc=(NOW + timedelta(minutes=5)).isoformat(),
                                 currency="EUR", name="ECB", impact=HIGH))

    assert view.blocking_event(EUR, NOW) is not None, "EUR is the base leg"
    assert view.blocking_event(JPY, NOW) is None, "USD/JPY touches neither EUR nor ECB"


def test_low_impact_events_do_not_block():
    settings = MacroSettings(block_impact=(HIGH,))
    view = MacroView(settings)
    view.add_event(EconomicEvent(when_utc=(NOW + timedelta(minutes=5)).isoformat(),
                                 currency="USD", name="Consumer sentiment", impact=LOW))
    assert view.blocking_event(EUR, NOW) is None


def test_carry_veto_is_opt_in():
    view = view_with_rates()
    view.settings.carry_max_differential = 3.5     # makes USD/JPY carry strength 1.0

    view.settings.carry_veto_opposing = False
    allowed, bias, _ = view.evaluate(JPY, SHORT, NOW)
    assert allowed, "by default a technical signal may trade against carry"
    assert bias < 0, "but the disagreement is reported"

    view.settings.carry_veto_opposing = True
    allowed, _, reason = view.evaluate(JPY, SHORT, NOW)
    assert not allowed and "carry" in reason, reason


def test_agreement_is_reported_as_a_positive_bias():
    view = view_with_rates()
    allowed, bias, _ = view.evaluate(JPY, LONG, NOW)
    assert allowed and bias > 0, bias


def test_disabling_the_layer_allows_everything():
    view = default_macro_view()
    view.settings = MacroSettings(enabled=False)
    view.add_event(EconomicEvent(when_utc=NOW.isoformat(), currency="USD",
                                 name="FOMC", impact=HIGH))

    allowed, bias, _ = view.evaluate(EUR, LONG, NOW)
    assert allowed and close(bias, 0.0)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
