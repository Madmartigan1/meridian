# AGENTS.md — Meridian (FX decision engine)

Authoritative guidelines for anyone — human or agent — changing this codebase.
Read this before making any change.

---

## 1. Core principles

1. **The stop is not negotiable.** There is no `enable_stop` flag anywhere, and
   adding one is not an acceptable change. `PositionBook.apply_fill` raises if
   asked to open without a stop price. If a change makes that raise
   inconvenient, the change is wrong.

2. **Refuse rather than guess.** When a conversion rate is missing, a size is
   below the minimum ticket, or a spread is blown out, the engine declines and
   says why. Never substitute a plausible default — a guessed pip value sizes a
   trade against unknown risk, which is strictly worse than not trading.

3. **Never flatter a result.** Where an assumption could go either way, take the
   pessimistic one: the stop wins a bar containing both levels, slippage is
   always adverse, gapped stops fill at the open, open positions pay their exit
   spread at the end of a run. Reversing any of these makes every number look
   better and mean less.

4. **Every decision carries a reason.** Entries, vetoes and refusals all log
   why. Refusal codes are stable single tokens before a colon so reports can
   aggregate them. A silent skip is a bug.

5. **FX conventions live in one place.** `engine/instruments.py` owns pip size,
   lot size, contract size and the base/quote split. No other module may
   hardcode `0.0001`, `100_000`, or a currency split.

6. **No dead code.** Do not add `_debug_*`, `_self_test_*`, `sanity_check*` or
   similar helpers to engine modules. Tests belong in `tests/`.

---

## 2. Repository layout (fixed)

```
engine/       core engine - never imports Qt or anything from gui/
gui/          desktop app (PySide6), optional
gui/widgets/  one file per tab
tests/        standalone test scripts
docs/         canonical documentation
data/         OHLC CSVs (not versioned)
.state/       runtime state (not versioned)
```

The dependency arrow points one way: `gui/` imports `engine/`, never the
reverse. The CLI and every engine test must keep running with Qt uninstalled.

Do not move files between directories, and do not create `.py` files in the
repository root other than the `meridian.py` and `meridian_gui.py`
entrypoint shims.

---

## 3. The parts that are high-impact

| Module | Why care |
|---|---|
| `instruments.py` | Everything numeric is built on it. A mistake here is invisible at runtime and wrong everywhere |
| `position.py` | Netting and flips. A bug corrupts exposure, risk, margin and PnL at once, while the engine keeps running happily |
| `sizing.py` | Decides how much is at risk. Changes here need the risk invariant re-verified, not just a passing run |
| `exits.py` | Stop semantics. Every bug in this file flatters results |
| `risk.py` | The gates. Loosening one is a decision, not a cleanup |
| `gui/controller.py` | The thread boundary. Bugs here are intermittent and blamed on the engine |

Changes to these require a test that fails before the change and passes after.

---

## 4. Invariants that must survive any change

- Every open position has a stop price; stops only ever tighten.
- Realised risk per trade is at or below `risk_per_trade_pct × equity`, never above.
- Lot sizes round **down**; below the minimum ticket the answer is zero, meaning
  "do not trade", never "trade something".
- Pip value uses the **quote→account** leg; margin uses **base→account**. These
  are different legs and differ by ~150× on USD/JPY.
- Each closed bar is processed exactly once, in chronological order.
- Exits are evaluated before entries on the same bar.
- Simulated state never touches live state. Paths come only from
  `constants.state_paths(simulated)`.
- `Store.reset()` refuses to run against live state.
- Macro may shrink a position or refuse one. It may never enlarge one.
- No hedged positions: one net position per symbol.
- No lookahead: `PolicyRateSeries.asof` returns the observation in force at
  that moment or None, and None must propagate to a refusal. An undated rate
  never satisfies a dated query.
- The engine never touches a widget; the GUI never mutates a widget off the Qt
  main thread; everything between them crosses `gui/controller.py` as a signal.
- Only plain data crosses a thread boundary — never a live `Engine` or `Position`.
- Both front ends run `Engine.run()`. The GUI does not own a loop of its own.
- A stop is cooperative and still runs the full shutdown, settling open positions.

---

## 5. Workflow for changes

1. State the plan in 3–8 bullets before editing.
2. Make the smallest change that solves the problem.
3. Add or update a test that **fails before and passes after**. Verify it can
   fail by temporarily reverting the behaviour.
4. Run the full test suite:
   ```bash
   for f in tests/test_*.py; do python "$f" || exit 1; done
   ```
5. Run the engine end-to-end on sample data and confirm the report is coherent —
   trades happen, refusal codes are aggregatable, risk per trade matches config.
6. If the change touches costs, sizing or exits, re-run at `--spread-mult 2.0`
   and note the effect.
7. State rollback steps for anything touching `position.py`, `sizing.py` or
   `exits.py`.

---

## 6. Testing standards

- Standalone scripts, no framework. `tests/_runner.py` provides the runner.
- Float comparisons use `close(a, b, tol)`. **Derive the tolerance**; do not
  widen it to make a failing test pass. `test_sizing.py:assert_risk_at_or_under_budget`
  is the worked example — its bound is one lot step, computed, not guessed.
- Dates in tests are real and fixed. `2026-01-14` is a Wednesday, `2026-01-16` a
  Friday, `2026-01-18` a Sunday. Session and rollover tests depend on it.
- A test that passes against both correct and broken code is worse than no test.

---

## 7. What not to add

Each of these is absent on purpose. Adding one needs an explicit decision, not
a pull request that treats it as an oversight.

| Not present | Why |
|---|---|
| Live order path | Placing real orders is the operator's deliberate decision, with an adapter they wrote and reviewed |
| Parameter optimiser | Searching against one history produces a curve fit that looks like an edge and behaves like noise |
| Hedged positions | Doubles financing for zero net exposure; makes currency netting unreadable |
| Martingale / grid / averaging down | Converts a string of small losses into one account-sized loss |
| A "disable stop" option | See §1 |
| pandas / numpy | The engine is streaming and O(1) per bar; a DataFrame dependency would invite vectorised rewrites that break the exactly-once guarantee |
| pytz / tzdata | NY offsets are derived from US DST rules because a stock Windows Python has no tz database |

---

## 8. Honesty requirements

This project makes claims about a domain where overclaiming costs people money.

- Never describe a simulated result as evidence of profitability. The report
  footer says so; do not remove it.
- Placeholder data — policy rates, swap values, typical spreads — must stay
  labelled as placeholders wherever it appears.
- If a change makes results look better, establish **why** before accepting it.
  The prior should be that it removed a cost or a guard.
- `docs/FX_PRIMER.md` §1 states what retail FX outcomes actually look like.
  Keep it accurate, and do not soften it.

---

## 9. Style

- Python 3.10+. `engine/` is standard library only; `gui/` may use PySide6
  and nothing else.
- Type hints on public functions; dataclasses for settings and state.
- Comments explain **why**, especially where a choice is deliberately
  pessimistic or counter-intuitive. The code says what it does.
- Docstrings on every module stating what it owns and what would break if it
  were wrong.
- `%`-style logging arguments, not f-strings, so disabled levels cost nothing.
- No printing on the engine hot path; the engine logs and the CLI prints.

---

## 10. If a request is ambiguous

Ask before changing anything in `position.py`, `sizing.py`, `exits.py` or
`risk.py`. Elsewhere, make the smallest reasonable interpretation, implement
it, and state the assumption in your summary.

If a request would violate §1 or §4, say so plainly, explain which invariant it
breaks, and propose the nearest thing that does not.
