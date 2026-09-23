#!/usr/bin/env python3
"""Download real OHLC bars into the CSV layout CsvReplayFeed reads.

    python scripts/fetch_data.py
    python scripts/fetch_data.py --symbols EUR/USD,EUR/GBP --interval 1h --range 2y

Uses a public market-data endpoint that needs no key and no account. It writes
`data/<SYMBOL>.csv` in exactly the format `engine/feed.py` expects, so nothing
else in the engine changes.

WHAT THIS DATA IS, AND IS NOT
-----------------------------
It is real price history, which is a large step up from the synthetic random
walk the engine ships with. It is still not what a broker would fill you at:

  * Indicative mid prices. There is no bid/ask, so the engine falls back to the
    `typical_spread_pips` assumption in the instrument table -- which is a
    placeholder until you replace it with your broker's published figure.
  * Free consumer-grade data. Expect occasional gaps, the odd bad print, and
    timestamps that may not agree exactly with your broker's bar boundaries.
  * Hourly history is limited to roughly the last two years at this endpoint.

Treat a result on it as a much better behaviour check, not as a backtest you
would risk money on. Buy proper tick data if you reach that point.

ONE TRAP WORTH KNOWING
----------------------
Use an intraday interval (1h is the default). With DAILY bars, every bar
carries a single timestamp that usually falls outside the London/New York
window, so the liquidity gate refuses every entry and the run takes no trades
at all. If you do use daily data, pass --any-session to the engine and set
--bar-minutes 1440.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.instruments import DEFAULT_INSTRUMENTS, get_instrument  # noqa: E402

ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
USER_AGENT = "Mozilla/5.0 (compatible; Meridian/0.1; +local research use)"
TIMEOUT = 30


def ticker_for(symbol: str) -> str:
    """Engine symbol to the vendor's FX ticker: EUR/USD -> EURUSD=X."""
    return symbol.replace("/", "").upper() + "=X"


def fetch(symbol: str, interval: str, span: str) -> list:
    """Return [(datetime_utc, open, high, low, close, volume)] for one pair."""
    url = ENDPOINT.format(ticker=ticker_for(symbol))
    url += "?interval=%s&range=%s" % (interval, span)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError("%s: %s" % (symbol, chart["error"]))
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("%s: no data returned" % symbol)

    block = results[0]
    stamps = block.get("timestamp") or []
    quote = (block.get("indicators", {}).get("quote") or [{}])[0]

    # Sanity check the vendor is quoting the pair we think it is. A silent
    # mismatch here would feed USD/JPY prices into a EUR/USD instrument spec.
    meta_ccy = (block.get("meta") or {}).get("currency")
    expected = get_instrument(symbol).quote
    if meta_ccy and meta_ccy.upper() != expected:
        raise RuntimeError(
            "%s: vendor quotes in %s but the instrument spec says %s"
            % (symbol, meta_ccy, expected)
        )

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows = []
    skipped = 0
    for index, stamp in enumerate(stamps):
        try:
            o, h, l, c = opens[index], highs[index], lows[index], closes[index]
        except IndexError:
            skipped += 1
            continue
        # Nulls are routine in this feed; a bar with a missing leg is dropped
        # rather than interpolated, because an invented bar is a fabricated
        # price the engine would happily trade.
        if None in (o, h, l, c):
            skipped += 1
            continue
        volume = 0
        if index < len(volumes) and volumes[index] is not None:
            volume = volumes[index]
        rows.append((
            datetime.fromtimestamp(int(stamp), tz=timezone.utc),
            float(o), float(h), float(l), float(c), float(volume),
        ))

    rows.sort(key=lambda r: r[0])
    # Drop duplicate timestamps: the engine requires each bar exactly once.
    deduped, seen = [], set()
    for row in rows:
        if row[0] in seen:
            skipped += 1
            continue
        seen.add(row[0])
        deduped.append(row)
    return deduped, skipped


def write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in rows:
            writer.writerow([ts.isoformat(), "%.6f" % o, "%.6f" % h,
                             "%.6f" % l, "%.6f" % c, "%g" % v])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Download real FX bars into data/ for Meridian.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Free indicative data with no bid/ask. See the module docstring.",
    )
    parser.add_argument("--symbols", default="EUR/USD,USD/JPY,GBP/USD",
                        help="comma-separated pairs (default: EUR/USD,USD/JPY,GBP/USD)")
    parser.add_argument("--interval", default="1h",
                        choices=["5m", "15m", "30m", "1h", "1d"],
                        help="bar interval (default: 1h). Daily needs --any-session "
                             "on the engine; see the module docstring")
    parser.add_argument("--range", dest="span", default="2y",
                        help="history to request, e.g. 1mo, 6mo, 1y, 2y (default: 2y)")
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in DEFAULT_INSTRUMENTS]
    if unknown:
        print("Not in the instrument table: %s" % ", ".join(unknown))
        print("Known pairs: %s" % ", ".join(sorted(DEFAULT_INSTRUMENTS)))
        return 1

    out_dir = Path(args.out)
    failures = 0
    for symbol in symbols:
        try:
            rows, skipped = fetch(symbol, args.interval, args.span)
        except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as exc:
            print("  %-8s FAILED  %s" % (symbol, exc))
            failures += 1
            continue

        if not rows:
            print("  %-8s no usable bars returned" % symbol)
            failures += 1
            continue

        path = out_dir / ("%s.csv" % symbol.replace("/", ""))
        write_csv(path, rows)
        note = " (%d incomplete bars dropped)" % skipped if skipped else ""
        print("  %-8s %5d bars  %s to %s  -> %s%s"
              % (symbol, len(rows), rows[0][0].date(), rows[-1][0].date(), path, note))

    if failures:
        print("\n%d symbol(s) failed." % failures)
        return 1

    minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}[args.interval]
    print("\nRun it with:")
    print("  python meridian.py --symbols %s --bar-minutes %d --reset-state%s"
          % (",".join(symbols), minutes, " --any-session" if minutes == 1440 else ""))
    print("\nThis is indicative mid data with no bid/ask, so the engine uses the")
    print("placeholder spreads in engine/instruments.py. Replace those with your")
    print("broker's published figures before trusting any number it produces.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
