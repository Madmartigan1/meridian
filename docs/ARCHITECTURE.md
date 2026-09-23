# Meridian Architecture

## Design stance

Meridian is a single-threaded, streaming engine. It consumes one closed bar at
a time and holds O(1) state per symbol, so a replay of five years and a live
session run through **identical code**. There is no separate backtest path,
because a separate backtest path is how a strategy comes to behave differently
in the one mode that costs money.

Three properties are structural rather than configurable:

1. **Every position carries a stop.** Enforced at the position book, not by policy.
2. **Simulated and live state never share a file.** Enforced by a single path resolver.
3. **Each closed bar is processed exactly once.** The feed adapter owns this guarantee.

---

## Module map

```
                          ┌─────────────┐
                          │   feed.py   │  Bar objects, chronological, once each
                          └──────┬──────┘
                                 │
                          ┌──────▼──────┐
                          │  engine.py  │  the per-bar decision loop
                          └──────┬──────┘
          ┌──────────────┬───────┼───────┬──────────────┬─────────────┐
          │              │       │       │              │             │
   ┌──────▼─────┐ ┌──────▼────┐ │ ┌─────▼──────┐ ┌─────▼─────┐ ┌─────▼──────┐
   │indicators  │ │ strategy  │ │ │  exits.py  │ │  risk.py  │ │  macro.py  │
   │ EMA/RSI/   │ │ trigger + │ │ │ stop first │ │ exposure  │ │ carry,     │
   │ MACD/ATR   │ │ gates     │ │ │ always     │ │ netting   │ │ blackout   │
   └────────────┘ └───────────┘ │ └────────────┘ └───────────┘ └────────────┘
                                │
          ┌─────────────┬───────┴───────┬───────────────┐
   ┌──────▼──────┐ ┌────▼─────┐ ┌───────▼──────┐ ┌──────▼────────┐
   │ position.py │ │sizing.py │ │ execution.py │ │  sessions.py  │
   │ signed book │ │ risk →   │ │ spread,      │ │ 24x5 calendar │
   │ + netting   │ │ lots     │ │ slip, gaps   │ │ + financing   │
   └─────────────┘ └──────────┘ └──────────────┘ └───────────────┘
                                │
                        ┌───────▼────────┐
                        │ instruments.py │  pip/lot conventions, pip value
                        └────────────────┘  ← everything depends on this
```

`instruments.py` sits at the bottom deliberately. Pip size, lot size, contract
size and the base/quote split are declared **once**, and no other module may
hardcode `0.0001`, `100_000`, or a currency split. A mistake there is invisible
at runtime and wrong everywhere, which is why it has the most direct test
coverage in the repository.

---

## Source layout

| File | Role |
|---|---|
| `engine/instruments.py` | Instrument specs, pip↔price, lot rounding, `RateBook`, pip value |
| `engine/position.py` | `Position`, `PositionBook`, netting, flips, `ClosedLeg` |
| `engine/sizing.py` | Risk-based sizing, margin, pips→money |
| `engine/execution.py` | Quote resolution, spread, slippage, gapped stop fills, cost baseline |
| `engine/exits.py` | Stop distance from ATR, initial levels, trailing, exit evaluation |
| `engine/risk.py` | Currency exposure, open risk, margin use, the entry gate, daily halt |
| `engine/sessions.py` | NY offsets, week boundaries, rollover weighting, `SwapBook` |
| `engine/macro.py` | `PolicyRate`, `EconomicEvent`, carry bias, surprise tilt, blackout |
| `engine/indicators.py` | Streaming EMA / RSI / MACD / ATR and the per-symbol bundle |
| `engine/strategy.py` | `TrendSignal` trigger and the advisory gates |
| `engine/feed.py` | `Bar`, `Feed`, `CsvReplayFeed`, `LiveFeed` stub, synthetic generator |
| `engine/persistence.py` | Atomic state writes, trade ledger |
| `engine/config.py` | `EngineConfig`, hard ceilings, `validate()` |
| `engine/constants.py` | State paths and the simulated/live split |
| `engine/engine.py` | The loop, accounting, run report |
| `engine/main.py` | CLI parsing and wiring |
| `meridian.py` | Entrypoint shim |

---

## The per-bar order, and why it is that order

```
for each closed bar:
    1. update the rate book with the close
    2. roll the trading day, apply financing for any rollover crossed
    3. update indicators for this symbol
    4. age the open position (bars held, last price)
    5. if the market is shut: mark equity and stop here
    6. MANAGE the open position   ← exits, stop checked first
    7. CONSIDER an entry           ← only if flat, only if every gate agrees
    8. mark equity
```

**Exits before entries (6 before 7).** A bar that both stops a position out and
triggers a fresh signal must settle the loss first, so the new trade is sized
against the equity that actually remains. Reversed, the engine sizes the next
trade off money it has already lost.

**Financing before indicators (2 before 3).** Swap accrues on the position as it
was held, not as it will be after this bar's decision.

**Market-closed check after ageing (5 after 4).** A bar outside trading hours
still updates indicators and marks the book; it simply cannot produce a
decision.

---

## Signal flow

```
IndicatorSet.ready ──no──► "warmup: N bars left"
        │ yes
        ▼
EMA separation vs. deadband ──inside──► FLAT, reason logged
        │ outside
        ▼
held for confirm_bars? ──no──► "forming (2/3 bars)"
        │ yes
        ▼
advisor gates: RSI / MACD / ATR floor ──veto──► Signal(side, vetoed_by=...)
        │ pass
        ▼
macro: blackout? carry veto? ──block──► SKIP with reason
        │ allow, with a bias score
        ▼
stop distance from ATR, clamped ──inside costs──► refuse
        │
        ▼
risk-based sizing ──below min ticket / no rate / no margin──► refuse
        │
        ▼
macro bias may SHRINK the size (never enlarge)
        │
        ▼
risk gate: exposure, total risk, margin, positions, session, daily halt
        │ ok
        ▼
execute: spread + adverse slippage, spread cap enforced
        │ filled
        ▼
place stop and take profit FROM THE FILL, not from the signal price
```

The last line matters: anchoring levels to the close rather than to the fill
understates the stop distance by exactly the spread and slippage just paid.

---

## The two-sided position model

This is the structural difference from a spot long-only engine and the one that
silently corrupts everything downstream if it is wrong.

In spot, `SELL` means exit and size cannot go below zero. In FX, `SELL` is a
**direction**:

- `apply_fill(side=SHORT, ...)` on a flat book **opens** a short.
- A same-direction fill sizes up at the lot-weighted average entry.
- An opposing fill **reduces**, returning a `ClosedLeg` for the settled slice.
- An opposing fill **larger** than the open position closes it entirely and
  opens the remainder the other way — a flip — returning the settled leg.
- `close()` is a separate operation that can never flip, however oversized the
  request. An exit must not be able to open a trade.

One net position per symbol. Hedged positions (simultaneous long and short on
one pair) are deliberately unsupported: they double financing for zero net
exposure and make currency netting unreadable.

---

## The conversion legs, which are not the same leg

Two conversions are needed, and confusing them is a classic FX accounting bug:

| Quantity | Currency it arises in | Conversion leg |
|---|---|---|
| Pip value / PnL | **Quote** currency | quote → account |
| Notional / margin | **Base** currency | base → account |

On USD/JPY for a USD account these differ by a factor of 150. `RateBook`
resolves either directly, by inversion, or through one triangular hop, and
returns `None` rather than guessing — callers then refuse to trade rather than
size against unknown risk.

---

## State and persistence

```
.state/                          # live (never written in simulated mode)
  positions.json
  trades.csv
  account.json
  daily_risk.json
  sim_data/                      # simulated
    sim_positions.json
    sim_trades.csv
    sim_account.json
    sim_daily_risk.json
```

`constants.state_paths(simulated)` is the only place a path is chosen. Nothing
assembles one by hand, so a confused caller cannot write simulated results into
a live ledger.

Override the root with `MERIDIAN_STATE_DIR`. Packaged builds write under
`%LOCALAPPDATA%\Meridian\state`; a development clone uses `<repo>/.state` when
writable.

Writes go through a temp file and `os.replace`, so an interrupted run leaves the
previous state readable rather than a truncated file. `Store.reset()` refuses to
run against live state at all.

---

## The GUI seam

The desktop app (`gui/`) is a front end over the same engine, not a second
engine. It is optional: the CLI and every engine test run without Qt installed.

```
gui/app.py  →  gui/main_window.py  →  gui/controller.py  →  engine/engine.py
                                            │
                              QThread worker │ queued signals
                                            ▼
                                    Qt main thread only
```

**`Engine.run(observer=...)` is the seam.** Both front ends call the same
method, so the loop, the end-of-run flatten and the state write cannot drift
between them. The controller does *not* iterate the feed itself — doing so
would duplicate the shutdown sequence in the GUI path, and the two copies would
diverge the first time either changed.

Three rules, enforced by `tests/test_gui_controller.py`:

1. **The engine never touches a widget.** It runs on one worker thread and
   communicates only by Qt signal, which is queued across threads by default,
   so every slot runs on the main thread.
2. **Only plain data crosses.** `controller.snapshot()` flattens live state into
   primitives, lists and dicts. Handing over a live `Engine` or `Position` would
   let the GUI read an object the worker is still mutating.
3. **The log bridge is installed at start and removed in a `finally`.** It
   attaches to the shared `meridian` logger; leaking it double-logs every
   subsequent run.

Stopping is cooperative: `Engine.request_stop()` sets a flag, the loop finishes
the current bar, and the normal shutdown still settles open positions and
persists state. A stop is never a kill.

## Feed contract

`Feed.stream()` yields `Bar` objects that are:

- **closed** — never a forming candle,
- **chronological** — merged across symbols in timestamp order,
- **exactly once** — a bar repeated after a reconnect double-counts a crossover.

`CsvReplayFeed` enforces all three by sorting and de-duplicating on
`(symbol, timestamp)`. A `LiveFeed` subclass must enforce them itself, because
only the adapter knows what the upstream replayed. That responsibility is stated
in the stub's docstring rather than left implicit.

---

## Not built

**Charts.** The dashboard shows numbers and tables, not price plots. A
per-pair price and EMA chart would be the obvious next addition.

**Live execution.** There is no broker adapter and no code that places an order.
`--live` reports what is missing and exits non-zero. Adding one means writing
an order path, a fill reconciler, and a reconnect-safe position sync — none of
which can be inferred from the simulated path, and all of which should be
reviewed as carefully as the risk model.

**Optimiser.** Deliberately absent. Searching parameters against one history
produces a curve fit that looks like an edge and behaves like noise.
