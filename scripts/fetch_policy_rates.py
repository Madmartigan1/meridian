#!/usr/bin/env python3
"""Build a dated policy-rate history for the carry strategy.

    python scripts/fetch_policy_rates.py
    python scripts/fetch_policy_rates.py --start 2020-01-01 --out policy_rates.json

Pulls central bank policy rates and headline CPI from FRED's public CSV
endpoint (no API key, no account) and writes the JSON shape
`MacroView.load_policy_rates` reads.

WHY THIS IS A SEPARATE SCRIPT, NOT A STARTUP FETCH
---------------------------------------------------
The engine must never query the web while it runs. On a historical replay,
anything fetched "now" describes today and would be applied to bars from years
ago -- which is exactly the lookahead bias the dated rate lookup exists to
prevent. So the flow is deliberately two-step: fetch dated facts to a file
here, then replay against the file.

RATE CHANGES BECOME EFFECTIVE DATES
------------------------------------
Each series is compressed to its change points: the date a value first differs
from the previous one. For daily series that is the actual decision date. For
monthly series it is the month the change appears in, which is coarser -- the
`precision` field on every point records which you are getting.

WHAT THE NUMBERS ARE
---------------------
  USD  Federal funds target range, upper limit      (daily, exact)
  EUR  ECB deposit facility rate                    (daily, exact)
  GBP  SONIA overnight rate                         (daily, tracks Bank Rate closely)
  JPY  Overnight call rate                          (monthly, effective not target)
  CHF  Policy rate                                  (monthly)
  AUD  Cash rate                                    (monthly)

GBP and JPY are *effective* market rates rather than the published policy
target. They track it closely but are not identical, and the JPY series is
monthly. If your carry decisions hinge on a differential of a few basis points,
replace them with the central bank's own published series.

Inflation is headline CPI, year over year. The reading attached to each rate
change is the most recent one published on or before that date -- never a later
one, which would be lookahead inside the rate file itself.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
USER_AGENT = "Mozilla/5.0 (compatible; Meridian/0.1; +local research use)"
TIMEOUT = 40

# currency -> (rate series, rate precision, CPI series, description)
SERIES = {
    "USD": ("DFEDTARU", "daily", "CPALTT01USM659N", "Fed funds target range, upper limit"),
    "EUR": ("ECBDFR", "daily", "CPHPTT01EZM659N", "ECB deposit facility rate"),
    "GBP": ("IUDSOIA", "daily", "CPALTT01GBM659N", "SONIA overnight rate"),
    "JPY": ("IRSTCI01JPM156N", "monthly", "CPALTT01JPM659N", "Overnight call rate (effective)"),
    "CHF": ("IRSTCI01CHM156N", "monthly", "CPALTT01CHM659N", "Policy rate"),
    "AUD": ("IRSTCI01AUM156N", "monthly", "CPALTT01AUM659N", "Cash rate"),
}

# Below this, a move is rounding in the source series rather than a decision.
MIN_RATE_MOVE = 0.005


def fetch_series(series_id: str):
    """Return [(date, value)] for a FRED series, skipping missing observations."""
    request = urllib.request.Request(FRED_CSV.format(series=series_id),
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = response.read().decode("utf-8", errors="replace")

    if payload.lstrip().startswith("<"):
        raise RuntimeError("%s: FRED returned a web page, not CSV "
                           "(series id may be wrong or discontinued)" % series_id)

    rows = []
    reader = csv.reader(io.StringIO(payload))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise RuntimeError("%s: unexpected CSV header %r" % (series_id, header))
    for row in reader:
        if len(row) < 2 or row[1] in (".", "", None):
            continue                      # FRED marks gaps with a bare dot
        try:
            rows.append((date.fromisoformat(row[0].strip()), float(row[1])))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def change_points(rows, start: date, min_move: float):
    """Compress a series to the dates its value actually changed.

    One point is always emitted at or before `start`, carrying the level in
    force when the window opens -- otherwise the first weeks of a replay would
    have no rate and refuse every decision.
    """
    if not rows:
        return []

    opening = None
    for when, value in rows:
        if when <= start:
            opening = (start, value)
        else:
            break

    points = []
    if opening is not None:
        points.append(opening)

    last = opening[1] if opening else None
    for when, value in rows:
        if when <= start:
            continue
        if last is None or abs(value - last) >= min_move:
            points.append((when, value))
            last = value
    return points


def cpi_asof(cpi_rows, when: date):
    """Most recent CPI reading published on or before `when`.

    Never a later one: attaching next month's inflation print to this month's
    rate decision would bake lookahead into the rate file itself.
    """
    chosen = None
    for observed, value in cpi_rows:
        if observed <= when:
            chosen = value
        else:
            break
    return round(chosen, 2) if chosen is not None else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch dated central bank policy rates into policy_rates.json.")
    parser.add_argument("--start", default="2024-01-01",
                        help="earliest effective date to keep (default: 2024-01-01). "
                             "Set it before your price history starts")
    parser.add_argument("--out", default="policy_rates.json", help="output file")
    parser.add_argument("--currencies", default=",".join(SERIES),
                        help="comma-separated currencies (default: all supported)")
    args = parser.parse_args(argv)

    try:
        start = date.fromisoformat(args.start)
    except ValueError:
        print("--start must be YYYY-MM-DD")
        return 1

    wanted = [c.strip().upper() for c in args.currencies.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in SERIES]
    if unknown:
        print("No series configured for: %s" % ", ".join(unknown))
        print("Supported: %s" % ", ".join(SERIES))
        return 1

    out = {
        "_README": [
            "Generated by scripts/fetch_policy_rates.py from FRED public data.",
            "Generated on: %s" % datetime.now().date().isoformat(),
            "Window start: %s" % start.isoformat(),
            "",
            "Each point is a CHANGE in the published series, so 'effective' is the",
            "date the new level first appears. Daily series give the actual decision",
            "date; monthly series are accurate only to the month -- see 'precision'.",
            "",
            "GBP and JPY are effective market rates (SONIA, overnight call), not the",
            "published policy target. They track it closely but are not identical.",
            "",
            "'inflation' is headline CPI year-over-year as most recently published on",
            "or before that date -- never a later print.",
            "",
            "Re-run the script to refresh. Verify against each central bank's own",
            "published decisions before relying on a result.",
        ]
    }

    failures = 0
    warnings = []
    for currency in wanted:
        rate_id, precision, cpi_id, description = SERIES[currency]
        try:
            rate_rows = fetch_series(rate_id)
        except (urllib.error.URLError, RuntimeError) as exc:
            print("  %-4s FAILED  %s" % (currency, exc))
            failures += 1
            continue

        try:
            cpi_rows = fetch_series(cpi_id)
        except (urllib.error.URLError, RuntimeError) as exc:
            print("  %-4s CPI unavailable (%s); falling back to nominal rates" % (currency, exc))
            cpi_rows = []

        points = change_points(rate_rows, start, MIN_RATE_MOVE)
        if not points:
            print("  %-4s no observations at or after %s" % (currency, start))
            failures += 1
            continue

        out[currency] = [
            {
                "effective": when.isoformat(),
                "rate": round(value, 4),
                "inflation": cpi_asof(cpi_rows, when),
                "precision": precision,
                "source": "FRED:%s (%s)" % (rate_id, description),
            }
            for when, value in points
        ]
        levels = ", ".join("%.2f" % v for _d, v in points[:6])
        print("  %-4s %2d change points from %s  [%s%s]"
              % (currency, len(points), points[0][0], levels,
                 ", ..." if len(points) > 6 else ""))

        # Quality gates. A file that looks populated but is not usable is worse
        # than one that is obviously empty, because the run still produces a
        # confident number.
        if len(points) <= 1:
            warnings.append(
                "%s has only %d point(s): the source series looks stale or "
                "discontinued, so every bar would see the same %s rate. Do not "
                "trade pairs touching %s on this file."
                % (currency, len(points), points[0][0], currency))
        if not any(p["inflation"] is not None for p in out[currency]):
            warnings.append(
                "%s has no inflation data, so its carry is computed from NOMINAL "
                "rates. That overstates the appeal of a high-inflation currency -- "
                "the real rate is what attracts capital." % currency)

    if len(out) <= 1:
        print("\nNothing fetched; not writing %s." % args.out)
        return 1

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\nWrote %s" % args.out)
    print("Run carry with:")
    print("  python meridian.py --strategy carry --policy-rates %s --reset-state" % args.out)
    if failures:
        print("\n%d currency(ies) failed; pairs touching them will refuse to trade."
              % failures)

    if warnings:
        # Written into the file as well as printed. A file that looks populated
        # but is not usable is worse than an obviously empty one, because the
        # run still produces a confident number from it.
        print("\nDATA QUALITY WARNINGS")
        for note in warnings:
            print("  ! %s" % note)
        out["_README"].extend([""] + ["WARNING: " + w for w in warnings])
        Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
