# Operational Specification

What the engine does, in operator terms, stated precisely enough to be checked
against. Where this document and the code disagree, the code is authoritative
and this document is a bug.

---

## Philosophy

Meridian is a **validation instrument**, not a profit engine. Its job is to let
you see how a set of rules behaves once spread, slippage, financing, sessions
and leverage are modelled honestly. Every decision it takes is accompanied by
the reason it took it, and every refusal by the reason it refused.

Three properties are structural, not configurable:

1. Every position carries a stop, placed at entry, never removed, only ever
   tightened.
2. Simulated state and live state never share a file.
3. Each closed bar is processed exactly once.

---

## 1. Lifecycle

### 1.1 Startup

1. Resolve config: defaults → saved config (if `--config`) → CLI flags.
2. `validate()` coerces out-of-range values to a safe envelope and warns. Hard
   ceilings on risk, leverage and stop floor cannot be raised by configuration.
3. Build the instrument table from the configured universe, dropping unknown
   pairs with a warning.
4. Load persisted state for the run mode: positions, account balance, daily
   risk counters. A corrupt state file warns and starts flat rather than
   aborting.
5. Print the **round-trip cost** per pair before any decision is taken.

### 1.2 Warm-up

Indicators are streaming and report their own readiness. Until
`IndicatorSet.ready` is true, the signal evaluates to `FLAT` with the reason
`warmup: N bars left`. Warm-up is the longest of the EMA, RSI, MACD and ATR
requirements, plus one bar for the previous EMA separation.

No entry is possible during warm-up. Exits on an existing position are
evaluated normally — a position loaded from state is managed from the first
bar, including its stop.

### 1.3 Steady state

Per closed bar, in this exact order:

| Step | Action | Notes |
|---|---|---|
| 1 | Update the rate book with the close | Feeds every conversion downstream |
| 2 | Roll the trading day; apply financing for any rollover crossed | Day keyed to New York |
| 3 | Update indicators for this symbol | Exactly one bar, exactly once |
| 4 | Age the open position | `bars_held`, `last_price` |
| 5 | If the market is shut, mark equity and stop | Bar still counted |
| 6 | Manage the open position | Exits — stop checked first |
| 7 | Consider an entry | Only if flat, only if every gate agrees |
| 8 | Mark equity, update peak and drawdown | |

Step 6 precedes step 7 so that a bar which both stops a position out and
triggers a fresh signal settles the loss first, and the next trade is sized
against the equity that remains.

### 1.4 Shutdown

At the end of a replay, all open positions are closed at the last known price,
paying the exit spread. An open position at the end of a run is unrealised PnL
that has not paid its exit cost; leaving it open flatters the report.

State is then persisted: positions, daily risk counters, account.

---

## 2. Entry decision

An entry requires **all** of the following. Any single failure refuses the
trade, with a reason.

1. **Flat in this symbol.** No pyramiding. No hedging.
2. **Indicators ready.**
3. **Trigger fires.** EMA separation beyond `deadband_pips`, held for
   `confirm_bars` consecutive closed bars.
4. **Advisor gates pass.** RSI not exhausted in the signal direction; MACD
   histogram confirming; ATR above the floor. Gates may only veto.
5. **Macro allows.** Not inside a high-impact release blackout for either leg
   of the pair; not vetoed by carry if `carry_veto_opposing` is enabled.
6. **Stop clears costs.** ATR-derived stop distance must exceed the round-trip
   cost. A stop inside the spread is a fee, not a stop.
7. **Size is tradable.** Risk-based sizing yields at least the minimum ticket,
   with a conversion rate available and margin priceable.
8. **Risk gate passes.** Not halted for the day; within position count, total
   open risk, per-currency exposure and margin utilisation; inside the liquidity
   window if required.
9. **Fill obtained.** Spread within `max_spread_pips`, size non-zero.

Only then is the position opened, with stop and take profit placed **from the
fill price**, not the signal price.

### Sizing

```
risk_amount = equity × risk_per_trade_pct
lots        = risk_amount / (stop_distance_pips × pip_value_per_lot)
```

`pip_value_per_lot` is pip size × contract size, converted from the **quote**
currency to the account currency. Margin uses a different leg — notional is in
the **base** currency. Lots are rounded **down** to the lot step, so realised
risk lands at or just under budget, never over. A size below the minimum ticket
is refused, never rounded up.

Macro disagreement may scale the size down, bounded by `macro_size_floor`. It
can never scale it up: `macro_size_ceiling` is clamped to 1.0 at validation.

---

## 3. Exit decision

Evaluated every bar, in severity order. The first match wins.

| Order | Exit | Trigger | Fill |
|---|---|---|---|
| 1 | **Stop** | Long: bar low ≤ stop. Short: bar high ≥ stop | `min(stop, bar open)` for a long, minus stop slippage — a gapped open fills at the open |
| 2 | Take profit | Long: high ≥ TP. Short: low ≤ TP | At the level |
| 3 | Weekend flat | Within `weekend_flat_hours` of the Friday close | At market |
| 4 | Max hold | `market_hours_between(opened, now) ≥ max_hold_hours` | At market |
| 5 | Stagnation | `bars_held ≥ stagnation_bars` and excursion within `stagnation_pips` | At market |
| 6 | Reverse signal | A confirmed, ungated signal the other way | At market |

**When a bar contains both the stop and the take profit, the stop wins.** Bar
data cannot say which came first, and assuming the profit is the assumption
that flatters every result.

A reverse signal **closes only**. The entry path then decides independently
whether to open the other way, clearing every gate a fresh entry does. A flip
is never implicit.

### Trailing

Once the excursion reaches `trail_start_r × R`, the stop tightens to
`peak − trail_distance_r × R`. It only ever moves toward the position. A stop
that can widen is not a stop.

---

## 4. Financing

Rollover is 17:00 New York. Crossing one charges
`swap_*_pips_per_day × weight` to the open position, where weight is 1 on most
days and **3 on Wednesday** (spot settles T+2, so Wednesday's roll covers the
weekend). Saturday has no rollover.

Financing accrues on the position in pips and is settled with it. A partial
close takes its pro-rata share.

---

## 5. Currency exposure

Exposure is aggregated **per currency**, not per pair. A long EUR/USD position
is long EUR and short USD by the same account-currency notional. Three separate
dollar-short positions are therefore recognised as one concentration and
refused, while a same-sized entry in the opposite dollar direction is allowed.

The cap is a concentration limit, not a leverage limit. At 30:1 with a tight
stop, a single risk-based position is normally 300–400% of equity in notional,
so the default is 500% and the margin utilisation cap bounds the total.

---

## 6. Daily halt

Realised PnL is accumulated per **New York** trading day. When it reaches
`−max_daily_loss_pct × equity`, new entries are refused with `daily_halt` for
the remainder of that day. Open positions continue to manage their own exits —
the halt stops new risk, it does not abandon existing risk.

---

## 7. What the engine will not do

- Open a position without a stop — the position book raises.
- Widen a stop, ever.
- Round a size up to reach the minimum ticket.
- Guess a conversion rate when none is derivable; it refuses to size instead.
- Hold simultaneous long and short positions in one pair.
- Add to a losing position, average down, or martingale.
- Place a real order. There is no broker path.
