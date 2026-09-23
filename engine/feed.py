# engine/feed.py
"""Market data adapters.

The engine consumes `Bar` objects and nothing else. Replay and live wear the
same interface, so the decision path that is validated on historical data is
byte-for-byte the path that would run live -- the alternative, a separate
backtest code path, is how a strategy comes to behave differently in the one
mode that costs money.

`CsvReplayFeed` works with no credentials and no network. `LiveFeed` is a
documented stub: it defines the contract a broker or data vendor adapter must
satisfy and deliberately refuses to run until one is wired in.

Bars are emitted only once closed, in chronological order, exactly once. A bar
delivered twice double-counts a signal; a partially-formed bar produces a
crossover that unwinds before the candle closes.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from .instruments import Instrument, price_to_pips


@dataclass(frozen=True)
class Bar:
    """One closed candle. `bid`/`ask` are the close-time two-way price if known."""
    symbol: str
    ts: datetime          # bar close time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.close

    def spread_pips(self, inst: Instrument) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return price_to_pips(inst, self.ask - self.bid)


class Feed:
    """Interface every data source implements."""

    def symbols(self) -> Sequence[str]:
        raise NotImplementedError

    def stream(self) -> Iterator[Bar]:
        """Yield closed bars in chronological order across all symbols."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _parse_ts(raw: str) -> datetime:
    """Accept ISO-8601, with or without a zone, plus a plain epoch second."""
    raw = str(raw).strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    text = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class CsvReplayFeed(Feed):
    """Replays OHLC CSV files, one per symbol.

    Expected header (extra columns ignored, bid/ask optional):

        timestamp,open,high,low,close[,volume][,bid][,ask]

    Files for several symbols are merged and emitted in timestamp order, so a
    multi-pair run sees the same interleaving it would see live.
    """

    def __init__(self, paths: Dict[str, str]):
        self._paths = {sym.upper(): Path(p) for sym, p in paths.items()}
        missing = [str(p) for p in self._paths.values() if not p.exists()]
        if missing:
            raise FileNotFoundError("missing data files: %s" % ", ".join(missing))

    def symbols(self) -> Sequence[str]:
        return list(self._paths.keys())

    def _rows(self, symbol: str, path: Path) -> List[Bar]:
        bars: List[Bar] = []
        with path.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return bars
            cols = {name.strip().lower(): name for name in reader.fieldnames}
            required = ("open", "high", "low", "close")
            ts_col = cols.get("timestamp") or cols.get("time") or cols.get("date")
            if ts_col is None or any(c not in cols for c in required):
                raise ValueError(
                    "%s needs timestamp,open,high,low,close columns; found %s"
                    % (path, reader.fieldnames)
                )
            for row in reader:
                try:
                    bar = Bar(
                        symbol=symbol,
                        ts=_parse_ts(row[ts_col]),
                        open=float(row[cols["open"]]),
                        high=float(row[cols["high"]]),
                        low=float(row[cols["low"]]),
                        close=float(row[cols["close"]]),
                        volume=float(row[cols["volume"]]) if "volume" in cols and row[cols["volume"]] else 0.0,
                        bid=float(row[cols["bid"]]) if "bid" in cols and row[cols["bid"]] else None,
                        ask=float(row[cols["ask"]]) if "ask" in cols and row[cols["ask"]] else None,
                    )
                except (TypeError, ValueError):
                    continue  # skip malformed rows rather than abort the run
                bars.append(bar)
        bars.sort(key=lambda b: b.ts)
        return bars

    def stream(self) -> Iterator[Bar]:
        merged: List[Bar] = []
        for sym, path in self._paths.items():
            merged.extend(self._rows(sym, path))
        merged.sort(key=lambda b: (b.ts, b.symbol))
        seen = set()
        for bar in merged:
            key = (bar.symbol, bar.ts)
            if key in seen:
                continue  # each close is processed exactly once
            seen.add(key)
            yield bar


class LiveFeed(Feed):
    """Contract for a live adapter. Intentionally not implemented.

    To wire a broker or vendor, subclass this and implement `stream()` so that
    it yields a `Bar` only when a candle has *closed*, never on every tick, and
    never re-emits a bar after a reconnect. The engine's exactly-once guarantee
    is the adapter's responsibility, because only the adapter knows what the
    upstream replayed.

    No live order path ships with this engine. Placing real orders requires a
    broker adapter you write, review, and take responsibility for.
    """

    def __init__(self, symbols: Sequence[str], **kwargs):
        self._symbols = [s.upper() for s in symbols]
        self._kwargs = kwargs

    def symbols(self) -> Sequence[str]:
        return list(self._symbols)

    def stream(self) -> Iterator[Bar]:
        raise NotImplementedError(
            "LiveFeed is a stub. Subclass it with your broker or data vendor "
            "adapter; see docs/ARCHITECTURE.md for the exactly-once contract."
        )


def synthetic_bars(
    symbol: str,
    start: datetime,
    count: int,
    start_price: float,
    minutes: int = 60,
    drift_pips_per_bar: float = 0.0,
    vol_pips: float = 6.0,
    pip_size: float = 0.0001,
    seed: int = 7,
) -> List[Bar]:
    """Deterministic random-walk bars for smoke tests and demos.

    This is scaffolding, not a market: it has no fat tails, no session volatility
    pattern and no macro response. Results produced on it say the engine runs,
    never that a strategy works.
    """
    import random

    rng = random.Random(seed)
    bars: List[Bar] = []
    price = float(start_price)
    ts = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    from datetime import timedelta

    for _ in range(count):
        step = (rng.gauss(0.0, vol_pips) + drift_pips_per_bar) * pip_size
        o = price
        c = price + step
        wick = abs(rng.gauss(0.0, vol_pips * 0.4)) * pip_size
        h = max(o, c) + wick
        l = min(o, c) - wick
        bars.append(Bar(symbol=symbol, ts=ts, open=o, high=h, low=l, close=c))
        price = c
        ts = ts + timedelta(minutes=minutes)
    return bars


def write_csv(path: str, bars: Iterable[Bar]) -> str:
    """Write bars to the CSV layout CsvReplayFeed reads."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([
                b.ts.isoformat(),
                "%.6f" % b.open, "%.6f" % b.high,
                "%.6f" % b.low, "%.6f" % b.close, b.volume,
            ])
    return str(p)
