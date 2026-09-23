"""Pip, lot and pip-value arithmetic.

These are the numbers every other number is built from, and they are the ones a
run that "works" will never catch being wrong: a mis-sized pip produces a
plausible-looking position that risks the wrong amount, silently, forever.

Three traps in particular:

  1. JPY-quoted pairs use a 0.01 pip, not 0.0001. A stop of "20 pips" is a
     different price distance on USD/JPY than on EUR/USD by a factor of 100.
  2. Pip value depends on the *quote* currency and the *account* currency. A
     USD account trading EUR/GBP earns GBP and needs a GBP->USD leg.
  3. Lot rounding must round *down* and must return zero below the minimum
     ticket, never round up into a size larger than the risk budget allows.

Run directly, no test framework required:

    python tests/test_instruments.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.instruments import (
    RateBook, get_instrument, make_instrument, pip_value_per_lot,
    pips_to_price, price_to_pips, round_lots, round_price,
)


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_pip_size_by_quote_convention():
    """JPY quotes use a 0.01 pip; everything else uses 0.0001."""
    assert close(get_instrument("EUR/USD").pip_size, 0.0001)
    assert close(get_instrument("GBP/USD").pip_size, 0.0001)
    assert close(get_instrument("USD/JPY").pip_size, 0.01)
    assert close(get_instrument("EUR/JPY").pip_size, 0.01)

    # A pair added later must pick up the convention automatically, not need
    # someone to remember the JPY exception.
    assert close(make_instrument("CAD/JPY").pip_size, 0.01)
    assert close(make_instrument("NZD/CAD").pip_size, 0.0001)


def test_pips_to_price_and_back():
    """The conversion must round-trip on both conventions."""
    eur = get_instrument("EUR/USD")
    jpy = get_instrument("USD/JPY")

    assert close(pips_to_price(eur, 20), 0.0020)
    assert close(pips_to_price(jpy, 20), 0.20)

    # The same 20 pips is a 100x different price distance. This is the error
    # that makes a JPY stop either 100x too tight or 100x too wide.
    assert not close(pips_to_price(eur, 20), pips_to_price(jpy, 20))

    assert close(price_to_pips(eur, 0.0035), 35.0, tol=1e-6)
    assert close(price_to_pips(jpy, 0.35), 35.0, tol=1e-6)

    for inst in (eur, jpy):
        for pips in (0.5, 7.0, 13.4, 250.0):
            assert close(price_to_pips(inst, pips_to_price(inst, pips)), pips, tol=1e-6)


def test_pip_value_quote_equals_account():
    """USD account, USD-quoted pair: one pip on one lot is exactly 10 USD."""
    rates = RateBook("USD", {"EUR/USD": 1.0850})
    assert close(pip_value_per_lot(get_instrument("EUR/USD"), rates), 10.0, tol=1e-9)


def test_pip_value_jpy_quote_needs_conversion():
    """USD account, JPY-quoted pair: 1000 JPY per pip, converted at the rate.

    This is the case where using 10.0 as a universal pip value -- the shortcut
    that works for every USD-quoted major -- overstates risk by roughly 50%.
    """
    rates = RateBook("USD", {"USD/JPY": 150.00})
    value = pip_value_per_lot(get_instrument("USD/JPY"), rates)
    # 0.01 * 100_000 = 1000 JPY, and JPY->USD is 1/150.
    assert close(value, 1000.0 / 150.0, tol=1e-9), value
    assert 6.0 < value < 7.0, value


def test_pip_value_cross_neither_leg_is_account_currency():
    """USD account, EUR/GBP: PnL lands in GBP and needs a GBP->USD leg.

    The rate book holds no GBP/USD-to-USD entry directly named for the cross,
    so this exercises the derivation path rather than a lookup.
    """
    rates = RateBook("USD", {"EUR/GBP": 0.8500, "GBP/USD": 1.2700})
    value = pip_value_per_lot(get_instrument("EUR/GBP"), rates)
    # 0.0001 * 100_000 = 10 GBP per pip, at 1.27 USD per GBP.
    assert close(value, 12.70, tol=1e-9), value


def test_pip_value_inverted_rate():
    """A GBP account must derive USD->GBP from a GBP/USD quote by inversion."""
    rates = RateBook("GBP", {"GBP/USD": 1.2500})
    value = pip_value_per_lot(get_instrument("EUR/USD"), rates)
    assert close(value, 10.0 / 1.25, tol=1e-9), value


def test_pip_value_refuses_without_a_rate():
    """No conversion leg must yield None, not a plausible default.

    Returning 10.0 here would size a trade against an unknown risk, which is
    strictly worse than refusing to trade.
    """
    rates = RateBook("USD", {})
    assert pip_value_per_lot(get_instrument("EUR/GBP"), rates) is None


def test_cross_rate_derives_through_the_dollar_legs():
    """EUR->GBP with only dollar pairs loaded must route through USD.

    The derivation walks the rate book looking for a one-hop path, so this pins
    that it finds the arithmetically correct route rather than whichever entry
    happens to be visited first.
    """
    rates = RateBook("USD", {"EUR/USD": 1.1000, "GBP/USD": 1.2500, "USD/JPY": 150.0})

    assert close(rates.rate("EUR", "GBP"), 1.1000 / 1.2500, tol=1e-12)
    assert close(rates.rate("GBP", "EUR"), 1.2500 / 1.1000, tol=1e-12)
    assert close(rates.rate("EUR", "JPY"), 1.1000 * 150.0, tol=1e-9)
    assert close(rates.rate("USD", "USD"), 1.0)


def test_a_quoted_rate_beats_a_derived_one():
    """A directly quoted pair takes precedence over a triangular derivation.

    The dollar legs here imply 0.88; the explicit quote says 0.8650. The market
    rate is the quoted one, and a stale triangulation must not override it.
    """
    rates = RateBook("USD", {"EUR/USD": 1.1000, "GBP/USD": 1.2500, "EUR/GBP": 0.8650})
    assert close(rates.rate("EUR", "GBP"), 0.8650, tol=1e-12)
    assert close(rates.rate("GBP", "EUR"), 1.0 / 0.8650, tol=1e-12)


def test_lot_rounding_at_the_minimum_boundary():
    """Rounds down to the lot step, and returns zero below the minimum ticket."""
    eur = get_instrument("EUR/USD")           # lot_step 0.01, min 0.01

    assert close(round_lots(eur, 0.0149), 0.01)
    assert close(round_lots(eur, 0.019), 0.01)
    assert close(round_lots(eur, 0.37777), 0.37)
    assert close(round_lots(eur, 1.0), 1.0)

    # Below the minimum ticket: zero, never rounded up into a real position.
    assert close(round_lots(eur, 0.009), 0.0)
    assert close(round_lots(eur, 0.0), 0.0)
    assert close(round_lots(eur, -5.0), 0.0)

    # Exactly on the boundary must survive float representation error.
    assert close(round_lots(eur, 0.01), 0.01)
    assert close(round_lots(eur, 0.03), 0.03)
    assert close(round_lots(eur, 0.07), 0.07)

    # And the broker ceiling is honoured.
    assert close(round_lots(eur, 500.0), eur.max_lots)


def test_price_rounding_uses_the_point_not_the_pip():
    """Prices snap to the quotable point, which is a tenth of a pip."""
    eur = get_instrument("EUR/USD")
    jpy = get_instrument("USD/JPY")
    assert close(round_price(eur, 1.08523456), 1.08523, tol=1e-9)
    assert close(round_price(jpy, 151.23456), 151.235, tol=1e-9)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print("  PASS  %s" % fn.__name__)
        except AssertionError as exc:
            failures += 1
            print("  FAIL  %s: %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001 - a crash is a failure too
            failures += 1
            print("  ERROR %s: %s: %s" % (fn.__name__, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
