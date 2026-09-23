"""Signed position netting: partial closes, adds, and flips.

This is the file that separates an FX engine from a spot one. In spot, SELL
means exit and the size can never go below zero. In FX, SELL is a direction,
and a fill larger than the open position does not error -- it settles the old
leg and opens the opposite one. If netting is wrong here, every downstream
number (exposure, open risk, margin, PnL) is wrong in a way no smoke test
surfaces, because the engine keeps running happily on a corrupt book.

Run directly:

    python tests/test_position.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.instruments import get_instrument  # noqa: E402
from engine.position import LONG, SHORT, PositionBook  # noqa: E402

EUR = get_instrument("EUR/USD")
JPY = get_instrument("USD/JPY")


def test_open_long_and_short_are_both_first_class():
    """A short is an opening action, not an exit from nothing."""
    book = PositionBook()
    book.apply_fill(EUR, SHORT, 0.50, 1.0800, stop_price=1.0850)

    pos = book.get("EUR/USD")
    assert pos is not None
    assert pos.side == SHORT
    assert close(pos.lots, 0.50)
    assert pos.sign == -1
    assert close(pos.signed_units(EUR), -50_000.0)


def test_short_profits_when_price_falls():
    """Direction-aware PnL. A short is not a negative long with a sign bug."""
    book = PositionBook()
    book.apply_fill(EUR, SHORT, 1.0, 1.0800, stop_price=1.0850)
    pos = book.get("EUR/USD")

    assert close(pos.excursion_pips(EUR, 1.0780), 20.0, tol=1e-6)   # fell: profit
    assert close(pos.excursion_pips(EUR, 1.0820), -20.0, tol=1e-6)  # rose: loss

    book2 = PositionBook()
    book2.apply_fill(EUR, LONG, 1.0, 1.0800, stop_price=1.0750)
    long_pos = book2.get("EUR/USD")
    assert close(long_pos.excursion_pips(EUR, 1.0780), -20.0, tol=1e-6)


def test_adding_averages_the_entry_by_lots():
    """A same-direction fill sizes up at the lot-weighted average price."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    book.apply_fill(EUR, LONG, 3.0, 1.1100, stop_price=1.0950)

    pos = book.get("EUR/USD")
    assert close(pos.lots, 4.0)
    # (1 * 1.1000 + 3 * 1.1100) / 4
    assert close(pos.entry_price, 1.1075, tol=1e-9), pos.entry_price


def test_partial_close_settles_only_the_closed_slice():
    """Closing half returns one leg for half, and leaves half open."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 2.0, 1.0800, stop_price=1.0750)

    legs = book.close(EUR, 1.0850, lots=0.5, reason="scale out")
    assert len(legs) == 1
    leg = legs[0]
    assert close(leg.lots, 0.5)
    assert leg.side == LONG
    assert close(leg.pips, 50.0, tol=1e-6)

    pos = book.get("EUR/USD")
    assert pos is not None, "partial close must not remove the position"
    assert close(pos.lots, 1.5)
    assert close(pos.entry_price, 1.0800), "a partial close must not move the entry"


def test_full_close_removes_the_position():
    book = PositionBook()
    book.apply_fill(EUR, SHORT, 0.8, 1.0900, stop_price=1.0950)

    legs = book.close(EUR, 1.0870, reason="take profit")
    assert len(legs) == 1
    assert close(legs[0].pips, 30.0, tol=1e-6), "short gains when price falls"
    assert book.get("EUR/USD") is None
    assert len(book) == 0


def test_oversized_opposing_fill_flips_rather_than_going_negative():
    """The case a long-only model cannot express at all.

    Selling 3 lots against a 1-lot long settles the long and opens a 2-lot
    short. A spot-shaped book would either clamp at zero or record -2 lots of a
    position it still calls LONG.
    """
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)

    legs = book.apply_fill(EUR, SHORT, 3.0, 1.1050, stop_price=1.1100, reason="flip")
    assert len(legs) == 1
    assert legs[0].side == LONG
    assert close(legs[0].lots, 1.0)
    assert close(legs[0].pips, 50.0, tol=1e-6), "the long leg closed in profit"

    pos = book.get("EUR/USD")
    assert pos is not None
    assert pos.side == SHORT, "the remainder opens the other way"
    assert close(pos.lots, 2.0)
    assert close(pos.entry_price, 1.1050), "the new leg starts at the flip price"
    assert close(pos.stop_price, 1.1100)


def test_exact_opposing_fill_closes_without_opening_anything():
    """The boundary between a close and a flip must not open a dust position."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    legs = book.apply_fill(EUR, SHORT, 1.0, 1.1020, stop_price=1.1050)

    assert len(legs) == 1
    assert book.get("EUR/USD") is None, "an exact offset must leave the book flat"


def test_close_never_flips_even_when_oversized():
    """`close` is the exit path, and an exit must not be able to open a trade.

    A drifting size calculation must produce a full close, never a surprise
    reverse position that nothing in the engine asked for.
    """
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)

    legs = book.close(EUR, 1.1020, lots=99.0, reason="oversized exit")
    assert len(legs) == 1
    assert close(legs[0].lots, 1.0)
    assert book.get("EUR/USD") is None, "close must never leave a reversed position"


def test_opening_without_a_stop_is_refused():
    """The one non-negotiable rule, enforced at the book rather than by policy."""
    book = PositionBook()
    try:
        book.apply_fill(EUR, LONG, 1.0, 1.1000)
    except ValueError:
        pass
    else:
        raise AssertionError("opening without a stop price must raise")
    assert len(book) == 0


def test_swap_is_split_pro_rata_on_a_partial_close():
    """Financing follows the lots it was charged on."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 4.0, 1.1000, stop_price=1.0950)
    pos = book.get("EUR/USD")
    pos.swap_pips = -8.0

    legs = book.close(EUR, 1.1010, lots=1.0, reason="scale out")
    assert close(legs[0].swap_pips, -2.0, tol=1e-9), "one quarter of the position"
    assert close(book.get("EUR/USD").swap_pips, -6.0, tol=1e-9), "the rest stays open"


def test_jpy_pair_nets_in_its_own_pip_convention():
    """Netting must use the instrument's pip size, not a global constant."""
    book = PositionBook()
    book.apply_fill(JPY, LONG, 1.0, 150.00, stop_price=149.50)
    legs = book.close(JPY, 150.25, reason="take profit")
    assert close(legs[0].pips, 25.0, tol=1e-6), legs[0].pips


def test_book_round_trips_through_serialisation():
    """State must survive a restart with the direction and stop intact."""
    book = PositionBook()
    book.apply_fill(EUR, SHORT, 0.33, 1.0750, stop_price=1.0800,
                    take_profit_price=1.0650, reason="ema down")
    rows = book.to_list()

    restored = PositionBook()
    restored.load_list(rows)
    pos = restored.get("EUR/USD")
    assert pos is not None
    assert pos.side == SHORT
    assert close(pos.lots, 0.33)
    assert close(pos.stop_price, 1.0800)
    assert close(pos.take_profit_price, 1.0650)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
