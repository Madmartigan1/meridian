# Runbook

Operating Meridian, reading what it tells you, and recognising the failure
modes that matter.

---

## Commands

| Task | Command |
|---|---|
| Generate sample data | `python meridian.py --generate-data --bars 3000` |
| Standard run | `python meridian.py --reset-state` |
| Cost stress test | `python meridian.py --reset-state --spread-mult 2.0` |
| Debug a quiet run | `python meridian.py --reset-state -v` |
| Report only | `python meridian.py --reset-state -q` |
| Save a config | `python meridian.py ... --save-config setup.json` |
| Run a saved config | `python meridian.py --config setup.json --reset-state` |
| All tests | `for f in tests/test_*.py; do python "$f" || exit 1; done` |
| Desktop app | `python meridian_gui.py` (needs PySide6) |

`--reset-state` clears **simulated** state only. `Store.reset()` refuses to
touch live state at all.

---

## Reading the log

```
ENTER EUR/USD  LONG  0.24 lots @ 1.08637  stop 1.08451 (18.6 pips, risk 44.65)  [ema up by 12.4 pips, held 3 bars]
EXIT  EUR/USD  LONG  0.24 lots  -18.6 pips (swap -0.20)  -45.13 USD  [stop hit]
SKIP  GBP/USD  SHORT  currency_exposure: USD would reach 52000 against a 50000 cap
```

Every line carries the reason. `ENTER` states the money at risk explicitly —
if that number is not close to `risk_per_trade_pct × equity`, something in the
sizing chain is wrong and worth chasing before anything else.

### Refusal codes

Codes are stable single tokens before the colon, so the report can aggregate
them.

| Code | Meaning | Usual fix |
|---|---|---|
| `macd_not_confirming` | MACD histogram disagrees with the EMA direction | Normal, often the largest count. `--no-macd` to measure its contribution |
| `rsi_overbought` / `rsi_oversold` | Entry into an exhausted move | Widen `rsi_long_max` / `rsi_short_min` |
| `atr_below_floor` | Range too small to clear costs | Correct behaviour; lower `min_atr_pips` only with a reason |
| `stop_inside_costs` | ATR stop narrower than the round trip | Raise `atr_stop_mult` or trade a longer bar |
| `illiquid_session` | Outside London / New York | `--any-session`, but raise your spread assumption with it |
| `currency_exposure` | Would concentrate one currency | Working as intended. Raise the cap only knowingly |
| `total_risk` | Open stops plus this one exceed the cap | Lower `risk_per_trade_pct` or hold fewer positions |
| `margin_cap` | Beyond `max_margin_utilisation` | |
| `max_positions` | Already at the limit | |
| `daily_halt` | Daily loss limit hit | Resets on the New York day |
| `sizing` | Below the minimum ticket, or no conversion rate | See below |
| `no_conversion` | Cannot price the pair into the account currency | Add a pair that bridges the currencies |

---

## Failure modes

### Zero trades, thousands of signals

Read the `refusals` line — it names the gate. The common causes, in order:

1. `currency_exposure` dominating → the cap is set as though it were a leverage
   limit. One leveraged position is normally 300–400% of equity in notional on
   its own; the default is 500%.
2. `sizing` dominating → equity too small for the minimum ticket at your stop
   distance. At 0.5% of $1,000 with a 30-pip stop, the correct size is below
   0.01 lots, and the engine refuses rather than rounding up.
3. `illiquid_session` dominating → your data timestamps may be in local time,
   not UTC. Check that a bar you know was during London reports as liquid.

### Every exit is "stop hit"

The stop is inside the noise. Raise `atr_stop_mult`, or move to a longer bar
interval. Note this reduces position size proportionally — that is the model
working, not a problem to correct.

### Realised risk does not match the configured percentage

Check in this order:

1. Is the pair's `pip_size` right for its quote convention? JPY pairs use 0.01.
2. Can `RateBook` reach your account currency from the pair's quote currency?
3. Is the position sized off the **fill** or the signal price? Levels anchored
   to the close understate the stop by the spread and slippage paid.

`tests/test_sizing.py` covers all three directly.

### Results look excellent

Before believing it:

- Re-run at `--spread-mult 2.0`. Does it survive?
- Check `exits by reason`. If take profits dominate and stops are rare on a
  mean-reverting series, you may be measuring a strategy that wins often and
  loses enormously — check `max drawdown` against `net PnL`.
- Was the data synthetic? A random walk has no edge to find. A *positive*
  result on synthetic data is more suspicious than a negative one.
- How many parameters did you try before this one? Every attempt spends some of
  the significance of the result.

### A JPY pair's numbers look 100× off

The pip convention. `pip_size` is 0.01 for JPY-quoted pairs and 0.0001
elsewhere, and pip *value* additionally needs the quote→account conversion.
`tests/test_instruments.py` pins both.

---

## State

| Path | Contents |
|---|---|
| `.state/sim_data/` | Simulated positions, trades, account, daily risk |
| `.state/` | Live equivalents — never written while simulated |

Override the root with `MERIDIAN_STATE_DIR`.

Writes are atomic (temp file plus `os.replace`), so an interrupted run leaves
the previous state readable. If a state file is corrupted anyway, the engine
warns and starts flat rather than refusing to run — check the trade ledger
before continuing, since a stale `positions.json` and a fresh run disagree
about what is open.

`trades.csv` / `sim_trades.csv` carry one row per closed slice, with entry and
exit reason, pips, financing, money and equity after. This is the record to
analyse; the report is a summary of it.

---

## Recovery

| Situation | Action |
|---|---|
| Run interrupted mid-bar | Restart. State was written after the last completed bar |
| Positions file looks wrong | Inspect `sim_positions.json`, then `--reset-state` |
| Results changed with no config change | Check whether the data files changed; `--save-config` pins everything else |
| Want a clean baseline | `--reset-state --generate-data` then run |

---

## Before trusting any result

1. The full test suite passes.
2. The run has a non-trivial number of closed trades. Twenty trades is an
   anecdote.
3. The result survives `--spread-mult 2.0`.
4. The data is real, and its timestamps are UTC.
5. `--bar-minutes` matches the data.
6. Broker-specific figures — `typical_spread_pips`, swap values, policy rates —
   have been replaced with real ones. The shipped values are placeholders.
7. You have read `docs/FX_PRIMER.md` §1 recently enough to remember what a
   simulated result does and does not establish.

---

## Live trading

There is none. `--live` prints what is missing and exits non-zero.

Adding it means writing a broker adapter, an order path, a fill reconciler and
a reconnect-safe position sync. None of that can be inferred from the simulated
path, and all of it deserves the same scrutiny as the risk model. The stub
interface and its exactly-once contract are documented in `engine/feed.py` and
`docs/ARCHITECTURE.md`.
