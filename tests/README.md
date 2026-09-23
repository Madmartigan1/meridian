# Tests

Behavioural tests for the engine. **No test framework is required** — each file
is a plain script that runs standalone and exits non-zero on failure.

## Running

```bash
python tests/test_instruments.py
```

Or all of them:

```bash
for f in tests/test_*.py; do python "$f" || exit 1; done
```

PowerShell:

```powershell
Get-ChildItem tests/test_*.py | ForEach-Object { python $_.FullName; if ($LASTEXITCODE -ne 0) { break } }
```

## Approach

`tests/_runner.py` collects every `test_*` callable from a module's globals,
runs it, and reports pass/fail. A test signals failure by raising
`AssertionError`; an unexpected exception is reported as an error, not a pass.

The engine is built to be testable without a network, a broker, or a clock:
every indicator is streaming, the position book is a plain data structure, and
the session functions take an explicit `datetime`. So these tests construct the
real objects rather than stubbing them — there is nothing expensive to avoid.

`test_gui_controller.py` is the only file that needs PySide6; the rest run on
the standard library alone. It sets `QT_QPA_PLATFORM=offscreen` and points
`MERIDIAN_STATE_DIR` at a temporary directory itself, before importing Qt or
the engine — so it needs no display and never touches operator state.

**Dates in the tests are fixed and real.** `2026-01-14` is a Wednesday,
`2026-01-16` a Friday, `2026-01-18` the following Sunday. Session and rollover
tests depend on those weekdays, so do not casually change them.

## What is covered, and why these things specifically

The engine runs end-to-end on synthetic data, which proves it executes. It
proves almost nothing about correctness: wrong pip arithmetic produces a
plausible-looking position that risks the wrong amount, indefinitely, without
raising anything. So the tests target the numeric primitives everything else is
built from, plus the safety properties that a flattering bug would hide.

| File | Covers |
|---|---|
| `test_instruments.py` | Pip size by quote convention (the JPY 0.01 exception), pip↔price round-tripping, pip value when the quote currency is the account currency, when it is not, on a cross where *neither* leg is, and via an inverted rate. Lot rounding at the 0.01 boundary — rounds down, returns zero below the minimum ticket rather than rounding up into a size the risk budget never authorised — and refusal to produce a pip value at all when no conversion rate exists. Also cross-rate derivation: EUR->GBP routes through the dollar legs with the arithmetically correct result, and a directly quoted pair beats a triangulated one |
| `test_position.py` | Signed netting: short as a first-class opening action, lot-weighted averaging on adds, partial closes that leave the entry price alone, pro-rata financing on a partial close, and the case a long-only model cannot express — an opposing fill larger than the open position settles the old leg and opens the reverse. Also that `close()` can never flip, and that opening without a stop raises |
| `test_sizing.py` | Risk is constant: the same percent of equity is risked across pip conventions, stop distances, and a cross needing a conversion leg. Margin uses the base leg while pip value uses the quote leg — conflating them misstates USD/JPY margin by 150x. Free margin steps size down rather than refusing. A size below the minimum ticket is refused, never rounded up. Realised risk lands at or just under budget, never over, bounded by one lot step |
| `test_sessions.py` | New York DST offsets without the tz database, the Sunday-open/Friday-close week, Wednesday's triple rollover, no rollover on Saturday, and elapsed **market** hours excluding the weekend — the property that stops a 72-hour max-hold firing on a position that was tradable for 24 of them. Plus financing windows measured per position: one opened at 18:00 New York is not billed for that day's 17:00 roll, while one held through it is charged the Wednesday triple |
| `test_exits.py` | The stop rules, because every bug here flatters the result: a bar containing both the stop and the take profit settles as the stop; a trailing stop only ever tightens, in both directions; a gapped open fills at the open rather than at the stop level; max hold counts market hours; the weekend-flat setting is actually respected when disabled. Also that R is fixed at entry, so a 0.8R trail stays a constant distance behind the peak instead of accelerating as the stop tightens |
| `test_risk_exposure.py` | Currency netting across pairs — one position is two currency exposures, opposing pairs net rather than double-count, and three dollar-shorts in different pairs are recognised as one concentration and refused with the currency named, while a same-sized diversifying entry is allowed. Also the daily loss halt, the liquidity-window gate, and that refusal codes stay stable single tokens so the run report can aggregate them |
| `test_gui_controller.py` | The GUI threading contract, whose failures are all invisible at the time they happen: a run completes and emits progress, snapshots contain plain data only (a live `Engine` or `Position` crossing a thread would be read while the worker mutates it), the log bridge is removed after both a successful and a failed run (a leak double-logs the next one), a cooperative stop still settles open positions, config survives a round trip through the widgets, and no control exists anywhere for disabling the stop. Runs offscreen against a temporary state directory, so it needs no display and cannot touch operator state |
| `test_carry.py` | The carry strategy and the lookahead guard underneath it. Rate lookup returns the value **in force at the bar**, a bar predating the history refuses with `no_rate_asof`, and an undated rate can never satisfy a dated query even when it is the newest point held — that last one is what makes the shipped placeholder table safe rather than merely wrong. Then the behaviours that stop carry degenerating into trend: direction follows the differential and flips when it does, RSI/MACD cannot gate it (proved by feeding it advisors hostile enough to veto the trend signal), the re-entry cooldown blocks re-opening after a stop and its default is non-zero, and the preset disables the exits that would close a carry position for behaving like one — without ever removing the stop |
| `test_macro.py` | Real (inflation-adjusted) differentials rather than headline yield, surprise as actual-minus-forecast with the sign flipped for inverse indicators like unemployment, surprise signed per leg of the pair, decay of stale prints, and the release blackout — applying to both legs, only to relevant pairs, and only at the configured impact level. Also that the carry veto is opt-in and that macro agreement is reported as a bias rather than acted on directly |

## Adding a test

Name the file `test_*.py`, import `run` and `close` from `_runner`, expose
`test_*` functions, and end with:

```python
if __name__ == "__main__":
    raise SystemExit(run(globals()))
```

**Verify the test can fail.** Temporarily break the behaviour it covers and
confirm it reports a failure. A test that passes against both the correct and
the broken code is worse than no test, because it implies coverage that is not
there.

Float comparisons use `close(a, b, tol)` rather than `==`. Pick the tolerance
from the quantity being compared — a price needs `1e-9`, an accumulated money
figure needs more — and never widen a tolerance to make a failing test pass
without first establishing that the difference is rounding rather than a bug.
`test_sizing.py:assert_risk_at_or_under_budget` is the worked example: the
bound is one lot step, derived, not guessed.
