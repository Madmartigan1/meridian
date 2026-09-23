# engine/execution.py
"""Execution model: how a decision becomes a fill price.

Retail FX has no maker rebate and no passive queue to sit in. You deal on the
broker or liquidity provider's two-way price: you buy at the ask and sell at
the bid, and the spread is paid on entry and again on exit. A post-only,
maker-first execution path -- the right model on an exchange order book -- has
no analogue here, and pretending otherwise understates cost by roughly one
spread per round trip on every single trade.

So this module models exactly three costs and states them explicitly:

  1. Spread     -- always paid, on both legs
  2. Slippage   -- adverse, applied to market orders and to stops
  3. Commission -- per lot per side, zero on most spread-only accounts

Stops get a separate, larger slippage assumption, because a stop is filled in
exactly the conditions where liquidity is worst.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .instruments import Instrument, pips_to_price, price_to_pips, round_price
from .position import LONG, SHORT, side_to_sign


@dataclass
class ExecutionSettings:
    """Cost assumptions. These are the numbers that decide whether a strategy
    that looks profitable on mid prices actually is."""
    use_feed_spread: bool = True        # prefer bid/ask from the feed when present
    spread_multiplier: float = 1.0      # stress-test costs by scaling the spread
    slippage_pips: float = 0.2          # adverse slip on a normal market order
    stop_slippage_pips: float = 1.0     # adverse slip when a stop is triggered
    commission_per_lot: float = 0.0     # account currency, charged per side
    max_spread_pips: float = 3.5        # refuse to trade a blown-out spread


@dataclass
class Quote:
    """A two-way price at one instant."""
    mid: float
    bid: Optional[float] = None
    ask: Optional[float] = None

    def with_spread(self, inst: Instrument, spread_pips: float) -> "Quote":
        """Synthesise bid/ask around the mid when the feed carries mid only."""
        half = pips_to_price(inst, spread_pips) / 2.0
        return Quote(mid=self.mid, bid=self.mid - half, ask=self.mid + half)


@dataclass
class Fill:
    symbol: str
    side: str
    lots: float
    price: float
    spread_pips: float
    slippage_pips: float
    commission: float
    reason: str = ""
    rejected: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected and self.lots > 0


def resolve_quote(inst: Instrument, quote: Quote, settings: ExecutionSettings) -> Quote:
    """Produce a usable two-way price, honouring the cost settings."""
    if settings.use_feed_spread and quote.bid is not None and quote.ask is not None:
        spread_pips = price_to_pips(inst, quote.ask - quote.bid) * settings.spread_multiplier
    else:
        spread_pips = inst.typical_spread_pips * settings.spread_multiplier
    mid = quote.mid if quote.mid else ((quote.bid or 0.0) + (quote.ask or 0.0)) / 2.0
    return Quote(mid=mid).with_spread(inst, spread_pips)


def spread_pips_of(inst: Instrument, quote: Quote) -> float:
    if quote.bid is None or quote.ask is None:
        return inst.typical_spread_pips
    return price_to_pips(inst, quote.ask - quote.bid)


def fill_price(
    inst: Instrument,
    side: str,
    quote: Quote,
    settings: ExecutionSettings,
    is_stop: bool = False,
) -> float:
    """The price a market order in `side` actually gets.

    Buys cross to the ask and slip upward; sells cross to the bid and slip
    downward. Slippage is always adverse -- modelling it as symmetric noise
    would quietly hand back half the cost.
    """
    q = resolve_quote(inst, quote, settings)
    slip = settings.stop_slippage_pips if is_stop else settings.slippage_pips
    slip_price = pips_to_price(inst, slip)
    if side_to_sign(side) > 0:
        raw = (q.ask or q.mid) + slip_price
    else:
        raw = (q.bid or q.mid) - slip_price
    return round_price(inst, raw)


def execute(
    inst: Instrument,
    side: str,
    lots: float,
    quote: Quote,
    settings: ExecutionSettings,
    reason: str = "",
    is_stop: bool = False,
    enforce_spread_cap: bool = True,
) -> Fill:
    """Simulate a market order and return the resulting fill, or a rejection.

    The spread cap is the guard that matters around data releases: the trigger
    fires, the spread triples, and the trade is only profitable at the spread
    the backtest assumed. Refusing is the correct behaviour.
    """
    q = resolve_quote(inst, quote, settings)
    spread = spread_pips_of(inst, q)

    if enforce_spread_cap and not is_stop and spread > settings.max_spread_pips:
        return Fill(
            symbol=inst.symbol, side=side, lots=0.0, price=q.mid,
            spread_pips=spread, slippage_pips=0.0, commission=0.0,
            reason=reason,
            rejected="spread %.1f pips over cap %.1f" % (spread, settings.max_spread_pips),
        )

    if lots <= 0:
        return Fill(
            symbol=inst.symbol, side=side, lots=0.0, price=q.mid,
            spread_pips=spread, slippage_pips=0.0, commission=0.0,
            reason=reason, rejected="zero size",
        )

    price = fill_price(inst, side, q, settings, is_stop=is_stop)
    slip = settings.stop_slippage_pips if is_stop else settings.slippage_pips
    return Fill(
        symbol=inst.symbol,
        side=side,
        lots=float(lots),
        price=price,
        spread_pips=spread,
        slippage_pips=slip,
        commission=float(lots) * settings.commission_per_lot,
        reason=reason,
    )


def stop_fill_price(
    inst: Instrument,
    side: str,
    stop_price: float,
    bar_open: float,
    settings: ExecutionSettings,
) -> float:
    """Fill price for a triggered stop, including gap handling.

    If the bar opened beyond the stop -- a weekend gap, or a release spike --
    the fill is at the open, not at the stop level. A stop is an instruction to
    trade at market once touched, not a guarantee of price. Modelling it as an
    exact fill is the single most flattering error a backtest can make.
    """
    sign = side_to_sign(side)
    slip = pips_to_price(inst, settings.stop_slippage_pips)
    if sign > 0:  # long: stop sells below
        worst = min(float(stop_price), float(bar_open))
        return round_price(inst, worst - slip)
    worst = max(float(stop_price), float(bar_open))
    return round_price(inst, worst + slip)


def round_trip_cost_pips(inst: Instrument, settings: ExecutionSettings) -> float:
    """What a trade must clear before it earns anything.

    Worth printing at startup: if this exceeds the average favourable
    excursion of the strategy, no amount of tuning makes it profitable.
    """
    spread = inst.typical_spread_pips * settings.spread_multiplier
    return 2.0 * spread + 2.0 * settings.slippage_pips
