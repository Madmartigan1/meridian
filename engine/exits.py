# engine/exits.py
"""Exit management.

One rule here is not configurable: every position carries a hard stop, and the
stop is checked before anything else on every bar. Leverage is what makes FX
lethal rather than merely unprofitable -- at 30:1 a 3% move against an
unstopped position is the whole account -- so the stop is placed at entry, is
never removed, and may only ever be moved in the direction that reduces risk.

The remaining exits are opt-in and ordered by severity:

    stop  ->  take profit  ->  trailing  ->  stagnation  ->  max hold  ->  session
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .instruments import Instrument, pips_to_price, price_to_pips, round_price
from .position import LONG, Position
from .sessions import market_hours_between, weekend_gap_risk


@dataclass
class ExitSettings:
    """Exit parameters. Note there is no `enable_stop` flag, by design."""
    atr_stop_mult: float = 1.8          # stop distance = ATR * this
    min_stop_pips: float = 8.0          # floor, so cost noise cannot stop us out
    max_stop_pips: float = 80.0         # ceiling, so one trade cannot size to dust

    reward_risk: float = 2.0            # take profit at this multiple of the stop
    enable_take_profit: bool = True

    enable_trailing: bool = True
    trail_start_r: float = 1.0          # start trailing once up this many R
    trail_distance_r: float = 0.8       # trail this far behind the peak, in R

    enable_stagnation: bool = True
    stagnation_bars: int = 48           # bars without progress before cutting
    stagnation_pips: float = 3.0        # progress threshold

    enable_max_hold: bool = True
    max_hold_hours: float = 72.0        # counted in *market* hours only

    close_before_weekend: bool = True   # flat into the Friday close
    weekend_flat_hours: float = 2.0


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str = ""
    is_stop: bool = False
    price: Optional[float] = None       # set when the level, not the close, fills


def stop_distance_pips(inst: Instrument, atr_price: Optional[float], settings: ExitSettings) -> float:
    """Stop distance from volatility, clamped to a sane envelope.

    This is the number that drives position size, so it is computed once here
    and passed to the sizer rather than being re-derived anywhere else.
    """
    if atr_price is None or atr_price <= 0:
        return settings.min_stop_pips
    raw = price_to_pips(inst, atr_price) * settings.atr_stop_mult
    return max(settings.min_stop_pips, min(settings.max_stop_pips, raw))


def initial_levels(
    inst: Instrument,
    side: str,
    entry_price: float,
    stop_pips: float,
    settings: ExitSettings,
) -> Tuple[float, Optional[float]]:
    """(stop_price, take_profit_price) for a new position."""
    dist = pips_to_price(inst, stop_pips)
    if side == LONG:
        stop = entry_price - dist
        tp = entry_price + dist * settings.reward_risk if settings.enable_take_profit else None
    else:
        stop = entry_price + dist
        tp = entry_price - dist * settings.reward_risk if settings.enable_take_profit else None
    return round_price(inst, stop), (round_price(inst, tp) if tp is not None else None)


def tighten_trailing_stop(
    inst: Instrument,
    pos: Position,
    price: float,
    settings: ExitSettings,
) -> bool:
    """Move the stop toward the position once it is meaningfully in profit.

    Only ever tightens. A trailing stop that can widen is not a stop.
    """
    if not settings.enable_trailing:
        return False
    # R is the stop distance at entry, not the tightened one. Using the current
    # distance makes R shrink with every tighten, so the trail accelerates
    # toward price and the configured trail_distance_r stops meaning anything.
    risk_pips = pos.risk_pips(inst)
    if risk_pips <= 0:
        return False
    excursion = pos.excursion_pips(inst, price)
    pos.peak_pips = max(pos.peak_pips, excursion)
    if pos.peak_pips < settings.trail_start_r * risk_pips:
        return False

    trail_pips = settings.trail_distance_r * risk_pips
    locked = pos.peak_pips - trail_pips
    dist = pips_to_price(inst, locked)
    candidate = pos.entry_price + dist if pos.is_long else pos.entry_price - dist
    candidate = round_price(inst, candidate)

    if pos.is_long and candidate > pos.stop_price:
        pos.stop_price = candidate
        return True
    if (not pos.is_long) and candidate < pos.stop_price:
        pos.stop_price = candidate
        return True
    return False


def evaluate(
    inst: Instrument,
    pos: Position,
    high: float,
    low: float,
    close: float,
    now_utc: datetime,
    settings: ExitSettings,
) -> ExitDecision:
    """Decide whether this position closes on this bar, and why.

    Checked in severity order. The stop is first and unconditional: if both the
    stop and the take profit are inside the same bar, the stop is assumed to
    have filled, because assuming otherwise flatters every result produced.
    """
    if pos.touches_stop(high, low):
        return ExitDecision(True, "stop hit", is_stop=True, price=pos.stop_price)

    if settings.enable_take_profit and pos.touches_take_profit(high, low):
        return ExitDecision(True, "take profit", price=pos.take_profit_price)

    if settings.close_before_weekend and weekend_gap_risk(now_utc, settings.weekend_flat_hours):
        return ExitDecision(True, "flat into weekend close")

    if settings.enable_max_hold:
        try:
            opened = datetime.fromisoformat(pos.opened_at)
        except (TypeError, ValueError):
            opened = None
        if opened is not None:
            held = market_hours_between(opened, now_utc)
            if held >= settings.max_hold_hours:
                return ExitDecision(True, "max hold %.0f market hours" % held)

    if settings.enable_stagnation and pos.bars_held >= settings.stagnation_bars:
        if abs(pos.excursion_pips(inst, close)) < settings.stagnation_pips:
            return ExitDecision(
                True,
                "stagnant %d bars inside %.1f pips" % (pos.bars_held, settings.stagnation_pips),
            )

    return ExitDecision(False)
