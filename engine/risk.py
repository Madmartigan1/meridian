# engine/risk.py
"""Portfolio-level risk gates.

The gate that is genuinely FX-native is currency exposure netting. Every pair
is two currencies, so positions that look independent are not:

    long EUR/USD + long GBP/USD + short USD/JPY  =  three ways to be short USD

Per-instrument limits cannot see that. Sized at "1% each" this is a 3% bet on
one macro outcome, and a single dollar headline settles all three at once. So
exposure is aggregated per currency, in account-currency notional, and the gate
refuses the entry that would concentrate it further.

The other gates are conventional but non-negotiable in a leveraged product:
daily loss limit, total open risk, position count, and margin headroom.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .instruments import Instrument, RateBook
from .position import LONG, PositionBook
from .sizing import margin_for


@dataclass
class RiskSettings:
    risk_per_trade_pct: float = 0.005       # 0.5% of equity per trade
    max_open_positions: int = 4
    max_total_risk_pct: float = 0.02        # sum of open risk, 2% of equity
    # Per-currency notional as a multiple of equity. This is a *concentration*
    # limit, not a leverage limit: a single risk-based position at 30:1 with a
    # 15-pip stop is already 3-4x equity in notional, so a cap near 1.0 refuses
    # every trade rather than only the concentrated ones. It is set to sit just
    # under what max_margin_utilisation * leverage permits in total.
    max_currency_exposure_pct: float = 5.0
    max_daily_loss_pct: float = 0.03        # halt for the day past this
    max_margin_utilisation: float = 0.30    # never commit more than this to margin
    leverage: float = 30.0                  # ESMA retail cap on majors
    require_liquid_session: bool = True


@dataclass
class RiskState:
    """Rolling per-day counters. Reset on the New York trading day."""
    day_key: str = ""
    realised_pnl: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""

    def roll(self, day_key: str) -> None:
        if day_key != self.day_key:
            self.day_key = day_key
            self.realised_pnl = 0.0
            self.trades_today = 0
            self.halted = False
            self.halt_reason = ""

    def to_dict(self) -> dict:
        return {
            "day_key": self.day_key,
            "realised_pnl": self.realised_pnl,
            "trades_today": self.trades_today,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(
            day_key=str(d.get("day_key", "")),
            realised_pnl=float(d.get("realised_pnl", 0.0)),
            trades_today=int(d.get("trades_today", 0)),
            halted=bool(d.get("halted", False)),
            halt_reason=str(d.get("halt_reason", "")),
        )


def currency_exposure(
    book: PositionBook,
    instruments: Dict[str, Instrument],
    rates: RateBook,
) -> Dict[str, float]:
    """Signed exposure per currency, in account-currency notional.

    Long EUR/USD is long EUR and short USD by the same notional, so each
    position contributes to two currency buckets with opposite signs.
    """
    exposure: Dict[str, float] = {}
    for pos in book.all():
        inst = instruments.get(pos.symbol)
        if inst is None:
            continue
        price = pos.last_price or pos.entry_price
        notional_base = inst.units(pos.lots)
        base_conv = rates.rate(inst.base, rates.account_currency)
        quote_conv = rates.rate(inst.quote, rates.account_currency)
        if base_conv is None and quote_conv is not None and price:
            base_conv = price * quote_conv
        if base_conv is None:
            continue
        value = notional_base * base_conv
        sign = pos.sign
        exposure[inst.base] = exposure.get(inst.base, 0.0) + sign * value
        exposure[inst.quote] = exposure.get(inst.quote, 0.0) - sign * value
    return exposure


def projected_exposure(
    book: PositionBook,
    instruments: Dict[str, Instrument],
    rates: RateBook,
    inst: Instrument,
    side: str,
    lots: float,
    price: float,
) -> Dict[str, float]:
    """Exposure as it would be if this entry were taken."""
    exposure = currency_exposure(book, instruments, rates)
    quote_conv = rates.rate(inst.quote, rates.account_currency)
    base_conv = rates.rate(inst.base, rates.account_currency)
    if base_conv is None and quote_conv is not None and price:
        base_conv = price * quote_conv
    if base_conv is None:
        return exposure
    value = inst.units(lots) * base_conv
    sign = 1 if side == LONG else -1
    exposure[inst.base] = exposure.get(inst.base, 0.0) + sign * value
    exposure[inst.quote] = exposure.get(inst.quote, 0.0) - sign * value
    return exposure


def open_risk_amount(
    book: PositionBook,
    instruments: Dict[str, Instrument],
    rates: RateBook,
) -> float:
    """Account currency that would be lost if every open stop were hit at once.

    Positions trailed into profit contribute zero, not a negative -- a locked-in
    gain is not risk budget to spend twice.
    """
    from .instruments import pip_value_per_lot

    total = 0.0
    for pos in book.all():
        inst = instruments.get(pos.symbol)
        if inst is None:
            continue
        per_lot = pip_value_per_lot(inst, rates, price=pos.last_price or pos.entry_price)
        if per_lot is None:
            continue
        stop_pips = pos.stop_distance_pips(inst)
        # If the stop is already past entry, the remaining downside is a profit.
        beyond = (pos.stop_price - pos.entry_price) * pos.sign
        if beyond >= 0:
            continue
        total += stop_pips * pos.lots * per_lot
    return total


def used_margin(
    book: PositionBook,
    instruments: Dict[str, Instrument],
    rates: RateBook,
    leverage: float,
) -> float:
    total = 0.0
    for pos in book.all():
        inst = instruments.get(pos.symbol)
        if inst is None:
            continue
        m = margin_for(inst, pos.lots, pos.last_price or pos.entry_price, rates, leverage)
        if m:
            total += m
    return total


class RiskManager:
    """Answers one question: may this entry be taken, and if not, why not."""

    def __init__(self, settings: Optional[RiskSettings] = None, state: Optional[RiskState] = None):
        self.settings = settings or RiskSettings()
        self.state = state or RiskState()

    # --- daily accounting ---------------------------------------------------

    def day_key(self, now_utc: datetime) -> str:
        """Trading day keyed to New York, so the day rolls with the market."""
        from .sessions import to_ny
        ny = to_ny(now_utc)
        return ny.strftime("%Y-%m-%d")

    def on_bar(self, now_utc: datetime) -> None:
        self.state.roll(self.day_key(now_utc))

    def record_realised(self, pnl: float, equity: float, now_utc: datetime) -> None:
        self.on_bar(now_utc)
        self.state.realised_pnl += float(pnl)
        self.state.trades_today += 1
        limit = -abs(self.settings.max_daily_loss_pct * equity)
        if equity > 0 and self.state.realised_pnl <= limit:
            self.state.halted = True
            self.state.halt_reason = "daily loss %.2f hit limit %.2f" % (
                self.state.realised_pnl, limit)

    # --- the gate -----------------------------------------------------------

    def allows_entry(
        self,
        inst: Instrument,
        side: str,
        lots: float,
        price: float,
        equity: float,
        book: PositionBook,
        instruments: Dict[str, Instrument],
        rates: RateBook,
        now_utc: datetime,
    ) -> Tuple[bool, str]:
        s = self.settings
        self.on_bar(now_utc)

        # Refusals are prefixed with a stable short code before the colon, so a
        # run report can aggregate them without the numbers making every
        # message unique.
        if self.state.halted:
            return False, "daily_halt: %s" % self.state.halt_reason

        if equity <= 0:
            return False, "no_equity: account is empty"

        if len(book) >= s.max_open_positions and inst.symbol not in book:
            return False, "max_positions: already holding %d" % s.max_open_positions

        from .instruments import pip_value_per_lot
        per_lot = pip_value_per_lot(inst, rates, price=price)
        if per_lot is None:
            return False, "no_conversion: %s to %s unavailable" % (
                inst.quote, rates.account_currency)

        existing = open_risk_amount(book, instruments, rates)
        proposed_risk = s.risk_per_trade_pct * equity
        if (existing + proposed_risk) > s.max_total_risk_pct * equity:
            return False, "total_risk: open %.2f + new %.2f exceeds %.1f%% of equity" % (
                existing, proposed_risk, s.max_total_risk_pct * 100)

        exposure = projected_exposure(book, instruments, rates, inst, side, lots, price)
        cap = s.max_currency_exposure_pct * equity
        for ccy, value in exposure.items():
            if abs(value) > cap:
                return False, "currency_exposure: %s would reach %.0f against a %.0f cap" % (
                    ccy, abs(value), cap)

        margin = margin_for(inst, lots, price, rates, s.leverage)
        if margin is None:
            return False, "no_margin_price: cannot value %s notional" % inst.base
        in_use = used_margin(book, instruments, rates, s.leverage)
        if (in_use + margin) > s.max_margin_utilisation * equity:
            return False, "margin_cap: %.0f + %.0f exceeds %.0f%% utilisation" % (
                in_use, margin, s.max_margin_utilisation * 100)

        if s.require_liquid_session:
            from .sessions import is_liquid_window
            if not is_liquid_window(now_utc):
                return False, "illiquid_session: outside London/New York hours"

        return True, "ok"
