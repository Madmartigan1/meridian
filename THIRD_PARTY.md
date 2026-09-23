# Third-party components and data sources

Meridian itself is MIT licensed (see `LICENSE`). This file records what it
depends on and under what terms, so anyone redistributing it — or building
something commercial on top of it — knows where the obligations sit.

---

## Runtime dependencies

### The engine: none

`engine/`, the CLI (`meridian.py`) and every test except
`tests/test_gui_controller.py` use only the Python standard library. There is
nothing to license, attribute, or bundle.

This is deliberate, and it is the reason the engine can be vendored into other
projects without dragging obligations along.

### The GUI: PySide6 (LGPL v3)

`gui/` requires PySide6, the official Python binding for Qt, distributed under
the **GNU Lesser General Public License v3**. Meridian's own MIT license does
not extend to it.

LGPL permits use in proprietary and commercial products, but attaches
conditions. If you redistribute a packaged build that bundles Qt:

- Link **dynamically**. Ship the Qt libraries as separate files rather than
  statically linked into a single binary.
- Include the LGPL v3 license text and attribute PySide6/Qt.
- Allow the end user to replace the Qt libraries with their own build.

Freezing tools such as PyInstaller can blur the dynamic-linking boundary.
Check how your build lays Qt out before distributing it.

**If you do not ship the GUI, none of this applies.** The engine and CLI are
fully functional without Qt installed.

- PySide6 / Qt for Python — https://www.qt.io/qt-for-python
- LGPL v3 — https://www.gnu.org/licenses/lgpl-3.0.html

---

## Data sources

Neither source is bundled with this repository. Both are fetched on demand by
scripts the user runs deliberately, and neither is contacted by the engine
while it is running. Data files are gitignored.

### FRED — policy rates and CPI (`scripts/fetch_policy_rates.py`)

Federal Reserve Bank of St. Louis economic data, retrieved from the public CSV
endpoint. FRED aggregates series from many statistical agencies; most US
federal government data is public domain, and FRED's terms permit free use and
redistribution.

Individual series may carry the terms of their originating agency. Check the
source attribution on a series before redistributing derived data.

- https://fred.stlouisfed.org/

### Price history (`scripts/fetch_data.py`)

The included price fetcher reads an **undocumented public endpoint** operated by
a consumer finance portal. This is a convenience for personal research and it
comes with a real caveat:

> **Do not rely on it in anything commercial or redistributed.** The endpoint is
> undocumented, unsupported, may change or disappear without notice, and its
> operator's terms generally restrict commercial use and redistribution of the
> data.

For anything beyond personal experimentation, replace it with a provider whose
licence you have read. The engine does not care where bars come from — the CSV
format in `docs/QUICKSTART.md` is the whole interface, and `CsvReplayFeed`
reads any file matching it. Swapping sources is a one-file change.

The data itself is indicative mid prices with no bid/ask, so the engine falls
back to the placeholder spreads in `engine/instruments.py`. Those are **not**
real broker figures and must be replaced before any result is meaningful.

---

## What ships with no warranty of accuracy

Several values in this repository are **placeholders in the correct shape**,
not market data. They are labelled as such wherever they appear:

| Where | What |
|---|---|
| `engine/instruments.py` | `typical_spread_pips`, swap rates per pair |
| `engine/macro.py` (`default_macro_view`) | Policy rates — deliberately **undated**, so the engine refuses them rather than back-applying them |
| `policy_rates.example.json` | Illustrative rate history; the loader warns on any filename containing "example" |

Replace them with your broker's published contract specifications and real
central bank decisions before drawing a conclusion from any number this
software produces.

---

## Scope

Meridian is a **simulation-only** research tool. It contains no broker
integration and no code that places an order; `--live` reports what is missing
and exits non-zero.

See `RISK_DISCLOSURE.txt` for what that means for anyone thinking about using
it to inform real trading decisions.
