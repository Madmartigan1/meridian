"""Risk-based sizing and margin.

Sizing runs backwards from the stop: the loss is fixed, and the size is
whatever follows. The tests pin the property that matters -- the account-
currency amount at risk is the same regardless of pair, pip convention, or
stop distance -- because that invariant is what a "%-per-trade" risk model
actually means, and it is easy to break while every run still completes.

Run directly:

    python tests/test_sizing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.instruments import RateBook, get_instrument  # noqa: E402
from engine.sizing import margin_for, money_from_pips, size_for_risk  # noqa: E402

EUR = get_instrument("EUR/USD")
JPY = get_instrument("USD/JPY")
CROSS = get_instrument("EUR/GBP")


def test_size_risks_the_configured_amount():
    """1% of 10,000 over a 20-pip stop on EUR/USD is 0.50 lots risking 100."""
    rates = RateBook("USD", {"EUR/USD": 1.1000})
    result = size_for_risk(EUR, equity=10_000, risk_pct=0.01,
                           stop_distance_pips=20.0, rates=rates, price=1.1000)

    assert result.tradable
    assert close(result.lots, 0.50), result.lots
    assert close(result.risk_amount, 100.0, tol=0.01), result.risk_amount
    assert close(result.pip_value, 5.0, tol=0.01), result.pip_value


def test_a_wider_stop_produces_a_smaller_size_for_the_same_risk():
    """The whole point of the model: risk is constant, size absorbs volatility."""
    rates = RateBook("USD", {"EUR/USD": 1.1000})
    tight = size_for_risk(EUR, 10_000, 0.01, 10.0, rates, 1.1000)
    wide = size_for_risk(EUR, 10_000, 0.01, 40.0, rates, 1.1000)

    assert tight.lots > wide.lots
    assert close(tight.lots, 4 * wide.lots, tol=1e-6)
    assert close(tight.risk_amount, wide.risk_amount, tol=0.01), \
        "risk must not vary with stop distance"


def assert_risk_at_or_under_budget(result, budget, inst, rates, stop_pips, price):
    """Realised risk lands at the budget or just under it, never over.

    Lot rounding always rounds down, so the achieved risk is the budget minus
    at most one lot step's worth. Asserting exact equality would be asserting
    that rounding does not happen; asserting a loose tolerance would let a real
    over-risk through. The bound is the lot step itself.
    """
    from engine.instruments import pip_value_per_lot

    per_lot = pip_value_per_lot(inst, rates, price=price)
    step_risk = inst.lot_step * stop_pips * per_lot
    assert result.risk_amount <= budget + 1e-6, \
        "risked %.2f against a %.2f budget" % (result.risk_amount, budget)
    assert budget - result.risk_amount < step_risk + 1e-6, \
        "risked %.2f, more than one lot step under the %.2f budget" % (
            result.risk_amount, budget)


def test_risk_is_equal_across_pip_conventions():
    """A JPY pair and a USD pair must risk the same money for the same percent.

    If pip size were applied without its matching pip value, the JPY position
    would come out about 100x mis-sized while still looking like a normal
    number of lots.
    """
    rates = RateBook("USD", {"EUR/USD": 1.1000, "USD/JPY": 150.00})
    a = size_for_risk(EUR, 10_000, 0.01, 25.0, rates, 1.1000)
    b = size_for_risk(JPY, 10_000, 0.01, 25.0, rates, 150.00)

    assert a.tradable and b.tradable
    assert_risk_at_or_under_budget(a, 100.0, EUR, rates, 25.0, 1.1000)
    assert_risk_at_or_under_budget(b, 100.0, JPY, rates, 25.0, 150.00)
    assert not close(a.lots, b.lots, tol=0.05), "the lot counts should differ"


def test_risk_is_equal_on_a_cross_with_a_conversion_leg():
    """EUR/GBP from a USD account must still risk 1%, via the GBP->USD leg."""
    rates = RateBook("USD", {"EUR/GBP": 0.8500, "GBP/USD": 1.2700})
    result = size_for_risk(CROSS, 10_000, 0.01, 25.0, rates, 0.8500)

    assert result.tradable, result.reason
    assert_risk_at_or_under_budget(result, 100.0, CROSS, rates, 25.0, 0.8500)


def test_refuses_to_size_without_a_conversion_rate():
    """No rate means no known risk, so no trade -- not an assumed pip value."""
    rates = RateBook("USD", {})
    result = size_for_risk(CROSS, 10_000, 0.01, 25.0, rates, 0.8500)

    assert not result.tradable
    assert close(result.lots, 0.0)
    assert "conversion" in result.reason


def test_small_account_below_the_minimum_ticket_is_refused_not_rounded_up():
    """Rounding 0.004 lots up to the 0.01 minimum would risk 2.5x the budget."""
    rates = RateBook("USD", {"EUR/USD": 1.1000})
    result = size_for_risk(EUR, equity=100, risk_pct=0.005,
                           stop_distance_pips=30.0, rates=rates, price=1.1000)

    assert not result.tradable, "0.0167 lots must not become a real position"
    assert "minimum" in result.reason


def test_margin_limit_steps_the_size_down_rather_than_refusing():
    """Free margin is a ceiling on size, not an all-or-nothing gate."""
    rates = RateBook("USD", {"EUR/USD": 1.1000})
    unconstrained = size_for_risk(EUR, 100_000, 0.01, 10.0, rates, 1.1000, leverage=30.0)
    constrained = size_for_risk(EUR, 100_000, 0.01, 10.0, rates, 1.1000,
                                leverage=30.0, free_margin=1_000.0)

    assert unconstrained.lots > constrained.lots
    assert constrained.tradable
    assert constrained.margin_required <= 1_000.0 + 1e-6


def test_margin_uses_the_base_leg_not_the_quote_leg():
    """Notional is in base currency; pip value is in quote currency.

    Converting notional with the pip-value leg is a real and common bug, and on
    USD/JPY it misstates margin by a factor of 150.
    """
    rates = RateBook("USD", {"USD/JPY": 150.00})
    margin = margin_for(JPY, lots=1.0, price=150.00, rates=rates, leverage=30.0)
    # 100,000 USD of base at 30:1.
    assert close(margin, 100_000.0 / 30.0, tol=1e-6), margin


def test_money_from_pips_matches_the_sizing_pip_value():
    """The PnL path and the sizing path must agree, or the ledger drifts."""
    rates = RateBook("USD", {"USD/JPY": 150.00})
    result = size_for_risk(JPY, 10_000, 0.01, 20.0, rates, 150.00)
    loss = money_from_pips(JPY, -20.0, result.lots, rates, price=150.00)

    assert close(abs(loss), result.risk_amount, tol=0.01), (loss, result.risk_amount)


def test_zero_or_negative_inputs_are_refused():
    rates = RateBook("USD", {"EUR/USD": 1.1000})
    assert not size_for_risk(EUR, 0, 0.01, 20.0, rates, 1.1).tradable
    assert not size_for_risk(EUR, 10_000, 0.0, 20.0, rates, 1.1).tradable
    assert not size_for_risk(EUR, 10_000, 0.01, 0.0, rates, 1.1).tradable
    assert not size_for_risk(EUR, 10_000, 0.01, -5.0, rates, 1.1).tradable


if __name__ == "__main__":
    raise SystemExit(run(globals()))
