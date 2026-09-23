# Quickstart

From a fresh clone to a run you can interpret, in about five minutes. No
credentials and no network.

Requires Python 3.10 or newer. The engine, CLI and engine tests need nothing
installed; the optional desktop app needs PySide6.

---

## 1. Verify the arithmetic first

Before running anything, confirm the numeric primitives are sound on your
machine. Everything the engine reports is built on them.

```bash
python tests/test_instruments.py
python tests/test_sizing.py
python tests/test_position.py
python tests/test_sessions.py
python tests/test_exits.py
python tests/test_risk_exposure.py
python tests/test_macro.py
python tests/test_gui_controller.py    # needs PySide6; skip if not installed
```

All should report every test passing. If any fail, stop — the engine will still
run, and its numbers will be wrong.

---

## 2. Generate sample data

```bash
python meridian.py --generate-data
```

This writes `data/EURUSD.csv`, `data/USDJPY.csv` and `data/GBPUSD.csv` —
deterministic random-walk bars, 2000 of them per pair by default.

**These are scaffolding, not a market.** They have no fat tails, no session
volatility pattern, and no response to macro events. They prove the engine
executes and nothing else.

---

## 3. Run it

```bash
python meridian.py --reset-state
```

You will see, in order:

- the **round-trip cost** per pair, printed before the first trade,
- `ENTER` lines with direction, size, fill, stop, stop distance and the money
  at risk,
- `EXIT` lines with pips, financing, money and the reason,
- `SKIP` lines whenever a gate refused something,
- the run report.

---

## 4. Read the report honestly

```
  bars processed      4500
  signals             1828  (vetoed 1223, blocked 500)
  trades              105 entries / 105 exits
  closed              105  (42 win / 63 loss, 40.0%)
  gross pips          -141.2
  financing pips      -3.49
  net PnL             -405.94 USD  (-4.06%)
  max drawdown        571.19 USD
  refusals            macd_not_confirming=1089, currency_exposure=252, ...
  exits by reason     stop hit=67, take profit=17, flat into weekend close=14, ...
```

Read it in this order:

1. **Did it trade at all?** Zero entries with thousands of signals means a gate
   is refusing everything — the `refusals` line names which.
2. **Exits by reason.** Mostly stops means the stop is inside the noise, or the
   signal has no edge. Mostly stagnation means the entries are not going
   anywhere.
3. **Gross pips against the cost line.** This is the real question. A strategy
   losing on a random walk is behaving correctly: on a driftless series, costs
   are the only systematic term, so a loss of roughly (trades × round-trip
   cost) is the *expected* outcome, not a bug.
4. **Net PnL last.** It is the least informative number on a synthetic series.

---

## 5. Stress the costs

```bash
python meridian.py --reset-state --spread-mult 2.0
```

Any result that inverts under doubled costs was living inside its cost
assumption. Make this a habit before drawing a conclusion from anything.

---

## 6. Use your own data

Drop OHLC CSVs into `data/`, named for the pair with the slash removed:

```
data/EURUSD.csv
data/GBPJPY.csv
```

```
timestamp,open,high,low,close,volume,bid,ask
2026-01-14T15:00:00+00:00,1.08500,1.08620,1.08450,1.08590,0,1.08585,1.08595
```

- `timestamp` may be ISO-8601 (with or without a zone — naive is treated as
  UTC) or a plain epoch second.
- `volume`, `bid` and `ask` are optional. With bid/ask present, the real spread
  is used instead of the instrument default.
- Rows that fail to parse are skipped rather than aborting the run.

Then tell the engine what it is looking at:

```bash
python meridian.py --symbols EUR/USD,GBP/JPY --bar-minutes 60 --reset-state
```

**`--bar-minutes` must match your data.** Nothing verifies this. Mismatched,
every time-based rule is wrong by the ratio.

---

## 7. Save a configuration

```bash
python meridian.py --risk-pct 0.25 --atr-stop-mult 2.5 --save-config my_setup.json
python meridian.py --config my_setup.json --reset-state
```

Flags still override a loaded config, so a run is always reproducible from its
command line.

---

## 8. Or use the desktop app

```bash
pip install PySide6
python meridian_gui.py
```

Same engine, same config, same numbers. Configure on **Setup** and
**Strategy & Risk**, press **Start run**, and watch the Dashboard. The panel
headed *Why entries were refused* is the one to read when nothing trades.

**Stop** is cooperative: it finishes the current bar, settles open positions and
writes state. It never cuts a run off mid-bar.

---

## Where to go next

- `docs/FX_PRIMER.md` — the mechanics, and an honest answer on retail FX
- `docs/KNOB_REFERENCE.md` — every parameter and its safe range
- `docs/RUNBOOK.md` — troubleshooting and the failure modes worth recognising
- `docs/OPERATIONAL_SPEC.md` — exactly what happens on each bar
