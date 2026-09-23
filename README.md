# Meridian — a supervised FX decision engine

Meridian is a **simulation-first foreign exchange engine** for self-directed
operators who want to understand how a currency strategy behaves once the costs
are modelled honestly.

It combines deterministic technical signals (EMA / RSI / MACD / ATR) with a
macro layer (rate differentials, data-surprise tilt, release blackouts),
risk-based position sizing, and portfolio-level currency exposure controls —
with every decision, veto and refusal surfaced as a reason rather than a
number.

Meridian is built to be **inspectable, supervised and honest about costs**. It
is not a money machine, and it ships with no live order path.

---

## Read this first

The foreign exchange market is genuinely enormous and prices genuinely move.
Retail short-horizon FX speculation is nonetheless a documented losing
proposition for the large majority of participants — brokers required to
publish the figure consistently report **70–80% of retail accounts losing
money**. The cause is structural: spread on every round trip, financing on
every hold, slippage where it hurts most, and leverage multiplying all of it.

Meridian exists to let you *see that arithmetic operate* on a strategy you
control, not to beat it.

**`docs/FX_PRIMER.md` explains this properly, including what does persist in FX
(carry, longer-horizon momentum) and at what horizon.** Read it before the
configuration reference.

---

## What it does

### Signals
- **EMA crossover** with a pip deadband and a persistence requirement
- **RSI, MACD and an ATR volatility floor** as advisory gates that may only veto
- Fully **two-sided**: a bearish cross is a short entry candidate, not an exit

### Two strategies, one engine
- **`--strategy trend`** — EMA crossover with a deadband, confirmation and advisor gates
- **`--strategy carry`** — hold the higher **real** (inflation-adjusted) yielder, the
  one FX return source with a long documented history
- Carry queries the policy rate **in force at the bar being processed**. An undated
  rate table is *refused*, not silently back-applied — that refusal is the difference
  between a carry backtest and a fabricated one
- Switching strategies is one flag and touches one file

### Macro layer
- **Carry bias** from real (inflation-adjusted) policy rate differentials
- **Data-surprise tilt** — actual against forecast, decayed over time, signed
  per leg of the pair
- **Release blackout** — no fresh entries in the window around a high-impact
  print, for either currency in the pair
- Bounded by construction: macro can shrink a trade or refuse one, never enlarge it

### Risk
- **Mandatory stop on every position.** Not a setting. There is no flag to disable it.
- **Risk-based sizing** — lots follow from the stop distance, so the amount at
  risk is constant regardless of pair, volatility or pip convention
- **Currency exposure netting** — long EUR/USD + long GBP/USD + short USD/JPY is
  recognised as one concentrated dollar position, not three diversified trades
- Daily loss halt, total open-risk cap, margin utilisation cap, position count cap

### FX-native mechanics
- Pip and lot conventions per instrument, **including the JPY 0.01 pip**
- Pip value converted to account currency through the correct leg (quote→account
  for PnL, base→account for margin — they are different)
- **24×5 calendar** — Sunday open, Friday close, optional flat-into-the-weekend
- **Rollover financing** at 17:00 New York, **tripled on Wednesday**
- Max-hold counted in **market hours**, so a weekend does not expire a position
- Spread paid on both legs, adverse slippage, and **gapped stop fills**

### Desktop app (optional)
- Dashboard: equity, drawdown, open positions, and **why entries were refused**
- Strategy & Risk panel bound to the same `EngineConfig` the CLI uses
- Live log pane, cooperative stop, config save/load
- Controls lock while a run is in progress, so nothing edited mid-run silently does nothing

### Observability
- Every entry, veto and refusal logged with a reason
- Refusal codes aggregate into a run report instead of degenerating into one-offs
- Round-trip cost printed per pair **before** the first trade

---

## Quick start

No credentials and no network. The engine, CLI and engine tests need only the
Python standard library; the desktop app additionally needs PySide6.

```bash
python meridian.py --generate-data      # write synthetic sample CSVs
python meridian.py                      # run the engine over them
python meridian.py --help               # all flags
python meridian.py --strategy carry --policy-rates policy_rates.json
```

Or the desktop app (needs `pip install PySide6`):

```bash
python meridian_gui.py
```

Then run the tests:

```bash
python tests/test_instruments.py
# or all of them, see tests/README.md
```

To use your own data, drop OHLC CSVs into `data/` named `EURUSD.csv`,
`USDJPY.csv` and so on:

```
timestamp,open,high,low,close[,volume][,bid][,ask]
2026-01-14T15:00:00+00:00,1.08500,1.08620,1.08450,1.08590
```

---

## Repository layout

```
gui/             # desktop app (PySide6) - optional
  controller.py  #   worker thread + the only engine-to-GUI bridge
  main_window.py #   tabs, run controls, disclaimer banner
  widgets/       #   dashboard, setup, strategy/risk, logs
engine/          # the engine
  instruments.py #   pip/lot conventions, pip value, rate conversion
  position.py    #   signed position model with netting
  sizing.py      #   risk-based sizing and margin
  execution.py   #   spread, slippage, commission, gapped stop fills
  exits.py       #   mandatory stop, take profit, trailing, stagnation, max hold
  risk.py        #   currency exposure netting and portfolio gates
  sessions.py    #   24x5 calendar, rollover, financing
  macro.py       #   carry, surprise, release blackout
  indicators.py  #   streaming EMA / RSI / MACD / ATR
  strategy.py    #   signal trigger and advisory gates
  feed.py        #   CSV replay + documented live stub
  engine.py      #   the per-bar decision loop
  config.py      #   canonical defaults and validation
tests/           # standalone test scripts, no framework required
docs/            # architecture, primer, knobs, runbook, spec
data/            # OHLC CSVs (generated or your own)
.state/          # local run state, not versioned
```

---

## Documentation

| Document | What it covers |
|---|---|
| `docs/FX_PRIMER.md` | Market mechanics, and the honest answer on retail FX |
| `docs/ARCHITECTURE.md` | Module boundaries, data flow, invariants |
| `docs/OPERATIONAL_SPEC.md` | Per-bar behaviour in operator terms |
| `docs/KNOB_REFERENCE.md` | Every configuration parameter and its safe range |
| `docs/RUNBOOK.md` | Running, reading the report, troubleshooting |
| `docs/QUICKSTART.md` | First run, start to finish |
| `AGENTS.md` | Developer and contributor guidelines |

---

## Project status

**Pre-1.0.** The engine core, risk model and FX arithmetic are stable and
tested (91 tests). The desktop GUI is a working skeleton: configure, run,
watch positions and refusals live, stop cleanly. Charts are not built. Live
execution is deliberately unimplemented and there is no broker integration.

---

## License

MIT — see `LICENSE`.

The engine and CLI are standard library only, so they carry no third-party
obligations. The optional GUI depends on PySide6, which is LGPL v3; see
`THIRD_PARTY.md` for what that means if you redistribute a packaged build, plus
the terms on the bundled data fetchers.

---

## Disclaimer

Meridian is an educational and analytical tool for self-directed operators. It
does not provide financial advice, does not predict prices, and does not
guarantee any result. Simulated results are behaviour checks against
assumptions you set, not estimates of future profit.

See `RISK_DISCLOSURE.txt`.
