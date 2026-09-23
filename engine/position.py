# engine/position.py
"""Signed position model with netting.

The single most important difference from a spot long-only engine: in FX a
SELL is a *direction*, not an exit. Opening short is a first-class action, and
"close" is a distinct operation from "open the other way". Everything here is
built around signed units so that a flip (long to short in one fill) settles
the old leg and opens the new one, rather than silently producing a negative
holding.

One net position per symbol. Hedged (simultaneous long and short on the same
pair) is deliberately not supported: it doubles financing for no economic
exposure, and it makes the exposure netting in engine/risk.py unreadable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .instruments import Instrument, price_to_pips

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"
BUY = "BUY"
SELL = "SELL"

EPS = 1e-9


def side_to_sign(side: str) -> int:
    s = side.upper()
    if s in (BUY, LONG):
        return 1
    if s in (SELL, SHORT):
        return -1
    raise ValueError("unknown side %r" % (side,))


def sign_to_side(sign: int) -> str:
    return LONG if sign > 0 else SHORT


def opposite(side: str) -> str:
    return SHORT if side_to_sign(side) > 0 else LONG


@dataclass
class ClosedLeg:
    """One settled slice of a position, produced by a reducing or flipping fill."""
    symbol: str
    side: str            # direction that was held
    lots: float
    entry_price: float
    exit_price: float
    pips: float          # signed, direction-aware
    opened_at: str
    closed_at: str
    entry_reason: str = ""
    exit_reason: str = ""
    swap_pips: float = 0.0   # financing carried by the closed slice


@dataclass
class Position:
    """A live net position in one pair.

    stop_price is not optional. A leveraged FX position without a hard stop is
    the standard way an account is destroyed, so the exit manager refuses to
    open one without it (see engine/exits.py).
    """
    symbol: str
    side: str
    lots: float
    entry_price: float
    opened_at: str
    stop_price: float
    take_profit_price: Optional[float] = None
    entry_reason: str = ""
    swap_pips: float = 0.0
    # Stop distance at entry, in pips. This is the R that trailing and reward
    # targets are measured against. It must not be re-derived from the *current*
    # stop, because trailing moves that stop and R would then shrink every time
    # it tightened -- making a "0.8R trail" accelerate toward the price.
    initial_stop_pips: float = 0.0
    last_price: Optional[float] = None
    peak_pips: float = 0.0          # best excursion seen, for trailing / stagnation
    bars_held: int = 0
    rollovers: int = 0

    @property
    def sign(self) -> int:
        return side_to_sign(self.side)

    @property
    def is_long(self) -> bool:
        return self.sign > 0

    def signed_units(self, inst: Instrument) -> float:
        return self.sign * inst.units(self.lots)

    def unrealized_pips(self, inst: Instrument, price: float) -> float:
        """Direction-aware open PnL in pips, financing included."""
        raw = (float(price) - self.entry_price) * self.sign
        return price_to_pips(inst, raw) + self.swap_pips

    def excursion_pips(self, inst: Instrument, price: float) -> float:
        """Price excursion only, excluding financing (used by exit rules)."""
        raw = (float(price) - self.entry_price) * self.sign
        return price_to_pips(inst, raw)

    def stop_distance_pips(self, inst: Instrument) -> float:
        """Distance from entry to the stop *as it stands now*.

        This shrinks as a trailing stop tightens, so it measures remaining
        downside -- what open-risk accounting wants. Anything expressing a
        multiple of the original risk must use `risk_pips` instead.
        """
        return abs(price_to_pips(inst, self.entry_price - self.stop_price))

    def risk_pips(self, inst: Instrument) -> float:
        """The R unit: stop distance as it was at entry.

        Falls back to the current distance for positions restored from state
        written before this field existed.
        """
        if self.initial_stop_pips > 0:
            return self.initial_stop_pips
        return self.stop_distance_pips(inst)

    def touches_stop(self, high: float, low: float) -> bool:
        """Intrabar stop test. Long stops trigger on the low, short on the high."""
        if self.is_long:
            return float(low) <= self.stop_price
        return float(high) >= self.stop_price

    def touches_take_profit(self, high: float, low: float) -> bool:
        if self.take_profit_price is None:
            return False
        if self.is_long:
            return float(high) >= self.take_profit_price
        return float(low) <= self.take_profit_price

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class PositionBook:
    """All open positions, one per symbol, with netting on every fill."""

    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}

    # --- inspection ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._positions)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._positions

    def get(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol.upper())

    def all(self) -> List[Position]:
        return list(self._positions.values())

    def symbols(self) -> List[str]:
        return list(self._positions.keys())

    # --- mutation -----------------------------------------------------------

    def apply_fill(
        self,
        inst: Instrument,
        side: str,
        lots: float,
        price: float,
        when: Optional[datetime] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        reason: str = "",
    ) -> List[ClosedLeg]:
        """Net a fill into the book and return whatever it settled.

        Same direction  -> size up, entry price becomes the lot-weighted average.
        Opposite, smaller or equal -> reduce or close, returning one ClosedLeg.
        Opposite, larger -> close the old leg fully and open the remainder the
        other way (a flip), returning the settled leg.
        """
        sym = inst.symbol
        lots = float(lots)
        price = float(price)
        if lots <= EPS:
            return []
        ts = (when or datetime.now(timezone.utc)).isoformat()
        incoming = side_to_sign(side)
        pos = self._positions.get(sym)

        if pos is None:
            if stop_price is None:
                raise ValueError("refusing to open %s without a stop price" % sym)
            self._positions[sym] = Position(
                symbol=sym,
                side=sign_to_side(incoming),
                lots=lots,
                entry_price=price,
                opened_at=ts,
                stop_price=float(stop_price),
                take_profit_price=take_profit_price,
                entry_reason=reason,
                last_price=price,
                initial_stop_pips=abs(price_to_pips(inst, price - float(stop_price))),
            )
            return []

        if incoming == pos.sign:
            total = pos.lots + lots
            pos.entry_price = (pos.entry_price * pos.lots + price * lots) / total
            pos.lots = total
            pos.last_price = price
            if stop_price is not None:
                pos.stop_price = float(stop_price)
            if take_profit_price is not None:
                pos.take_profit_price = take_profit_price
            # Adding moves the average entry, so the risk unit is restated from
            # the new entry and stop rather than kept from the first fill.
            pos.initial_stop_pips = abs(price_to_pips(inst, pos.entry_price - pos.stop_price))
            return []

        # Opposing fill: settle up to the open size.
        closing = min(lots, pos.lots)
        share = closing / pos.lots if pos.lots > EPS else 1.0
        leg = ClosedLeg(
            symbol=sym,
            side=pos.side,
            lots=closing,
            entry_price=pos.entry_price,
            exit_price=price,
            pips=price_to_pips(inst, (price - pos.entry_price) * pos.sign),
            opened_at=pos.opened_at,
            closed_at=ts,
            entry_reason=pos.entry_reason,
            exit_reason=reason,
            swap_pips=pos.swap_pips * share,
        )
        remaining_open = pos.lots - closing
        leftover = lots - closing

        if remaining_open > EPS:
            pos.lots = remaining_open
            pos.swap_pips -= leg.swap_pips
            pos.last_price = price
            return [leg]

        del self._positions[sym]

        if leftover > EPS:
            # Flip: the excess opens a fresh position the other way.
            if stop_price is None:
                raise ValueError("refusing to flip %s without a stop price" % sym)
            self._positions[sym] = Position(
                symbol=sym,
                side=sign_to_side(incoming),
                lots=leftover,
                entry_price=price,
                opened_at=ts,
                stop_price=float(stop_price),
                take_profit_price=take_profit_price,
                entry_reason=reason,
                last_price=price,
                initial_stop_pips=abs(price_to_pips(inst, price - float(stop_price))),
            )
        return [leg]

    def close(
        self,
        inst: Instrument,
        price: float,
        lots: Optional[float] = None,
        when: Optional[datetime] = None,
        reason: str = "",
    ) -> List[ClosedLeg]:
        """Reduce or fully close an open position. Never opens or flips.

        This is the operation an exit rule calls. Keeping it separate from
        apply_fill is what stops an exit from accidentally reversing into a new
        trade when the size arithmetic drifts.
        """
        pos = self.get(inst.symbol)
        if pos is None:
            return []
        size = pos.lots if lots is None else min(float(lots), pos.lots)
        return self.apply_fill(
            inst, opposite(pos.side), size, price, when=when, reason=reason
        )

    def mark(self, symbol: str, price: float) -> None:
        pos = self.get(symbol)
        if pos is not None:
            pos.last_price = float(price)

    # --- persistence --------------------------------------------------------

    def to_list(self) -> List[dict]:
        return [p.to_dict() for p in self._positions.values()]

    def load_list(self, rows: List[dict]) -> None:
        self._positions = {}
        for row in rows or []:
            try:
                pos = Position.from_dict(row)
            except TypeError:
                continue
            self._positions[pos.symbol.upper()] = pos
