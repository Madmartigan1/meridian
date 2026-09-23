# FX Primer — and an honest answer to "is there money in this?"

This document does two things: it answers the question that prompted the
project, and it explains the market mechanics the engine encodes. Read it
before the configuration reference — several knobs only make sense once the
mechanics are clear.

---

## 1. The honest answer

**Is there money to be made in FX? Yes, and it is very unlikely to be you
making it on short-horizon retail speculation.**

Both halves of that sentence matter, so take them separately.

### The market is real and large

FX turnover is on the order of several trillion US dollars a day — the BIS
Triennial Survey put global turnover near $7.5 trillion/day in April 2022, and
the 2025 survey moved it higher again. Prices move continuously, the market is
deep, and real returns are available in it. Banks, market makers, macro funds
and corporate treasuries all extract value from currency markets.

They extract it from *different activities*, though, and that is the part that
gets lost:

| Who | What they actually earn |
|---|---|
| Dealers / market makers | The spread, thousands of times a day, with near-zero holding period |
| Corporates | Hedging — reducing variance, not seeking return |
| Macro funds | Multi-week to multi-quarter positions on rate and policy divergence |
| Carry investors | The interest differential, in size, with crash risk |
| Retail short-term speculators | — |

### The documented record for retail

Brokers in the EU and UK are required to publish the share of retail CFD
accounts that lose money. The published figures have sat consistently in the
**70–80%** range across firms and years, and that is the figure *before*
survivorship — it counts active accounts, not the ones that already closed.

The drag is structural, not a matter of skill or discipline:

- **Spread** is paid on entry and again on exit. At 1 pip round-trip on a
  20-pip stop, you start each trade about 5% behind.
- **Financing (swap)** is charged at every rollover, tripled on Wednesdays.
  Holding costs compound quietly.
- **Slippage** is worst exactly when you need it least — at releases, and on
  stops, which by definition fill in the worst available liquidity.
- **Leverage** multiplies all of the above along with the position. At 30:1, a
  3% adverse move on full margin is the account.

None of that is a reason the market is rigged. It is arithmetic: FX is roughly
zero-sum before costs and reliably negative-sum after them, for anyone whose
holding period is short enough that costs dominate returns.

### What *is* documented as persistent

Two effects have survived decades of academic and practitioner scrutiny:

- **Carry** — systematically holding higher-yielding currencies against lower
  yielders has earned a positive long-run return. It also crashes: the return
  profile is a long slow drift punctuated by violent unwinds, and the unwind
  takes back months in days. This is not a free lunch, it is compensation for
  bearing that specific risk.
- **Trend / momentum at longer horizons** — weaker, noisier, and it decays as
  it becomes crowded, but it has not vanished.

Both are *slow*. Both work in size, across many pairs, with strict risk
control, over horizons measured in weeks. Neither is an intraday crossover
system, and neither survives being run at 30:1 leverage on three pairs.

### So what is this engine for?

**Learning the mechanics with real arithmetic and no money at risk.**

It is a validation instrument. It answers "does this rule behave the way I
think it does, once spread, slippage, financing, sessions and leverage are
modelled honestly?" That is a genuinely useful question, and most retail
platforms answer it dishonestly by default — fills at mid, no financing, stops
that always fill exactly at the stop.

It does **not** answer "will this make money," and it ships with no live order
path precisely so that the question cannot be quietly skipped.

If you want exposure to currency as an asset class rather than a trading
hobby, the boring answer is that it is available through funds at a fraction of
the cost and none of the leverage. The engine will not tell you that at
runtime, so it is written down here.

---

## 2. Mechanics the engine encodes

### Pairs and quoting

Every FX price is a **relative** price: `EUR/USD = 1.0850` means one euro buys
1.0850 dollars. Buying EUR/USD is simultaneously long EUR and short USD.

The first currency is the **base**, the second the **quote**. Profit and loss
arises in the *quote* currency, which is why a USD-denominated account trading
EUR/GBP needs a GBP→USD conversion to state its PnL at all.

→ `engine/instruments.py`, `RateBook`

### Pips, points and lots

- A **pip** is 0.0001 for most pairs, **0.01 for JPY-quoted pairs**.
- A **point** is a tenth of a pip — the smallest quotable increment.
- A **standard lot** is 100,000 units of base currency; a micro lot (0.01) is
  1,000 units.
- **Pip value** = pip size × contract size, converted from quote currency to
  account currency. One pip on one standard lot of a USD-quoted pair is $10 for
  a USD account; on USD/JPY at 150 it is about $6.67.

These conventions are declared in exactly one place and covered directly by
tests, because a mistake here is invisible at runtime and wrong everywhere.

→ `engine/instruments.py`, `tests/test_instruments.py`

### What moves the price

| Driver | Mechanism | Where it lives |
|---|---|---|
| Interest rate differential | Capital chases yield; you earn or pay the gap while holding | `macro.carry_bias()`, `sessions.SwapBook` |
| Inflation | Erodes purchasing power and shapes the next policy move | `PolicyRate.real_rate` |
| Growth | Attracts investment, shapes rate expectations | `PolicyRate.growth` |
| **Surprise vs. forecast** | The tradable part — the level is already priced | `EconomicEvent.surprise` |
| Risk sentiment | Flight to reserve currencies under stress | not modelled |

The fourth row is the one worth dwelling on. A central bank raising rates as
expected typically moves the price very little, because the expectation was
already in it. The same hike when the market expected a hold moves it a great
deal. Price responds to the **gap between actual and consensus**, which is why
`EconomicEvent` stores both and derives the surprise rather than storing a
direction.

The engine cannot forecast a release. What it can do is refuse to open a fresh
position inside the blackout window around one — which is what the event gate
does, and the honest use of this layer.

→ `engine/macro.py`, `tests/test_macro.py`

### The dollar's position

The US dollar sits on one side of the large majority of FX turnover and remains
the dominant reserve currency. Practically, this means most retail pairs are
dollar pairs, and a portfolio of them is usually one concentrated dollar bet
wearing three different names.

Reserve status is not permanent — it has changed hands historically, over
decades — but that is a multi-generational observation, not a trade. The
engine's concrete response is the **currency exposure gate**: exposure is
aggregated per currency across all open positions, so three dollar-shorts are
counted as one large dollar-short and refused as a concentration.

→ `engine/risk.py:currency_exposure`, `tests/test_risk_exposure.py`

### Sessions, rollover and the weekend

FX runs ~24×5, not 24×7:

- The week opens **Sunday 17:00 New York** and closes **Friday 17:00 New York**.
- Price **gaps** over the weekend. A stop does not protect against a gap — it
  becomes a market order at whatever Sunday opens at.
- **Rollover** is 17:00 New York daily. Financing is credited or debited then,
  and **Wednesday charges triple** because spot settles T+2 and Wednesday's roll
  covers the weekend.
- Liquidity concentrates in the **London** and **London/New York overlap**
  hours. Spreads outside them are wider than the defaults assume.

→ `engine/sessions.py`, `tests/test_sessions.py`

### Leverage

Leverage does not change expectancy. It scales the outcome and it changes the
probability of ruin, which is a different and much less forgiving quantity.

A 2% edge at 2:1 is a slow grind. The same 2% edge at 30:1 has a materially
higher chance of hitting zero before the edge has time to show up, because the
path matters and the account cannot go below zero and recover.

This is why the engine's stop is not optional, sizing runs backwards from the
stop rather than forwards from a notional, and the ESMA retail cap (30:1 on
majors) is the default rather than the maximum available somewhere.

→ `engine/sizing.py`, `engine/exits.py`, `engine/risk.py`

---

## 3. What this engine deliberately does not do

- **No live order path.** `engine/feed.py` ships a `LiveFeed` stub and nothing
  that places an order. `--live` prints why and exits.
- **No profitability claim.** The run report says so in its own footer.
- **No optimiser.** Searching parameters against one history produces a curve
  fit that looks like an edge and behaves like noise.
- **No hedged positions.** Simultaneous long and short on one pair doubles
  financing for zero net exposure.
- **No martingale, grid, or averaging-down sizing.** These convert a string of
  small losses into one account-sized loss, and they look excellent right up
  until they do not.

---

## Sources worth reading yourself

- BIS Triennial Central Bank Survey — the turnover figures, from the primary source
- Your broker's own risk disclosure — the loss percentage they are required to publish
- Any introductory treatment of carry and momentum in currencies — for what
  persistent FX returns actually look like, and at what horizon

Nothing in this repository is financial advice. See `RISK_DISCLOSURE.txt`.
