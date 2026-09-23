# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

---

## What this project is

Meridian is a **simulation-only foreign exchange decision engine**. It combines
EMA/RSI/MACD/ATR technical signals with a macro layer (rate differentials, data
surprise, release blackouts), risk-based position sizing, and portfolio-level
currency exposure controls.

**There is no live trading path and no broker integration.** `--live` prints
what is missing and exits non-zero. This is deliberate. Do not add an order path
without an explicit request.

**Safety assumption:** treat every change as potentially affecting how someone
sizes real risk. Run the tests before and after.

---

## Running

```bash
python meridian.py --generate-data    # synthetic sample CSVs
python meridian.py --reset-state      # run
python meridian.py --help             # all flags
python meridian_gui.py                # desktop app (needs PySide6)

for f in tests/test_*.py; do python "$f" || exit 1; done
```

Python 3.10+. The engine, CLI and engine tests are standard library only; the
GUI needs PySide6 and is optional. `tests/test_gui_controller.py` is the only
test that imports Qt.

---

## Repository layout (fixed — do not reorganise)

```
engine/       core engine (no Qt import, ever)
gui/          desktop app - PySide6, optional
gui/widgets/  one file per tab
tests/        standalone test scripts (no framework)
docs/         canonical documentation
data/         OHLC CSVs (not versioned)
.state/       runtime state (not versioned)
```

No `.py` files in the root except the `meridian.py` and `meridian_gui.py`
entrypoint shims. `engine/` must never import from `gui/`, and must never
import Qt — the CLI has to run without it installed.

---

## Architecture

**One path, not two.** Replay and live run through identical code. There is no
separate backtest implementation, because that is how a strategy comes to
behave differently in the mode that costs money.

**Per-bar order (do not reorder):**

1. update the rate book
2. roll the day, apply rollover financing
3. update indicators
4. age the open position
5. bail out if the market is shut
6. **manage the position** — stop checked first, always
7. **consider an entry** — only if flat and every gate agrees
8. mark equity

Step 6 precedes step 7 so a bar that both stops out and signals settles the loss
before sizing the next trade.

**Dependency direction:** everything sits on `engine/instruments.py`. Pip size,
lot size, contract size and the base/quote split are declared there once, and
nowhere else may hardcode `0.0001`, `100_000`, or a currency split.

---

## Key files

| File | Role |
|---|---|
| `engine/instruments.py` | Pip/lot conventions, `RateBook`, pip value — **everything depends on this** |
| `engine/position.py` | Signed position book, netting, flips — high impact |
| `engine/sizing.py` | Risk-based sizing and margin — high impact |
| `engine/exits.py` | Mandatory stop, trailing, exit evaluation — high impact |
| `engine/risk.py` | Currency exposure netting and entry gates — high impact |
| `engine/sessions.py` | 24×5 calendar, rollover, financing |
| `engine/macro.py` | Carry bias, data surprise, release blackout |
| `engine/engine.py` | The per-bar loop and run report |
| `engine/config.py` | Canonical defaults, hard ceilings, `validate()` |
| `docs/FX_PRIMER.md` | Market mechanics and the honest retail-outcomes answer |
| `gui/controller.py` | Worker thread and the only engine-to-GUI bridge |
| `AGENTS.md` | **Authoritative guidelines — read before changing anything** |

---

## The FX-specific traps

These are the things that differ from a spot/crypto engine and silently corrupt
results if handled by analogy:

1. **`SELL` is a direction, not an exit.** Opening short is a first-class
   action. An opposing fill larger than the open position flips it. `close()` is
   a separate operation that can never flip.
2. **JPY-quoted pairs use a 0.01 pip**, everything else 0.0001.
3. **Two different conversion legs.** Pip value/PnL is quote→account; notional
   and margin are base→account. On USD/JPY these differ by ~150×.
4. **Sizing runs backwards from the stop**, not forwards from a notional. Lots
   round **down**; below the minimum ticket the answer is zero.
5. **24×5, not 24×7.** Weekend gaps, rollover at 17:00 New York, triple swap on
   Wednesday, and max-hold counted in **market** hours.
6. **No maker execution.** Retail FX is price-taking: buy the ask, sell the bid,
   pay the spread twice. Do not port a post-only/maker-first path here.
7. **Exposure nets per currency.** Long EUR/USD + long GBP/USD is a doubled
   short USD, not two diversified trades.

---

## Non-negotiable rules

- **Every position carries a stop.** No `enable_stop` flag exists; do not add
  one. `PositionBook.apply_fill` raises without a stop price.
- Stops only ever tighten.
- Refuse rather than guess — a missing rate means no trade, not an assumed
  pip value.
- Never take an assumption that flatters a result. The stop wins a bar
  containing both levels; slippage is always adverse; gapped stops fill at the
  open.
- Simulated and live state never share a file. Paths come only from
  `constants.state_paths(simulated)`.
- Macro may shrink a trade or refuse one; it may never enlarge one.
- Every refusal logs a stable single-token code before the colon.
- **No lookahead.** A dated query returns the value in force at that bar or
  nothing. An undated policy rate can never satisfy one. Applying today's
  rate to an older bar fabricates the result.

### GUI rules

- The engine runs on one worker thread and **never touches a widget**. Every
  widget mutation happens on the Qt main thread.
- Everything engine→GUI goes through `gui/controller.py` as a Qt signal.
- **Only plain data crosses threads.** Never hand a live `Engine` or `Position`
  to the GUI — use `controller.snapshot()`. The worker keeps mutating them.
- Both front ends call `Engine.run(observer=...)`. Never iterate the feed from
  the GUI: that duplicates the shutdown sequence and the copies will drift.
- The log bridge attaches to the shared `meridian` logger, so it must be removed
  in a `finally` or the next run double-logs.
- Stopping is cooperative. A stop finishes the current bar and runs the normal
  shutdown, so open positions are still settled.

---

## Workflow for changes

1. Explain the plan (3–8 bullets).
2. Make the smallest change that solves the problem.
3. Add a test that fails before and passes after — and verify it can fail.
4. Run the full test suite (all `tests/test_*.py`).
5. Run the engine end-to-end and check the report is coherent.
6. For cost/sizing/exit changes, re-run at `--spread-mult 2.0`.
7. Note rollback steps for `position.py`, `sizing.py` or `exits.py`.

---

## Honesty requirements

This project makes claims in a domain where overclaiming costs people money.

- Never present a simulated result as evidence of profitability.
- Keep placeholder data (policy rates, swaps, typical spreads) labelled as
  placeholders.
- If a change improves results, establish why before accepting it. The prior is
  that it removed a cost or a guard.
- Do not soften `docs/FX_PRIMER.md` §1.

---

## Docs to consult

- `AGENTS.md` — authoritative developer guidelines
- `docs/ARCHITECTURE.md` — module boundaries, data flow, invariants
- `docs/OPERATIONAL_SPEC.md` — exact per-bar behaviour
- `docs/KNOB_REFERENCE.md` — every parameter and safe range
- `docs/RUNBOOK.md` — reading output, failure modes
- `docs/FX_PRIMER.md` — the market mechanics behind the design
