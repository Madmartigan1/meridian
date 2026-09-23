# engine/sizing.py
"""Risk-based position sizing and margin.

FX sizing runs backwards from the stop, not forwards from a notional amount:

    risk_amount = equity * risk_per_trade_pct
    lots        = risk_amount / (stop_distance_pips * pip_value_per_lot)

A fixed notional per trade -- the natural model for a spot engine -- means the
loss taken when a stop is hit varies with volatility, which is the opposite of
risk control. Here the loss is held constant by construction and the size is
whatever follows from it.

Every function returns a `SizingResult` carrying the refusal reason rather than
a bare number, because a size of zero must never be mistaken for a small trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .instruments import Instrument, RateBook, pip_value_per_lot, round_lots


@dataclass
class SizingResult:
    lots: float
    risk_amount: float = 0.0        # account currency actually at risk
    pip_value: float = 0.0          # account currency per pip, at the final size
    margin_required: float = 0.0
    reason: str = ""

    @property
    def tradable(self) -> bool:
        return self.lots > 0.0


def margin_for(
    inst: Instrument,
    lots: float,
    price: float,
    rates: RateBook,
    leverage: float,
) -> Optional[float]:
    """Margin in account currency for a position of `lots` at `price`.

    Notional is in base currency, so the conversion leg is base to account --
    a different leg from the pip-value conversion, which is quote to account.
    Conflating the two is a classic FX accounting bug.
    """
    if leverage <= 0:
        return None
    notional_base = inst.units(lots)
    conv = rates.rate(inst.base, rates.account_currency)
    if conv is None:
        # Fall back to pricing the base leg through the pair itself.
        quote_conv = rates.rate(inst.quote, rates.account_currency)
        if quote_conv is None or price <= 0:
            return None
        conv = price * quote_conv
    return (notional_base * conv) / float(leverage)


def size_for_risk(
    inst: Instrument,
    equity: float,
    risk_pct: float,
    stop_distance_pips: float,
    rates: RateBook,
    price: float,
    leverage: float = 30.0,
    free_margin: Optional[float] = None,
    max_lots: Optional[float] = None,
) -> SizingResult:
    """Lots to risk exactly `risk_pct` of equity over `stop_distance_pips`."""
    if equity <= 0:
        return SizingResult(0.0, reason="no equity")
    if risk_pct <= 0:
        return SizingResult(0.0, reason="risk_per_trade_pct is zero")
    if stop_distance_pips <= 0:
        return SizingResult(0.0, reason="stop distance must be positive")

    per_lot = pip_value_per_lot(inst, rates, price=price)
    if per_lot is None or per_lot <= 0:
        # Refuse rather than guess: an assumed conversion rate produces a size
        # that looks reasonable and risks an unknown amount.
        return SizingResult(
            0.0,
            reason="no %s to %s conversion rate" % (inst.quote, rates.account_currency),
        )

    risk_amount = float(equity) * float(risk_pct)
    raw_lots = risk_amount / (float(stop_distance_pips) * per_lot)
    if max_lots is not None:
        raw_lots = min(raw_lots, max_lots)

    lots = round_lots(inst, raw_lots)
    if lots <= 0:
        return SizingResult(
            0.0,
            reason="size %.4f lots rounds below the %.2f lot minimum" % (raw_lots, inst.min_lots),
        )

    margin = margin_for(inst, lots, price, rates, leverage)
    if margin is None:
        return SizingResult(0.0, reason="cannot price margin for %s" % inst.symbol)

    if free_margin is not None and margin > free_margin:
        # Step down to what margin allows instead of refusing outright.
        affordable = raw_lots * (free_margin / margin) if margin > 0 else 0.0
        lots = round_lots(inst, affordable)
        if lots <= 0:
            return SizingResult(
                0.0,
                reason="margin %.2f exceeds free margin %.2f" % (margin, free_margin),
            )
        margin = margin_for(inst, lots, price, rates, leverage) or 0.0

    return SizingResult(
        lots=lots,
        risk_amount=lots * float(stop_distance_pips) * per_lot,
        pip_value=lots * per_lot,
        margin_required=margin,
        reason="%.2f lots risks %.2f over %.1f pips" % (
            lots, lots * float(stop_distance_pips) * per_lot, stop_distance_pips),
    )


def money_from_pips(
    inst: Instrument,
    pips: float,
    lots: float,
    rates: RateBook,
    price: Optional[float] = None,
) -> Optional[float]:
    """Convert a pip result into account currency. Returns None if not priceable."""
    per_lot = pip_value_per_lot(inst, rates, price=price)
    if per_lot is None:
        return None
    return float(pips) * float(lots) * per_lot
