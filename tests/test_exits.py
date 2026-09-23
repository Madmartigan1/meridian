"""Stop behaviour: precedence, tightening, and gap fills.

Leverage is what makes FX lethal rather than merely unprofitable, so the stop
rules get their own file. Three properties are pinned here because each one,
if broken, makes every simulated result look better than reality:

  1. When a bar contains both the stop and the take profit, the stop wins.
     Bar data cannot say which came first, and assuming the profit is the
     assumption that flatters.
  2. A trailing stop only ever tightens. A stop that can widen is not a stop.
  3. A gapped open fills at the open, not at the stop level. Modelling stops as
     exact fills is the single most flattering error a backtest can make, and
     weekend gaps are routine in FX.

Run directly:

    python tests/test_exits.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.execution import ExecutionSettings, stop_fill_price  # noqa: E402
from engine.exits import (  # noqa: E402
    ExitSettings, evaluate, initial_levels, stop_distance_pips, tighten_trailing_stop,
)
from engine.instruments import get_instrument  # noqa: E402
from engine.position import LONG, SHORT, PositionBook  # noqa: E402

EUR = get_instrument("EUR/USD")
JPY = get_instrument("USD/JPY")
MIDWEEK = datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)   # Wednesday, London


def open_position(side, entry, stop, tp=None, lots=1.0, inst=EUR):
    book = PositionBook()
    book.apply_fill(inst, side, lots, entry, when=MIDWEEK,
                    stop_price=stop, take_profit_price=tp)
    return book.get(inst.symbol)


def test_stop_distance_scales_with_volatility_inside_a_clamp():
    """ATR sets the distance; the clamp stops one quiet bar sizing the account."""
    s = ExitSettings(atr_stop_mult=2.0, min_stop_pips=8.0, max_stop_pips=80.0)

    assert close(stop_distance_pips(EUR, 0.0015, s), 30.0, tol=1e-6)   # 15 pips ATR
    assert close(stop_distance_pips(EUR, 0.0001, s), 8.0)              # floored
    assert close(stop_distance_pips(EUR, 0.0100, s), 80.0)             # capped
    assert close(stop_distance_pips(EUR, None, s), 8.0), "no ATR falls back to the floor"

    # The JPY convention must be honoured: 15 pips of USD/JPY is 0.15, not 0.0015.
    assert close(stop_distance_pips(JPY, 0.15, s), 30.0, tol=1e-6)


def test_initial_levels_sit_on_the_correct_side_for_each_direction():
    stop, tp = initial_levels(EUR, LONG, 1.1000, 20.0, ExitSettings(reward_risk=2.0))
    assert close(stop, 1.0980, tol=1e-9), stop
    assert close(tp, 1.1040, tol=1e-9), tp

    stop, tp = initial_levels(EUR, SHORT, 1.1000, 20.0, ExitSettings(reward_risk=2.0))
    assert close(stop, 1.1020, tol=1e-9), stop
    assert close(tp, 1.0960, tol=1e-9), tp


def test_long_stop_triggers_on_the_low_and_short_on_the_high():
    long_pos = open_position(LONG, 1.1000, 1.0980)
    assert long_pos.touches_stop(high=1.1050, low=1.0975)
    assert not long_pos.touches_stop(high=1.1050, low=1.0985)

    short_pos = open_position(SHORT, 1.1000, 1.1020)
    assert short_pos.touches_stop(high=1.1025, low=1.0950)
    assert not short_pos.touches_stop(high=1.1015, low=1.0950)


def test_stop_wins_when_a_bar_contains_both_levels():
    """The conservative assumption, because the bar cannot say which came first."""
    pos = open_position(LONG, 1.1000, 1.0980, tp=1.1040)
    decision = evaluate(EUR, pos, high=1.1050, low=1.0970, close=1.1000,
                        now_utc=MIDWEEK, settings=ExitSettings())

    assert decision.should_exit
    assert decision.is_stop, "a bar touching both must settle as the stop"
    assert close(decision.price, 1.0980)


def test_take_profit_fires_when_the_stop_is_untouched():
    pos = open_position(LONG, 1.1000, 1.0980, tp=1.1040)
    decision = evaluate(EUR, pos, high=1.1050, low=1.0995, close=1.1045,
                        now_utc=MIDWEEK, settings=ExitSettings())

    assert decision.should_exit and not decision.is_stop
    assert close(decision.price, 1.1040)


def test_trailing_stop_only_tightens():
    """Once moved up, a long's stop can never move back down."""
    settings = ExitSettings(enable_trailing=True, trail_start_r=1.0, trail_distance_r=0.5)
    pos = open_position(LONG, 1.1000, 1.0980)     # 20-pip risk

    assert not tighten_trailing_stop(EUR, pos, 1.1010, settings), "under 1R: no move"
    assert close(pos.stop_price, 1.0980)

    assert tighten_trailing_stop(EUR, pos, 1.1030, settings), "3R up: should trail"
    tightened = pos.stop_price
    assert tightened > 1.0980, tightened

    # Price falls back: the stop must hold where it is.
    assert not tighten_trailing_stop(EUR, pos, 1.1005, settings)
    assert close(pos.stop_price, tightened), "a trailing stop must never widen"


def test_trailing_r_is_the_entry_stop_not_the_trailed_one():
    """R is fixed at entry, so the trail stays a constant distance behind peak.

    Regression: R was re-derived from the *current* stop, which the trail
    itself had just moved. Each tighten shrank R, so the next tighten came in
    closer -- the trail accelerated toward price and `trail_distance_r` stopped
    describing anything. The stop must sit at exactly (peak - 0.8R) throughout.
    """
    settings = ExitSettings(enable_trailing=True, trail_start_r=1.0, trail_distance_r=0.8)
    pos = open_position(LONG, 1.1000, 1.0980)      # R = 20 pips
    assert close(pos.risk_pips(EUR), 20.0, tol=1e-6)

    for price, peak in ((1.1030, 30.0), (1.1050, 50.0), (1.1070, 70.0)):
        tighten_trailing_stop(EUR, pos, price, settings)
        locked_pips = (pos.stop_price - pos.entry_price) * 10_000
        expected = peak - 0.8 * 20.0        # 16 pips behind the peak, always
        assert close(locked_pips, expected, tol=0.01), (price, locked_pips, expected)
        assert close(pos.risk_pips(EUR), 20.0, tol=1e-6), "R must not drift"


def test_risk_pips_survives_state_written_before_the_field_existed():
    """A restored position with no stored R falls back to its current distance."""
    pos = open_position(LONG, 1.1000, 1.0980)
    pos.initial_stop_pips = 0.0
    assert close(pos.risk_pips(EUR), 20.0, tol=1e-6)


def test_trailing_stop_tightens_downward_for_a_short():
    settings = ExitSettings(enable_trailing=True, trail_start_r=1.0, trail_distance_r=0.5)
    pos = open_position(SHORT, 1.1000, 1.1020)    # 20-pip risk

    assert tighten_trailing_stop(EUR, pos, 1.0960, settings)
    assert pos.stop_price < 1.1020

    tightened = pos.stop_price
    assert not tighten_trailing_stop(EUR, pos, 1.0995, settings)
    assert close(pos.stop_price, tightened)


def test_gapped_open_fills_worse_than_the_stop_level():
    """A Sunday gap through a long's stop fills at the open, not the stop."""
    settings = ExecutionSettings(stop_slippage_pips=1.0)

    # Bar opens 30 pips below a long's stop.
    price = stop_fill_price(EUR, LONG, stop_price=1.0980, bar_open=1.0950, settings=settings)
    assert price < 1.0980, price
    assert close(price, 1.0949, tol=1e-9), price

    # No gap: the stop level, minus the usual adverse slip.
    price = stop_fill_price(EUR, LONG, stop_price=1.0980, bar_open=1.1010, settings=settings)
    assert close(price, 1.0979, tol=1e-9), price

    # A short gaps the other way.
    price = stop_fill_price(EUR, SHORT, stop_price=1.1020, bar_open=1.1060, settings=settings)
    assert price > 1.1020, price
    assert close(price, 1.1061, tol=1e-9), price


def test_weekend_flat_closes_before_the_friday_close():
    """Gap risk cannot be stopped out of, so the engine stands aside."""
    friday_late = datetime(2026, 1, 16, 20, 30, tzinfo=timezone.utc)   # 15:30 NY
    pos = open_position(LONG, 1.1000, 1.0980)
    decision = evaluate(EUR, pos, high=1.1005, low=1.0995, close=1.1000,
                        now_utc=friday_late, settings=ExitSettings(close_before_weekend=True))

    assert decision.should_exit and "weekend" in decision.reason

    decision = evaluate(EUR, pos, high=1.1005, low=1.0995, close=1.1000,
                        now_utc=friday_late, settings=ExitSettings(close_before_weekend=False))
    assert not decision.should_exit, "the setting must actually be respected"


def test_max_hold_counts_market_hours_not_wall_clock():
    """A position opened Friday must not expire over a shut weekend."""
    settings = ExitSettings(max_hold_hours=48.0, enable_stagnation=False,
                            close_before_weekend=False)
    book = PositionBook()
    friday = datetime(2026, 1, 16, 12, 0, tzinfo=timezone.utc)
    book.apply_fill(EUR, LONG, 1.0, 1.1000, when=friday, stop_price=1.0980)
    pos = book.get("EUR/USD")

    monday = datetime(2026, 1, 19, 12, 0, tzinfo=timezone.utc)   # 72 wall-clock hours
    decision = evaluate(EUR, pos, 1.1005, 1.0995, 1.1000, monday, settings)
    assert not decision.should_exit, \
        "72 wall-clock hours is about 24 market hours; the timer must not fire"

    thursday = datetime(2026, 1, 22, 12, 0, tzinfo=timezone.utc)
    decision = evaluate(EUR, pos, 1.1005, 1.0995, 1.1000, thursday, settings)
    assert decision.should_exit and "max hold" in decision.reason


def test_stagnation_closes_a_position_going_nowhere():
    settings = ExitSettings(enable_stagnation=True, stagnation_bars=10,
                            stagnation_pips=3.0, close_before_weekend=False,
                            enable_max_hold=False)
    pos = open_position(LONG, 1.1000, 1.0980)
    pos.bars_held = 12

    decision = evaluate(EUR, pos, 1.1002, 1.0999, 1.1001, MIDWEEK, settings)
    assert decision.should_exit and "stagnant" in decision.reason

    pos.bars_held = 12
    decision = evaluate(EUR, pos, 1.1010, 1.0999, 1.1008, MIDWEEK, settings)
    assert not decision.should_exit, "8 pips of progress is not stagnant"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
