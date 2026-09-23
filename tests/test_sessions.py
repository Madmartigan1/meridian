"""The FX week: open hours, rollover weighting, and market-hour elapsed time.

A crypto-shaped engine assumes 24x7 and a single clock. FX needs three things
that assumption breaks:

  * the week is Sunday 17:00 to Friday 17:00 New York, so a max-hold timer
    that counts wall-clock hours expires positions over a weekend when no exit
    was possible;
  * rollover is at 17:00 New York, and Wednesday carries triple financing;
  * New York observes DST, so the UTC offset is not a constant.

The offsets are computed from the US DST rules rather than read from the tz
database, because a stock Windows Python has no tz database and the failure
mode would be a crash mid-run.

Run directly:

    python tests/test_sessions.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.sessions import (  # noqa: E402
    active_sessions, is_liquid_window, is_market_open, market_hours_between,
    ny_offset_hours, rollover_count, to_ny, weekend_gap_risk,
)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_new_york_offset_follows_dst():
    """-5 in winter, -4 in summer, without the tz database."""
    assert ny_offset_hours(utc(2026, 1, 15, 12)) == -5
    assert ny_offset_hours(utc(2026, 7, 15, 12)) == -4
    # Just inside each boundary: DST runs second Sunday of March to first
    # Sunday of November.
    assert ny_offset_hours(utc(2026, 3, 1, 12)) == -5
    assert ny_offset_hours(utc(2026, 3, 20, 12)) == -4
    assert ny_offset_hours(utc(2026, 11, 20, 12)) == -5


def test_to_ny_converts_the_wall_clock():
    ny = to_ny(utc(2026, 1, 15, 14, 30))
    assert (ny.hour, ny.minute) == (9, 30), ny


def test_week_opens_sunday_evening_and_closes_friday_evening():
    """2026-01-16 is a Friday; 2026-01-18 is the following Sunday."""
    assert is_market_open(utc(2026, 1, 16, 21)), "Friday 16:00 NY is open"
    assert not is_market_open(utc(2026, 1, 16, 23)), "Friday 18:00 NY is shut"
    assert not is_market_open(utc(2026, 1, 17, 12)), "Saturday is shut all day"
    assert not is_market_open(utc(2026, 1, 18, 21)), "Sunday 16:00 NY is still shut"
    assert is_market_open(utc(2026, 1, 18, 23)), "Sunday 18:00 NY has opened"
    assert is_market_open(utc(2026, 1, 21, 3)), "midweek overnight is open"


def test_wednesday_rollover_is_weighted_triple():
    """Wednesday's roll settles T+2 across the weekend, so it charges 3x.

    Getting this wrong understates the financing on every position held
    through midweek by two thirds.
    """
    # 2026-01-14 is a Wednesday. 21:00 UTC = 16:00 NY, 23:00 UTC = 18:00 NY.
    weight = rollover_count(utc(2026, 1, 14, 21), utc(2026, 1, 14, 23))
    assert weight == 3, weight

    # 2026-01-13 is the Tuesday before: a single ordinary roll.
    weight = rollover_count(utc(2026, 1, 13, 21), utc(2026, 1, 13, 23))
    assert weight == 1, weight


def test_rollover_counts_every_boundary_crossed():
    """Tuesday 16:00 NY to Wednesday 18:00 NY crosses two rolls: 1 + 3."""
    weight = rollover_count(utc(2026, 1, 13, 21), utc(2026, 1, 14, 23))
    assert weight == 4, weight


def test_no_rollover_when_no_boundary_is_crossed():
    assert rollover_count(utc(2026, 1, 14, 18), utc(2026, 1, 14, 21)) == 0
    assert rollover_count(None, utc(2026, 1, 14, 23)) == 0, \
        "the first bar of a run must not back-charge financing"
    assert rollover_count(utc(2026, 1, 14, 23), utc(2026, 1, 14, 21)) == 0, \
        "time going backwards must not charge anything"


def test_saturday_has_no_rollover():
    """The market is shut, so there is nothing to roll."""
    # Friday 18:00 NY through Saturday 20:00 NY crosses Saturday 17:00 only.
    weight = rollover_count(utc(2026, 1, 16, 23), utc(2026, 1, 18, 1))
    assert weight == 0, weight


def test_market_hours_exclude_the_weekend():
    """A Friday-to-Monday hold is about a day of market time, not three.

    This is what stops a 72-hour max-hold rule firing on a position that was
    only tradable for 24 of those hours.
    """
    wall_clock = 72.0
    held = market_hours_between(utc(2026, 1, 16, 12), utc(2026, 1, 19, 12))
    assert held < wall_clock / 2, held
    assert 20.0 < held < 28.0, held


def test_market_hours_within_a_single_session():
    held = market_hours_between(utc(2026, 1, 14, 10), utc(2026, 1, 14, 16))
    assert close(held, 6.0, tol=0.6), held


def test_liquidity_window_is_london_and_new_york():
    """09:00 New York is liquid; 21:00 New York (Tokyo) is not."""
    assert is_liquid_window(utc(2026, 1, 15, 14)), active_sessions(utc(2026, 1, 15, 14))
    assert not is_liquid_window(utc(2026, 1, 15, 2)), active_sessions(utc(2026, 1, 15, 2))


def test_sessions_that_wrap_midnight_are_detected():
    """Tokyo runs 19:00 to 04:00 New York, crossing the date line."""
    assert "TOKYO" in active_sessions(utc(2026, 1, 15, 2))    # 21:00 NY
    assert "TOKYO" in active_sessions(utc(2026, 1, 15, 7))    # 02:00 NY
    assert "TOKYO" not in active_sessions(utc(2026, 1, 15, 14))  # 09:00 NY


def test_financing_skips_a_position_opened_after_the_roll():
    """A position that was flat at 17:00 must not be billed for that rollover.

    Regression: SwapBook originally charged every position in the book for any
    rollover crossed since its last call, regardless of when the position was
    opened. On a Wednesday that billed three days of financing to a trade that
    had not existed yet.
    """
    from engine.instruments import get_instrument
    from engine.position import LONG, PositionBook
    from engine.sessions import SwapBook

    inst = get_instrument("USD/JPY")
    book = PositionBook()
    # Wednesday 2026-01-14: 21:00 UTC is 16:00 NY, 23:00 UTC is 18:00 NY.
    book.apply_fill(inst, LONG, 1.0, 150.0, when=utc(2026, 1, 14, 23),
                    stop_price=149.5)

    swaps = SwapBook()
    swaps.last_seen_utc = utc(2026, 1, 14, 21)
    applied = swaps.apply(book, {"USD/JPY": inst}, utc(2026, 1, 14, 23, 30))

    assert applied == {}, applied
    assert close(book.get("USD/JPY").swap_pips, 0.0)


def test_financing_charges_a_position_held_through_the_roll():
    """The same window, with the position actually open at 17:00, charges 3x."""
    from engine.instruments import get_instrument
    from engine.position import LONG, PositionBook
    from engine.sessions import SwapBook

    inst = get_instrument("USD/JPY")     # swap_long_pips_per_day = 0.30
    book = PositionBook()
    book.apply_fill(inst, LONG, 1.0, 150.0, when=utc(2026, 1, 14, 20),
                    stop_price=149.5)

    swaps = SwapBook()
    swaps.last_seen_utc = utc(2026, 1, 14, 21)
    applied = swaps.apply(book, {"USD/JPY": inst}, utc(2026, 1, 14, 23, 30))

    assert close(applied.get("USD/JPY", 0.0), 0.90, tol=1e-9), applied
    assert book.get("USD/JPY").rollovers == 3


def test_weekend_gap_risk_flags_the_friday_close():
    """Inside the last hours of the week, a stop can be gapped rather than hit."""
    assert weekend_gap_risk(utc(2026, 1, 16, 20, 30)), "15:30 NY Friday"
    assert not weekend_gap_risk(utc(2026, 1, 16, 12)), "07:00 NY Friday is fine"
    assert not weekend_gap_risk(utc(2026, 1, 14, 21)), "Wednesday is never flagged"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
