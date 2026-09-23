# Knob Reference

Every configurable parameter, its default, its safe range, and what it actually
does. Defaults live in `engine/config.py` and the component dataclasses;
`validate()` coerces out-of-range values and warns rather than crashing.

CLI flags override for one invocation only. There is no third precedence layer.

> Percentages differ between the CLI and the config file. The CLI takes human
> percentages (`--risk-pct 0.5` = 0.5%); the config stores fractions
> (`risk_per_trade_pct: 0.005`). The conversion happens once, in
> `main.config_from_args`.

---

## Hard ceilings (`engine/config.py`)

These are not knobs. Config may be more conservative, never less.

| Constant | Value | Why |
|---|---|---|
| `MAX_RISK_PER_TRADE_PCT` | 0.02 | Above 2% per trade, a normal losing streak is an account-ending event rather than a drawdown |
| `MAX_LEVERAGE` | 50 | Beyond the retail caps in most regulated jurisdictions |
| `MAX_TOTAL_RISK_PCT` | 0.06 | All open stops hitting at once must be survivable |
| `MIN_STOP_PIPS_FLOOR` | 1.0 | A stop inside the spread is not a stop, it is a fee |

There is **no** `enable_stop` flag anywhere in the codebase. This is deliberate.

---

## Account

| Knob | CLI | Default | Range | Notes |
|---|---|---|---|---|
| `account_currency` | `--currency` | `USD` | any ISO code | Must be reachable from the pairs traded, or sizing refuses |
| `starting_equity` | `--equity` | 10000 | > 0 | |
| `risk.leverage` | `--leverage` | 30 | 1–50 | 30:1 is the ESMA retail cap on majors. Higher does not improve expectancy, only ruin probability |

---

## Universe and data

| Knob | CLI | Default | Notes |
|---|---|---|---|
| `symbols` | `--symbols` | `EUR/USD,USD/JPY,GBP/USD` | Must exist in `DEFAULT_INSTRUMENTS`; unknown pairs are dropped with a warning |
| `bar_minutes` | `--bar-minutes` | 60 | 1–1440. Must match your data. Nothing verifies this for you — mismatched, every time-based rule is wrong by the ratio |
| `data_dir` | `--data-dir` | `data` | Expects `<SYMBOL>.csv` with the slash removed |

---

## Strategy

| Knob | CLI | Default | Notes |
|---|---|---|---|
| `strategy` | `--strategy` | `trend` | `trend` or `carry`. Choosing `carry` also applies the carry exit preset below |
| `policy_rates_file` | `--policy-rates` | — | **Required by carry.** Dated rate history; see `policy_rates.example.json` |
| `carry.reentry_cooldown_hours` | `--reentry-cooldown-hours` | 72 | Hours after an exit before the same pair may re-open. **Not optional**: a carry signal is persistent, so without it a stop-out re-opens on the next bar and the stop becomes a fee paid repeatedly |
| `carry.require_atr_floor` | — | on | The only advisor gate carry honours, because stop distance comes from ATR |

### The carry exit preset

`--strategy carry` applies `apply_carry_preset()`, which turns **off** the exits
that would destroy a carry position, and widens the stop:

| Setting | Carry value | Why the trend default breaks carry |
|---|---|---|
| `enable_max_hold` | off | 72 hours closes it long before the differential earns anything |
| `enable_stagnation` | off | Closes precisely the positions that are working — a carry position going nowhere on price **is** the thesis |
| `close_before_weekend` | off | Forfeits the roll, and Wednesday's roll pays triple |
| `enable_trailing` | off | Ratchets a slow drifter out on the first normal retracement |
| `enable_take_profit` | off | Carry has no price target |
| `atr_stop_mult` | 6.0 | Room to breathe; the stop is for the unwind, not the noise |
| `enable_rsi` / `enable_macd` | off | Trend confirmations would turn carry back into a trend strategy in disguise |

**Running carry with trend exits is a misconfiguration that looks exactly like a
strategy failure.** Explicit flags still override the preset, so you can undo any
of it deliberately — but know which you are doing.

The stop itself stays mandatory. Carry's real risk is the violent unwind, which
is the precise thing a stop exists for.

## Signal (`SignalSettings`)

| Knob | CLI | Default | Range | Notes |
|---|---|---|---|---|
| `fast_ema` / `slow_ema` | `--fast-ema` / `--slow-ema` | 20 / 60 | fast < slow | On H1 bars, roughly a day against three |
| `confirm_bars` | `--confirm-bars` | 2 | 1–20 | Bars the separation must persist. Raising it cuts whipsaw and entry count together |
| `deadband_pips` | `--deadband-pips` | 1.0 | ≥ 0 | Minimum EMA separation before a cross counts. **Set this at or above your typical spread** — below it, the engine fires on noise it cannot profit from |

---

## Advisor gates (`AdvisorSettings`)

Gates may only veto. None of them can generate an entry.

| Knob | CLI | Default | Notes |
|---|---|---|---|
| `enable_rsi` | `--no-rsi` | on | |
| `rsi_long_max` | — | 72 | Block longs above this. Raise toward 80 to stop vetoing strong trends |
| `rsi_short_min` | — | 28 | Mirror image for shorts |
| `enable_macd` | `--no-macd` | on | Usually the most active gate — check the run report's refusal counts |
| `macd_normalize` | — | on | Expresses the histogram in pips, so one threshold is valid across a 1.08 pair and a 150 pair |
| `enable_atr_floor` | — | on | |
| `min_atr_pips` | — | 4.0 | Refuse to trade a range smaller than costs |

---

## Exits (`ExitSettings`)

| Knob | CLI | Default | Range | Notes |
|---|---|---|---|---|
| `atr_stop_mult` | `--atr-stop-mult` | 1.8 | 0.1–10 | Stop distance = ATR × this. **This knob sets position size**, because size follows from stop distance |
| `min_stop_pips` | — | 8.0 | ≥ 1 | |
| `max_stop_pips` | — | 80.0 | ≥ min | Caps how small one trade can be sized |
| `reward_risk` | `--reward-risk` | 2.0 | 0.1–20 | Take profit at this multiple of the stop |
| `enable_take_profit` | — | on | |
| `enable_trailing` | `--no-trailing` | on | |
| `trail_start_r` | — | 1.0 | Start trailing once up this many R |
| `trail_distance_r` | — | 0.8 | Trail this far behind the peak. Tight trails convert winners into scratches |
| `enable_stagnation` | — | on | |
| `stagnation_bars` | — | 48 | |
| `stagnation_pips` | — | 3.0 | Progress threshold below which the position is called dead |
| `enable_max_hold` | — | on | |
| `max_hold_hours` | `--max-hold-hours` | 72 | 1–720 | **Market hours, not wall clock.** A weekend does not age a position |
| `close_before_weekend` | `--hold-over-weekend` | on | Flat into the Friday close. Disabling accepts gap risk a stop cannot protect against |
| `weekend_flat_hours` | — | 2.0 | How early to start standing down |

---

## Execution costs (`ExecutionSettings`)

The numbers that decide whether a strategy that looks profitable on mid prices
actually is.

| Knob | CLI | Default | Notes |
|---|---|---|---|
| `use_feed_spread` | — | on | Prefer bid/ask from the data when present |
| `spread_multiplier` | `--spread-mult` | 1.0 | **Run at 2.0 as a stress test.** If the result inverts, the strategy was living inside its cost assumption |
| `slippage_pips` | `--slippage-pips` | 0.2 | Always adverse. Modelling it as symmetric noise hands back half the cost |
| `stop_slippage_pips` | `--stop-slippage-pips` | 1.0 | Larger because stops fill in the worst liquidity. `validate()` refuses to let this sit below normal slippage |
| `commission_per_lot` | `--commission-per-lot` | 0.0 | Per side. Zero on spread-only accounts |
| `max_spread_pips` | — | 3.5 | Refuse to open when the spread is blown out. This is what protects you around releases |

Per-instrument `typical_spread_pips` and swap values live in
`DEFAULT_INSTRUMENTS`. **They are placeholders.** Replace them with your
broker's published figures before drawing any conclusion.

---

## Risk (`RiskSettings`)

| Knob | CLI | Default | Range | Notes |
|---|---|---|---|---|
| `risk_per_trade_pct` | `--risk-pct` | 0.005 (0.5%) | ≤ 0.02 | The amount lost when a stop is hit. Size is derived from it |
| `max_open_positions` | `--max-positions` | 4 | 1–20 | |
| `max_total_risk_pct` | — | 0.02 | ≤ 0.06 | All open stops hitting together |
| `max_currency_exposure_pct` | `--max-currency-exposure-pct` | 5.0 (500%) | 0.1–50 | **Concentration limit, not a leverage limit.** One leveraged position is normally 300–400% of equity in notional on its own, so values near 1.0 refuse every trade rather than only concentrated ones |
| `max_daily_loss_pct` | `--max-daily-loss-pct` | 0.03 | ≤ 0.20 | Halts new entries for the New York day. Open positions still manage their exits |
| `max_margin_utilisation` | — | 0.30 | 0–1 | At 30:1 this caps total notional near 9× equity |
| `require_liquid_session` | `--any-session` | on | | Entries only during London or New York. Outside them the spread defaults understate real cost |

---

## Macro (`MacroSettings`)

| Knob | CLI | Default | Notes |
|---|---|---|---|
| `enabled` | `--no-macro` | on | |
| `use_carry_bias` | — | on | |
| `carry_min_differential` | — | 1.0 | Percentage points before a differential counts |
| `carry_max_differential` | — | 5.0 | Normalisation cap, so one outlier cannot dominate |
| `carry_veto_opposing` | `--carry-veto` | off | Refuse entries against a strong differential. Off by default: carry crashes, and a technical signal against it is often the early warning |
| `use_event_gate` | — | on | |
| `block_before_minutes` | — | 30 | |
| `block_after_minutes` | — | 15 | |
| `block_impact` | — | `("HIGH",)` | |
| `surprise_decay_hours` | — | 12 | How long a data surprise keeps tilting |
| `surprise_weight` | — | 0.5 | Bounded contribution |

The shipped policy rates in `default_macro_view()` are **placeholders**. A stale
rate table produces a carry bias confidently pointed the wrong way.

### Macro sizing

| Knob | Default | Notes |
|---|---|---|
| `macro_size_scaling` | on | Let macro disagreement shrink the trade |
| `macro_size_floor` | 0.5 | Never below half the risk-based size |
| `macro_size_ceiling` | 1.0 | **Clamped at 1.0.** Macro can never add risk — it is a prior, not an edge |

---

## Tuning without fooling yourself

1. **Change one thing at a time**, and read the refusal counts in the report,
   not just the PnL. A change that improved PnL by removing a gate has usually
   just increased variance.
2. **Always re-run at `--spread-mult 2.0`.** A result that survives doubled
   costs is telling you something; one that inverts was never there.
3. **Watch the round-trip cost line printed at startup.** If your average
   winning excursion is not clearly above it, no parameter fixes that.
4. **Synthetic data proves nothing about a strategy.** It is a random walk with
   no fat tails and no macro response. It proves the engine executes.
5. **Resist the parameter search.** There is no optimiser here on purpose.
