# engine/sessions.py
"""Trading calendar, rollover, and financing.

FX runs about 24x5, not 24x7, and that has three consequences a crypto-shaped
engine has no equivalent for:

1. The week opens Sunday 17:00 New York and closes Friday 17:00 New York. Price
   gaps across the weekend, so a stop can be jumped rather than touched.
2. Positions held through 17:00 New York are rolled: financing (swap) is
   credited or debited once per rollover.
3. Wednesday rollover conventionally carries triple financing, because spot FX
   settles T+2 and Wednesday's roll covers the weekend.

New York local time is computed from US DST rules directly rather than through
zoneinfo, because the tz database is not present on a stock Windows Python and
a missing-timezone crash mid-run is not an acceptable failure mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

# Week boundaries, in New York local hours.
WEEK_OPEN_WEEKDAY = 6    # Sunday (Python: Monday=0 .. Sunday=6)
WEEK_OPEN_HOUR = 17
WEEK_CLOSE_WEEKDAY = 4   # Friday
WEEK_CLOSE_HOUR = 17

ROLLOVER_HOUR = 17       # New York
TRIPLE_SWAP_WEEKDAY = 2  # Wednesday roll covers T+2 settlement over the weekend

# Session windows in New York local hours, [start, end). Approximate by
# convention; brokers differ by an hour around DST changeovers.
SESSION_WINDOWS: Dict[str, Tuple[int, int]] = {
    "SYDNEY": (17, 2),
    "TOKYO": (19, 4),
    "LONDON": (3, 12),
    "NEW_YORK": (8, 17),
}


def _parse_iso(value) -> Optional[datetime]:
    """Parse a stored ISO timestamp, returning None rather than raising.

    A position with an unreadable open time must not stop financing running
    for every other position in the book.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """Date of the nth given weekday of a month (weekday: Monday=0)."""
    d = datetime(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _us_dst_bounds(year: int) -> Tuple[datetime, datetime]:
    """DST start/end for the US, expressed in UTC."""
    # Second Sunday of March, 02:00 local standard time (= 07:00 UTC).
    start = _nth_weekday(year, 3, 6, 2).replace(hour=7)
    # First Sunday of November, 02:00 local daylight time (= 06:00 UTC).
    end = _nth_weekday(year, 11, 6, 1).replace(hour=6)
    return start, end


def ny_offset_hours(dt_utc: datetime) -> int:
    """New York UTC offset: -4 during daylight time, -5 otherwise."""
    naive = dt_utc.replace(tzinfo=None)
    start, end = _us_dst_bounds(naive.year)
    return -4 if start <= naive < end else -5


def to_ny(dt_utc: datetime) -> datetime:
    """Convert an aware UTC datetime to naive New York local time."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_utc = dt_utc.astimezone(timezone.utc)
    return (dt_utc + timedelta(hours=ny_offset_hours(dt_utc))).replace(tzinfo=None)


def is_market_open(dt_utc: datetime) -> bool:
    """True inside the Sunday 17:00 to Friday 17:00 New York window."""
    ny = to_ny(dt_utc)
    wd, hour = ny.weekday(), ny.hour
    if wd == 5:                                    # Saturday
        return False
    if wd == WEEK_OPEN_WEEKDAY:                    # Sunday
        return hour >= WEEK_OPEN_HOUR
    if wd == WEEK_CLOSE_WEEKDAY:                   # Friday
        return hour < WEEK_CLOSE_HOUR
    return True


def active_sessions(dt_utc: datetime) -> Tuple[str, ...]:
    """Which regional sessions are open at this instant."""
    hour = to_ny(dt_utc).hour
    out = []
    for name, (start, end) in SESSION_WINDOWS.items():
        inside = start <= hour < end if start < end else (hour >= start or hour < end)
        if inside:
            out.append(name)
    return tuple(out)


def is_liquid_window(dt_utc: datetime) -> bool:
    """London or New York open: where the spread assumptions actually hold.

    Outside these hours spreads widen and the typical_spread_pips in the
    instrument table understates real cost, so the engine can be configured to
    stand down rather than pay for thin liquidity.
    """
    active = active_sessions(dt_utc)
    return "LONDON" in active or "NEW_YORK" in active


def rollover_count(prev_utc: Optional[datetime], now_utc: datetime) -> int:
    """How many 17:00 New York rollovers fall in (prev, now].

    Returns a *weighted* count: a Wednesday roll counts three times, matching
    the triple-swap convention.
    """
    if prev_utc is None:
        return 0
    if now_utc <= prev_utc:
        return 0
    weight = 0
    cursor = to_ny(prev_utc)
    end = to_ny(now_utc)
    # Step day by day over the New York calendar, testing each 17:00 boundary.
    probe = cursor.replace(hour=ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
    if probe <= cursor:
        probe += timedelta(days=1)
    while probe <= end:
        if probe.weekday() != 5:  # no Saturday roll; the market is shut
            weight += 3 if probe.weekday() == TRIPLE_SWAP_WEEKDAY else 1
        probe += timedelta(days=1)
    return weight


def weekend_gap_risk(dt_utc: datetime, hours_ahead: float = 2.0) -> bool:
    """True when the Friday close is within `hours_ahead`.

    Entries taken here carry gap risk a stop cannot protect against, because
    Sunday can open past it.
    """
    ny = to_ny(dt_utc)
    if ny.weekday() != WEEK_CLOSE_WEEKDAY:
        return False
    close = ny.replace(hour=WEEK_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if ny >= close:
        return True
    return (close - ny).total_seconds() / 3600.0 <= hours_ahead


def market_hours_between(start_utc: datetime, end_utc: datetime) -> float:
    """Hours elapsed with the market actually open.

    A max-hold-time rule must not expire a position over a weekend when no
    price has moved and no exit was possible.
    """
    if end_utc <= start_utc:
        return 0.0
    total = 0.0
    step = timedelta(minutes=30)
    cursor = start_utc
    while cursor < end_utc:
        nxt = min(cursor + step, end_utc)
        if is_market_open(cursor):
            total += (nxt - cursor).total_seconds() / 3600.0
        cursor = nxt
    return total


@dataclass
class SwapBook:
    """Applies financing to open positions at each rollover.

    Swap is expressed in pips per lot per day on the instrument spec, so it
    lands in the same unit as everything else the position carries.
    """
    enabled: bool = True
    last_seen_utc: Optional[datetime] = None

    def apply(self, book, instruments: Dict[str, object], now_utc: datetime) -> Dict[str, float]:
        """Credit/debit financing for rollovers since the last call.

        The window is measured per position, from the later of the last call
        and the position's own open time. A position opened at 18:00 New York
        never held through that day's 17:00 roll, and charging it anyway would
        bill three days of financing to a trade that was flat at the time --
        the Wednesday weight makes that error the largest one available.

        Returns the pips applied per symbol, for logging.
        """
        applied: Dict[str, float] = {}
        if not self.enabled:
            self.last_seen_utc = now_utc
            return applied

        for pos in book.all():
            inst = instruments.get(pos.symbol)
            if inst is None:
                continue
            since = self.last_seen_utc
            opened = _parse_iso(getattr(pos, "opened_at", None))
            if opened is not None and (since is None or opened > since):
                since = opened
            weight = rollover_count(since, now_utc)
            if weight <= 0:
                continue
            per_day = inst.swap_long_pips_per_day if pos.is_long else inst.swap_short_pips_per_day
            pips = per_day * weight
            if pips:
                pos.swap_pips += pips
                pos.rollovers += weight
                applied[pos.symbol] = pips

        self.last_seen_utc = now_utc
        return applied
