# engine/instruments.py
"""Instrument specs and the pip/lot arithmetic everything else depends on.

This module is the single place where FX unit conventions live. Pip size, lot
size, lot step and the base/quote split are declared once here; no other module
may hardcode 0.0001, 100_000, or a currency split. Get this table wrong and
every downstream number is quietly wrong, so it is covered directly by
tests/test_instruments.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

EPS = 1e-12

# Contract sizes (units of base currency)
STANDARD_LOT = 100_000.0
MINI_LOT = 10_000.0
MICRO_LOT = 1_000.0


@dataclass(frozen=True)
class Instrument:
    """Static spec for one tradable pair.

    symbol:      canonical "BASE/QUOTE"
    pip_size:    price increment of one pip (0.0001; 0.01 for JPY-quoted pairs)
    price_step:  smallest quotable price increment (a "point", usually pip/10)
    lot_step:    smallest tradable size increment, in lots
    min_lots:    broker minimum ticket
    typical_spread_pips: cost assumption when the feed carries no bid/ask
    swap_long_pips_per_day / swap_short_pips_per_day:
        financing applied at rollover, expressed in pips of the pair so the
        sign convention stays readable (negative = you pay).
    """
    symbol: str
    pip_size: float
    price_step: float
    lot_step: float = 0.01
    min_lots: float = 0.01
    max_lots: float = 100.0
    contract_size: float = STANDARD_LOT
    typical_spread_pips: float = 1.0
    swap_long_pips_per_day: float = 0.0
    swap_short_pips_per_day: float = 0.0

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0].upper()

    @property
    def quote(self) -> str:
        return self.symbol.split("/")[1].upper()

    def units(self, lots: float) -> float:
        return float(lots) * self.contract_size

    def lots_from_units(self, units: float) -> float:
        return float(units) / self.contract_size


def _pip_for(symbol: str) -> Tuple[float, float]:
    """(pip_size, price_step) by quote convention. JPY-quoted pairs use 0.01."""
    quote = symbol.split("/")[1].upper()
    if quote == "JPY":
        return 0.01, 0.001
    return 0.0001, 0.00001


def make_instrument(symbol: str, **overrides) -> Instrument:
    """Build a spec with convention-correct pip/point defaults."""
    sym = symbol.upper().strip()
    if sym.count("/") != 1 or not all(sym.split("/")):
        raise ValueError("symbol must be BASE/QUOTE, got %r" % (symbol,))
    pip_size, price_step = _pip_for(sym)
    fields = {"pip_size": pip_size, "price_step": price_step}
    fields.update(overrides)
    return Instrument(symbol=sym, **fields)


# Default universe. Swap values are placeholders in the shape a broker quotes
# them; replace with your broker published financing before any live use.
DEFAULT_INSTRUMENTS: Dict[str, Instrument] = {
    inst.symbol: inst
    for inst in (
        make_instrument("EUR/USD", typical_spread_pips=0.8,
                        swap_long_pips_per_day=-0.20, swap_short_pips_per_day=0.05),
        make_instrument("GBP/USD", typical_spread_pips=1.0,
                        swap_long_pips_per_day=-0.15, swap_short_pips_per_day=0.02),
        make_instrument("USD/JPY", typical_spread_pips=0.9,
                        swap_long_pips_per_day=0.30, swap_short_pips_per_day=-0.60),
        make_instrument("AUD/USD", typical_spread_pips=1.0,
                        swap_long_pips_per_day=-0.10, swap_short_pips_per_day=0.00),
        make_instrument("USD/CHF", typical_spread_pips=1.2,
                        swap_long_pips_per_day=0.25, swap_short_pips_per_day=-0.50),
        make_instrument("EUR/GBP", typical_spread_pips=1.2,
                        swap_long_pips_per_day=-0.08, swap_short_pips_per_day=-0.02),
        make_instrument("EUR/JPY", typical_spread_pips=1.4,
                        swap_long_pips_per_day=0.10, swap_short_pips_per_day=-0.40),
    )
}


def get_instrument(symbol: str, table: Optional[Dict[str, Instrument]] = None) -> Instrument:
    tbl = table if table is not None else DEFAULT_INSTRUMENTS
    sym = symbol.upper().strip()
    if sym not in tbl:
        raise KeyError("unknown instrument %r; add it to the instrument table" % (sym,))
    return tbl[sym]


# --- pip and price conversion ------------------------------------------------

def pips_to_price(inst: Instrument, pips: float) -> float:
    """Convert a pip distance into a price distance for this pair."""
    return float(pips) * inst.pip_size


def price_to_pips(inst: Instrument, price_delta: float) -> float:
    """Convert a price distance into pips for this pair."""
    if inst.pip_size <= 0:
        return 0.0
    return float(price_delta) / inst.pip_size


def round_to_step(value: float, step: float, mode: str = "down") -> float:
    """Snap a value to a broker increment. Used for both prices and lots."""
    if step <= 0:
        return float(value)
    q = float(value) / step
    if mode == "up":
        steps = math.ceil(q - EPS)
    elif mode == "nearest":
        steps = math.floor(q + 0.5)
    else:
        steps = math.floor(q + EPS)
    # Re-round to kill binary float dust: 0.01 is not exactly representable.
    decimals = max(0, -int(math.floor(math.log10(step))) + 6)
    return round(steps * step, decimals)


def round_lots(inst: Instrument, lots: float) -> float:
    """Snap a size to the tradable lot grid, clamped to broker min/max.

    Returns 0.0 when the request rounds below the minimum ticket. Callers must
    treat 0.0 as "do not trade", never as "trade something".
    """
    snapped = round_to_step(max(0.0, float(lots)), inst.lot_step, mode="down")
    if snapped + EPS < inst.min_lots:
        return 0.0
    return min(snapped, inst.max_lots)


def round_price(inst: Instrument, price: float, mode: str = "nearest") -> float:
    return round_to_step(float(price), inst.price_step, mode=mode)


# --- pip value ---------------------------------------------------------------

class RateBook:
    """Latest mid prices, used to convert quote-currency PnL into account currency.

    A USD-denominated account trading EUR/GBP realises PnL in GBP and needs a
    GBP-to-USD leg to state it. That conversion is the most common source of
    silently wrong FX numbers, so it is explicit and centralised here.
    """

    def __init__(self, account_currency: str = "USD", rates: Optional[Dict[str, float]] = None):
        self.account_currency = account_currency.upper()
        self._rates: Dict[str, float] = {}
        for sym, px in (rates or {}).items():
            self.update(sym, px)

    def update(self, symbol: str, price: float) -> None:
        if price and price > 0:
            self._rates[symbol.upper()] = float(price)

    def update_many(self, prices: Dict[str, float]) -> None:
        for sym, px in prices.items():
            self.update(sym, px)

    def snapshot(self) -> Dict[str, float]:
        return dict(self._rates)

    def _direct(self, frm: str, to: str) -> Optional[float]:
        if frm == to:
            return 1.0
        d = self._rates.get(frm + "/" + to)
        if d:
            return d
        i = self._rates.get(to + "/" + frm)
        if i:
            return 1.0 / i
        return None

    def rate(self, frm: str, to: str) -> Optional[float]:
        """Units of `to` per one unit of `frm`, or None if not derivable."""
        frm, to = frm.upper(), to.upper()
        direct = self._direct(frm, to)
        if direct is not None:
            return direct
        # One triangular hop through any currency we hold a rate for.
        for sym in self._rates:
            a, b = sym.split("/")
            for mid in (a, b):
                leg1 = self._direct(frm, mid)
                leg2 = self._direct(mid, to)
                if leg1 is not None and leg2 is not None:
                    return leg1 * leg2
        return None


def pip_value_per_lot(
    inst: Instrument,
    rates: RateBook,
    price: Optional[float] = None,
) -> Optional[float]:
    """Account-currency value of a one-pip move on one standard lot.

    Returns None when the conversion leg is unavailable. Callers must refuse to
    size a trade rather than guess a rate.
    """
    quote_pnl = inst.pip_size * inst.contract_size  # in quote currency
    if price is not None and price > 0:
        rates.update(inst.symbol, price)
    conv = rates.rate(inst.quote, rates.account_currency)
    if conv is None:
        return None
    return quote_pnl * conv


def pip_value_for_lots(
    inst: Instrument,
    lots: float,
    rates: RateBook,
    price: Optional[float] = None,
) -> Optional[float]:
    per_lot = pip_value_per_lot(inst, rates, price=price)
    if per_lot is None:
        return None
    return per_lot * float(lots)


def currencies_in(symbols: Iterable[str]) -> Dict[str, int]:
    """Count how many configured pairs touch each currency."""
    counts: Dict[str, int] = {}
    for s in symbols:
        base, quote = s.upper().split("/")
        counts[base] = counts.get(base, 0) + 1
        counts[quote] = counts.get(quote, 0) + 1
    return counts
