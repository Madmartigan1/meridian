# engine/engine.py
"""The decision loop.

One bar at a time, in a fixed order that does not vary between replay and live:

    1. mark the rate book and roll financing
    2. update indicators for the symbol that just closed a bar
    3. manage the open position -- stop first, always
    4. consider an entry only if flat and every gate agrees
    5. mark equity

Order matters. Exits are evaluated before entries so that a bar which both
stops a position out and triggers a fresh signal settles the loss first, and
the new trade is sized against the equity that actually remains. Reversing
those two steps silently sizes the next trade off money already lost.

Each closed bar is processed exactly once. A repeated bar double-counts a
crossover; a bar processed before close produces signals that unwind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .config import EngineConfig
from .exits import ExitSettings, evaluate as evaluate_exit, initial_levels, stop_distance_pips, tighten_trailing_stop
from .execution import ExecutionSettings, Fill, Quote, execute, round_trip_cost_pips, stop_fill_price
from .feed import Bar, Feed
from .indicators import IndicatorSet
from .instruments import Instrument, RateBook, price_to_pips
from .macro import MacroView, default_macro_view
from .position import LONG, SHORT, ClosedLeg, PositionBook, opposite
from .persistence import Store
from .risk import RiskManager, RiskSettings, used_margin
from .sessions import SwapBook, is_market_open
from .sizing import money_from_pips, size_for_risk
from .strategy import FLAT, SignalContext, build_signal

log = logging.getLogger("meridian")


@dataclass
class RunStats:
    """Everything the run report is built from."""
    bars: int = 0
    signals: int = 0
    vetoed: int = 0
    blocked: int = 0
    entries: int = 0
    exits: int = 0
    wins: int = 0
    losses: int = 0
    gross_pips: float = 0.0
    swap_pips: float = 0.0
    commission: float = 0.0
    realised: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    veto_counts: Dict[str, int] = field(default_factory=dict)
    exit_counts: Dict[str, int] = field(default_factory=dict)

    def note_veto(self, reason: str) -> None:
        key = reason.split(":")[0].strip()
        self.veto_counts[key] = self.veto_counts.get(key, 0) + 1

    def note_exit(self, reason: str) -> None:
        key = reason.split(":")[0].strip()
        self.exit_counts[key] = self.exit_counts.get(key, 0) + 1


class Engine:
    """Owns the position book, the account, and the per-bar decision."""

    def __init__(
        self,
        cfg: EngineConfig,
        feed: Feed,
        macro: Optional[MacroView] = None,
        store: Optional[Store] = None,
    ):
        self.cfg = cfg
        self.feed = feed
        self.instruments: Dict[str, Instrument] = cfg.instruments()
        self.rates = RateBook(cfg.account_currency)
        self.macro = macro or default_macro_view()
        self.macro.settings = cfg.macro
        self.store = store if store is not None else Store(simulated=cfg.simulated)

        self.book = PositionBook()
        self.risk = RiskManager(cfg.risk, self.store.load_risk_state())
        self.swaps = SwapBook()
        self.signals = {
            sym: build_signal(cfg.strategy, cfg.signal, cfg.carry)
            for sym in self.instruments
        }
        # When each symbol last closed a position, so a persistent signal (carry
        # especially) cannot re-open the identical trade on the bar after a stop.
        self.last_exit: Dict[str, datetime] = {}
        self.indicators: Dict[str, IndicatorSet] = {
            sym: IndicatorSet(
                fast_ema=cfg.fast_ema, slow_ema=cfg.slow_ema, rsi_period=cfg.rsi_period,
                macd_fast=cfg.macd_fast, macd_slow=cfg.macd_slow,
                macd_signal=cfg.macd_signal, atr_period=cfg.atr_period,
            )
            for sym in self.instruments
        }

        account = self.store.load_account()
        self.balance = float(account.get("balance", cfg.starting_equity))
        self.equity = self.balance
        self.stats = RunStats(peak_equity=self.balance)
        self._stop_requested = False
        self.store.load_positions(self.book)

    # --- accounting ---------------------------------------------------------

    def unrealised(self) -> float:
        total = 0.0
        for pos in self.book.all():
            inst = self.instruments.get(pos.symbol)
            if inst is None:
                continue
            price = pos.last_price or pos.entry_price
            pips = pos.unrealized_pips(inst, price)
            money = money_from_pips(inst, pips, pos.lots, self.rates, price=price)
            if money is not None:
                total += money
        return total

    def mark_equity(self) -> None:
        self.equity = self.balance + self.unrealised()
        if self.equity > self.stats.peak_equity:
            self.stats.peak_equity = self.equity
        drawdown = self.stats.peak_equity - self.equity
        if drawdown > self.stats.max_drawdown:
            self.stats.max_drawdown = drawdown

    def free_margin(self) -> float:
        in_use = used_margin(self.book, self.instruments, self.rates, self.cfg.risk.leverage)
        cap = self.cfg.risk.max_margin_utilisation * self.equity
        return max(0.0, min(self.equity - in_use, cap - in_use))

    # --- the loop -----------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish after the current bar.

        Cooperative, so a stop never interrupts a bar halfway through and
        leaves the book and the ledger disagreeing. Safe to call from another
        thread: it only sets a flag.
        """
        self._stop_requested = True

    def run(self, observer=None) -> RunStats:
        """Run to the end of the feed, or until `request_stop()` is called.

        `observer` is called as `observer(bar, self)` after each bar is fully
        processed. It exists so a front end can watch progress without owning
        the loop: both the CLI and the GUI run *this* method, so the shutdown
        sequence below cannot drift between them.

        An observer that raises is reported and ignored -- a display problem
        must not abort a run or skip the shutdown that settles open positions.
        """
        self._stop_requested = False
        self._log_cost_baseline()
        self._log_rate_coverage()
        for bar in self.feed.stream():
            if self._stop_requested:
                log.info("Stop requested; finishing after %d bars.", self.stats.bars)
                break
            self.on_bar(bar)
            if observer is not None:
                try:
                    observer(bar, self)
                except Exception as exc:  # noqa: BLE001 - never let a view kill a run
                    log.warning("Observer raised %s: %s", type(exc).__name__, exc)
        self._flatten_at_end()
        self._persist()
        return self.stats

    def on_bar(self, bar: Bar) -> None:
        inst = self.instruments.get(bar.symbol.upper())
        if inst is None:
            return

        self.stats.bars += 1
        now = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=timezone.utc)

        self.rates.update(inst.symbol, bar.close)
        self.risk.on_bar(now)

        applied = self.swaps.apply(self.book, self.instruments, now)
        for sym, pips in applied.items():
            self.stats.swap_pips += pips
            log.debug("%s financing %+.2f pips at rollover", sym, pips)

        ind = self.indicators[inst.symbol]
        ind.update(bar.high, bar.low, bar.close)

        pos = self.book.get(inst.symbol)
        if pos is not None:
            pos.bars_held += 1
            pos.last_price = bar.close

        if not is_market_open(now):
            self.mark_equity()
            return

        self._manage_position(inst, bar, now)
        self._consider_entry(inst, bar, now)
        self.mark_equity()

        if self.cfg.log_every_bar:
            log.info("%s %s close=%.5f equity=%.2f open=%d",
                     now.isoformat(), inst.symbol, bar.close, self.equity, len(self.book))

    def _context(self, inst: Instrument, now: datetime) -> SignalContext:
        """Bundle what a signal source may read on this bar."""
        return SignalContext(
            inst=inst,
            indicators=self.indicators[inst.symbol],
            now=now,
            advisors=self.cfg.advisors,
            macro=self.macro,
            last_exit_utc=self.last_exit.get(inst.symbol),
        )

    # --- position management ------------------------------------------------

    def _manage_position(self, inst: Instrument, bar: Bar, now: datetime) -> None:
        pos = self.book.get(inst.symbol)
        if pos is None:
            return

        decision = evaluate_exit(inst, pos, bar.high, bar.low, bar.close, now, self.cfg.exits)
        if decision.should_exit:
            if decision.is_stop:
                price = stop_fill_price(inst, pos.side, pos.stop_price, bar.open, self.cfg.execution)
            elif decision.price is not None:
                price = decision.price
            else:
                fill = execute(
                    inst, opposite(pos.side), pos.lots,
                    Quote(mid=bar.close, bid=bar.bid, ask=bar.ask),
                    self.cfg.execution, reason=decision.reason, enforce_spread_cap=False,
                )
                price = fill.price
            self._close(inst, price, now, decision.reason)
            return

        if tighten_trailing_stop(inst, pos, bar.close, self.cfg.exits):
            log.debug("%s stop tightened to %.5f", inst.symbol, pos.stop_price)

        # A confirmed signal the other way closes the position; the entry path
        # below then decides independently whether to open the reverse. A flip
        # is never implicit -- it must clear every gate a fresh entry does.
        sig = self.signals[inst.symbol].evaluate_gated(self._context(inst, now))
        if sig.actionable and sig.side != pos.side:
            fill = execute(
                inst, opposite(pos.side), pos.lots,
                Quote(mid=bar.close, bid=bar.bid, ask=bar.ask),
                self.cfg.execution, reason="reverse signal", enforce_spread_cap=False,
            )
            self._close(inst, fill.price, now, "reverse signal")

    def _close(self, inst: Instrument, price: float, now: datetime, reason: str) -> None:
        legs = self.book.close(inst, price, when=now, reason=reason)
        for leg in legs:
            self._settle(inst, leg, price)
        if legs:
            self.stats.exits += 1
            self.stats.note_exit(reason)
            self.last_exit[inst.symbol] = now

    def _settle(self, inst: Instrument, leg: ClosedLeg, price: float) -> None:
        net_pips = leg.pips + leg.swap_pips
        pnl = money_from_pips(inst, net_pips, leg.lots, self.rates, price=price)
        if pnl is None:
            log.warning("%s closed but PnL is not priceable in %s; recorded as zero.",
                        inst.symbol, self.rates.account_currency)
            pnl = 0.0
        commission = leg.lots * self.cfg.execution.commission_per_lot * 2.0  # both sides
        pnl -= commission

        self.balance += pnl
        self.stats.realised += pnl
        self.stats.gross_pips += leg.pips
        self.stats.commission += commission
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1

        self.mark_equity()
        self.risk.record_realised(pnl, self.equity, datetime.now(timezone.utc))
        self.store.append_trade(leg, pnl, commission, self.equity)

        log.info("EXIT  %-8s %-5s %.2f lots  %+.1f pips (swap %+.2f)  %+.2f %s  [%s]",
                 leg.symbol, leg.side, leg.lots, leg.pips, leg.swap_pips, pnl,
                 self.rates.account_currency, leg.exit_reason)

    # --- entry --------------------------------------------------------------

    def _consider_entry(self, inst: Instrument, bar: Bar, now: datetime) -> None:
        if inst.symbol in self.book:
            return

        ind = self.indicators[inst.symbol]
        sig = self.signals[inst.symbol].evaluate_gated(self._context(inst, now))
        if sig.side == FLAT:
            return

        self.stats.signals += 1
        if sig.vetoed_by:
            self.stats.vetoed += 1
            self.stats.note_veto(sig.vetoed_by)
            log.debug("%s %s vetoed by %s", inst.symbol, sig.side, sig.vetoed_by)
            return

        allowed, bias, macro_reason = self.macro.evaluate(inst, sig.side, now)
        if not allowed:
            self.stats.blocked += 1
            self.stats.note_veto("macro")
            log.info("SKIP  %-8s %-5s  %s", inst.symbol, sig.side, macro_reason)
            return

        stop_pips = stop_distance_pips(inst, ind.atr.value, self.cfg.exits)

        # A stop inside the round-trip cost is not a stop; it is a fee.
        cost_pips = round_trip_cost_pips(inst, self.cfg.execution)
        if stop_pips <= cost_pips:
            self.stats.blocked += 1
            self.stats.note_veto("stop_inside_costs")
            log.debug("%s stop %.1f pips inside %.1f pips of costs", inst.symbol, stop_pips, cost_pips)
            return

        sizing = size_for_risk(
            inst=inst,
            equity=self.equity,
            risk_pct=self.cfg.risk.risk_per_trade_pct,
            stop_distance_pips=stop_pips,
            rates=self.rates,
            price=bar.close,
            leverage=self.cfg.risk.leverage,
            free_margin=self.free_margin(),
        )
        if not sizing.tradable:
            self.stats.blocked += 1
            self.stats.note_veto("sizing")
            log.debug("%s not sized: %s", inst.symbol, sizing.reason)
            return

        lots = sizing.lots
        if self.cfg.macro_size_scaling and bias < 0:
            # Macro disagreement may only shrink the trade. It can never add
            # size, because a macro view is a prior, not an edge.
            scale = max(self.cfg.macro_size_floor,
                        min(self.cfg.macro_size_ceiling, 1.0 + bias * 0.5))
            scaled = size_for_risk(
                inst=inst, equity=self.equity,
                risk_pct=self.cfg.risk.risk_per_trade_pct * scale,
                stop_distance_pips=stop_pips, rates=self.rates, price=bar.close,
                leverage=self.cfg.risk.leverage, free_margin=self.free_margin(),
            )
            if scaled.tradable:
                lots = scaled.lots

        ok, why = self.risk.allows_entry(
            inst, sig.side, lots, bar.close, self.equity,
            self.book, self.instruments, self.rates, now,
        )
        if not ok:
            self.stats.blocked += 1
            self.stats.note_veto(why)
            log.info("SKIP  %-8s %-5s  %s", inst.symbol, sig.side, why)
            return

        fill = execute(
            inst, sig.side, lots,
            Quote(mid=bar.close, bid=bar.bid, ask=bar.ask),
            self.cfg.execution, reason=sig.reason,
        )
        if not fill.ok:
            self.stats.blocked += 1
            self.stats.note_veto(fill.rejected)
            log.info("SKIP  %-8s %-5s  %s", inst.symbol, sig.side, fill.rejected)
            return

        # Levels are placed off the *fill*, not the close. Anchoring them to
        # the signal price understates the stop distance by the spread and
        # slippage actually paid.
        stop_price, tp_price = initial_levels(inst, sig.side, fill.price, stop_pips, self.cfg.exits)

        self.book.apply_fill(
            inst, sig.side, fill.lots, fill.price, when=now,
            stop_price=stop_price, take_profit_price=tp_price,
            reason="%s | %s" % (sig.reason, macro_reason),
        )
        self.stats.entries += 1
        log.info("ENTER %-8s %-5s %.2f lots @ %.5f  stop %.5f (%.1f pips, risk %.2f)  [%s]",
                 inst.symbol, sig.side, fill.lots, fill.price, stop_price,
                 stop_pips, sizing.risk_amount, sig.reason)

    # --- shutdown -----------------------------------------------------------

    def _flatten_at_end(self) -> None:
        """Close everything at the last known price so the report is complete.

        An open position at the end of a replay is unrealised PnL that has not
        paid its exit spread. Leaving it open flatters the result.
        """
        for pos in list(self.book.all()):
            inst = self.instruments.get(pos.symbol)
            if inst is None:
                continue
            price = pos.last_price or pos.entry_price
            when = datetime.now(timezone.utc)
            fill = execute(
                inst, opposite(pos.side), pos.lots, Quote(mid=price),
                self.cfg.execution, reason="end of run", enforce_spread_cap=False,
            )
            self._close(inst, fill.price, when, "end of run")

    def _persist(self) -> None:
        self.store.save_positions(self.book)
        self.store.save_risk_state(self.risk.state)
        self.store.save_account({
            "balance": self.balance,
            "equity": self.equity,
            "account_currency": self.cfg.account_currency,
            "simulated": self.cfg.simulated,
            "updated": datetime.now(timezone.utc).isoformat(),
        })

    def _log_rate_coverage(self) -> None:
        """State what the carry layer can actually see before it trades.

        An undated rate table silently refuses every carry decision. Printing
        coverage here means the operator learns that from a startup line rather
        than from an empty trade list at the end of a long run.
        """
        if self.cfg.strategy != "carry":
            return
        coverage = self.macro.coverage(self.instruments.keys())
        undated = [ccy for ccy, first in coverage.items() if first is None]
        for ccy, first in sorted(coverage.items()):
            log.info("%-5s policy rates: %s", ccy,
                     "from %s" % first.date() if first else "NONE DATED -- will refuse")
        if undated:
            log.warning(
                "Carry cannot trade any pair touching %s: no dated rate history. "
                "Load one with --policy-rates (see policy_rates.example.json). "
                "Undated rates are refused on purpose -- applying today's rate to "
                "an older bar is lookahead bias, not a strategy.",
                ", ".join(sorted(undated)),
            )

    def _log_cost_baseline(self) -> None:
        """State the hurdle before the first trade.

        If a strategy's average winning excursion does not clearly exceed this,
        no amount of parameter tuning makes it profitable -- the costs are the
        result.
        """
        for sym, inst in self.instruments.items():
            log.info("%-8s round-trip cost %.1f pips (spread %.1f x2 + slippage %.1f x2)",
                     sym, round_trip_cost_pips(inst, self.cfg.execution),
                     inst.typical_spread_pips * self.cfg.execution.spread_multiplier,
                     self.cfg.execution.slippage_pips)


def format_report(stats: RunStats, cfg: EngineConfig, engine: Engine) -> str:
    """Human-readable run summary."""
    closed = stats.wins + stats.losses
    win_rate = (100.0 * stats.wins / closed) if closed else 0.0
    net_pct = (100.0 * stats.realised / cfg.starting_equity) if cfg.starting_equity else 0.0
    avg = (stats.realised / closed) if closed else 0.0

    lines = [
        "",
        "=" * 64,
        "  MERIDIAN RUN REPORT  (%s)" % ("SIMULATED" if cfg.simulated else "LIVE"),
        "=" * 64,
        "  bars processed      %d" % stats.bars,
        "  signals             %d  (vetoed %d, blocked %d)" % (stats.signals, stats.vetoed, stats.blocked),
        "  trades              %d entries / %d exits" % (stats.entries, stats.exits),
        "  closed              %d  (%d win / %d loss, %.1f%%)" % (closed, stats.wins, stats.losses, win_rate),
        "  gross pips          %+.1f" % stats.gross_pips,
        "  financing pips      %+.2f" % stats.swap_pips,
        "  commission          %.2f %s" % (stats.commission, cfg.account_currency),
        "  net PnL             %+.2f %s  (%+.2f%%)" % (stats.realised, cfg.account_currency, net_pct),
        "  average trade       %+.2f %s" % (avg, cfg.account_currency),
        "  max drawdown        %.2f %s" % (stats.max_drawdown, cfg.account_currency),
        "  ending equity       %.2f %s" % (engine.equity, cfg.account_currency),
    ]
    if stats.veto_counts:
        lines.append("  refusals            " + ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(stats.veto_counts.items(), key=lambda kv: -kv[1])[:6]))
    if stats.exit_counts:
        lines.append("  exits by reason     " + ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(stats.exit_counts.items(), key=lambda kv: -kv[1])[:6]))
    lines += [
        "=" * 64,
        "  A simulated result is a behaviour check, not an expectancy estimate.",
        "  Costs, slippage and financing here are assumptions you set.",
        "=" * 64,
        "",
    ]
    return "\n".join(lines)
