"""Currency exposure netting across pairs.

The gate no per-instrument risk model can see. Three positions sized at "0.5%
each" look like 1.5% of diversified risk, but:

    long EUR/USD + long GBP/USD + short USD/JPY

is three ways to be short the US dollar, and one dollar headline settles all
three together. Aggregating exposure per currency is what turns that from an
invisible concentration into a refusal with a reason attached.

Run directly:

    python tests/test_risk_exposure.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runner import close, run  # noqa: E402
from engine.instruments import DEFAULT_INSTRUMENTS, RateBook, get_instrument  # noqa: E402
from engine.position import LONG, SHORT, PositionBook  # noqa: E402
from engine.risk import (  # noqa: E402
    RiskManager, RiskSettings, currency_exposure, open_risk_amount, projected_exposure,
)

EUR = get_instrument("EUR/USD")
GBP = get_instrument("GBP/USD")
JPY = get_instrument("USD/JPY")
LONDON = datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)   # Wednesday, liquid


def rates():
    return RateBook("USD", {"EUR/USD": 1.1000, "GBP/USD": 1.2700, "USD/JPY": 150.00})


def instruments():
    return {s: DEFAULT_INSTRUMENTS[s] for s in ("EUR/USD", "GBP/USD", "USD/JPY")}


def test_one_position_is_two_currency_exposures():
    """Long EUR/USD is long EUR and short USD by the same notional."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)

    exposure = currency_exposure(book, instruments(), rates())
    assert close(exposure["EUR"], 110_000.0, tol=1.0), exposure
    assert close(exposure["USD"], -110_000.0, tol=1.0), exposure


def test_a_short_reverses_both_legs():
    book = PositionBook()
    book.apply_fill(EUR, SHORT, 1.0, 1.1000, stop_price=1.1050)

    exposure = currency_exposure(book, instruments(), rates())
    assert exposure["EUR"] < 0
    assert exposure["USD"] > 0


def test_dollar_exposure_accumulates_across_apparently_different_trades():
    """The whole point: three 'different' pairs, one concentrated dollar bet."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    book.apply_fill(GBP, LONG, 1.0, 1.2700, stop_price=1.2650)
    book.apply_fill(JPY, SHORT, 1.0, 150.00, stop_price=151.00)

    exposure = currency_exposure(book, instruments(), rates())
    # Short USD from both majors, and short USD again from the USD/JPY short.
    assert exposure["USD"] < -300_000.0, exposure["USD"]
    assert exposure["EUR"] > 0 and exposure["GBP"] > 0 and exposure["JPY"] > 0


def test_opposing_pairs_net_rather_than_double_count():
    """Long EUR/USD and long USD/JPY partly cancel on the dollar leg."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    book.apply_fill(JPY, LONG, 1.0, 150.00, stop_price=149.50)

    exposure = currency_exposure(book, instruments(), rates())
    # -110,000 USD from the first, +100,000 USD from the second.
    assert close(exposure["USD"], -10_000.0, tol=1_000.0), exposure["USD"]


def test_projected_exposure_includes_the_trade_not_yet_taken():
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)

    before = currency_exposure(book, instruments(), rates())
    after = projected_exposure(book, instruments(), rates(), GBP, LONG, 1.0, 1.2700)

    assert after["USD"] < before["USD"], "adding a second dollar short must deepen it"
    assert "GBP" in after and after["GBP"] > 0


def test_the_gate_refuses_a_concentrating_entry():
    """A third dollar-short leg is refused with the currency named."""
    settings = RiskSettings(max_currency_exposure_pct=2.5, max_open_positions=10,
                            max_total_risk_pct=0.06, require_liquid_session=False)
    manager = RiskManager(settings)

    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    book.apply_fill(GBP, LONG, 1.0, 1.2700, stop_price=1.2650)

    ok, why = manager.allows_entry(
        JPY, SHORT, 1.0, 150.00, equity=100_000,
        book=book, instruments=instruments(), rates=rates(), now_utc=LONDON,
    )
    assert not ok
    assert why.startswith("currency_exposure:"), why
    assert "USD" in why, why


def test_the_gate_allows_a_diversifying_entry_of_the_same_size():
    """The same size in the opposite dollar direction reduces concentration."""
    settings = RiskSettings(max_currency_exposure_pct=2.5, max_open_positions=10,
                            max_total_risk_pct=0.06, require_liquid_session=False)
    manager = RiskManager(settings)

    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    book.apply_fill(GBP, LONG, 1.0, 1.2700, stop_price=1.2650)

    ok, why = manager.allows_entry(
        JPY, LONG, 1.0, 150.00, equity=100_000,
        book=book, instruments=instruments(), rates=rates(), now_utc=LONDON,
    )
    assert ok, why


def test_open_risk_ignores_positions_trailed_into_profit():
    """A locked-in gain is not risk budget to spend a second time."""
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)
    at_risk = open_risk_amount(book, instruments(), rates())
    assert at_risk > 0

    book.get("EUR/USD").stop_price = 1.1020      # trailed above entry
    assert close(open_risk_amount(book, instruments(), rates()), 0.0), \
        "a stop beyond entry has no downside left to count"


def test_daily_loss_limit_halts_further_entries():
    settings = RiskSettings(max_daily_loss_pct=0.03, require_liquid_session=False)
    manager = RiskManager(settings)

    manager.record_realised(-3_500.0, equity=100_000, now_utc=LONDON)
    ok, why = manager.allows_entry(
        EUR, LONG, 0.1, 1.1000, equity=100_000,
        book=PositionBook(), instruments=instruments(), rates=rates(), now_utc=LONDON,
    )
    assert not ok
    assert why.startswith("daily_halt:"), why


def test_illiquid_hours_are_refused_when_configured():
    manager = RiskManager(RiskSettings(require_liquid_session=True))
    tokyo = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)   # 21:00 New York

    ok, why = manager.allows_entry(
        EUR, LONG, 0.1, 1.1000, equity=100_000,
        book=PositionBook(), instruments=instruments(), rates=rates(), now_utc=tokyo,
    )
    assert not ok
    assert why.startswith("illiquid_session:"), why


def test_refusal_reasons_carry_a_stable_code():
    """Run reports aggregate on the code before the colon.

    Without it, every refusal that embeds a number is a unique string and the
    summary degenerates into a list of one-offs.
    """
    manager = RiskManager(RiskSettings(max_open_positions=1, require_liquid_session=False))
    book = PositionBook()
    book.apply_fill(EUR, LONG, 1.0, 1.1000, stop_price=1.0950)

    ok, why = manager.allows_entry(
        GBP, LONG, 0.1, 1.2700, equity=100_000,
        book=book, instruments=instruments(), rates=rates(), now_utc=LONDON,
    )
    assert not ok
    code = why.split(":")[0]
    assert code == "max_positions", why
    assert " " not in code, "the code must be a single token"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
