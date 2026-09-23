# engine/main.py
"""CLI wiring: parse flags, build the config, run the engine, print the report.

The CLI follows a set-and-run model. Configuration is supplied at startup and
does not change once the loop begins, so a run is reproducible from its command
line alone.

Live execution is deliberately unreachable from here. `--live` reports what is
missing rather than pretending: there is no broker order path in this engine,
and adding one is a decision the operator makes explicitly by writing an
adapter, not a flag they can trip over.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import EngineConfig, apply_carry_preset, validate
from .constants import MERIDIAN_VERSION
from .engine import Engine, format_report
from .feed import CsvReplayFeed, synthetic_bars, write_csv
from .instruments import DEFAULT_INSTRUMENTS, get_instrument
from .macro import default_macro_view
from .persistence import Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meridian",
        description="Meridian - a supervised FX decision engine (simulation only).",
        epilog="Leveraged FX trading loses money for most retail participants. "
               "See RISK_DISCLOSURE.txt before using this on anything real.",
    )
    p.add_argument("--version", action="version", version="meridian %s" % MERIDIAN_VERSION)

    # Every knob below defaults to None rather than to its value. argparse
    # cannot otherwise distinguish "the user did not pass this" from "the user
    # passed the default", so a --config file would be silently overwritten by
    # defaults on every run and --save-config would be useless. The real
    # defaults live in engine/config.py and are quoted in the help text.
    g = p.add_argument_group("universe and data")
    g.add_argument("--symbols", default=None,
                   help="comma-separated pairs (default: EUR/USD,USD/JPY,GBP/USD)")
    g.add_argument("--data-dir", default=None,
                   help="directory holding <SYMBOL>.csv files (default: data)")
    g.add_argument("--bar-minutes", type=int, default=None,
                   help="bar interval of the data (default: 60)")
    g.add_argument("--generate-data", action="store_true",
                   help="write synthetic sample CSVs into --data-dir and exit")
    g.add_argument("--bars", type=int, default=2000, help="bars to generate with --generate-data")

    g = p.add_argument_group("account")
    g.add_argument("--equity", type=float, default=None, help="starting equity (default: 10000)")
    g.add_argument("--currency", default=None, help="account currency (default: USD)")
    g.add_argument("--leverage", type=float, default=None, help="account leverage (default: 30)")

    g = p.add_argument_group("risk")
    g.add_argument("--risk-pct", type=float, default=None,
                   help="percent of equity risked per trade (default: 0.5)")
    g.add_argument("--max-positions", type=int, default=None, help="(default: 4)")
    g.add_argument("--max-daily-loss-pct", type=float, default=None,
                   help="halt trading for the day past this percent loss (default: 3.0)")
    g.add_argument("--max-currency-exposure-pct", type=float, default=None,
                   help="cap on per-currency notional as percent of equity (default: 500). "
                        "This is a concentration limit; one leveraged position is "
                        "normally 300-400%% of equity in notional on its own.")
    g.add_argument("--any-session", action="store_true",
                   help="allow entries outside the London/New York window")

    g = p.add_argument_group("strategy")
    g.add_argument("--strategy", default=None, choices=["trend", "carry"],
                   help="trend = EMA crossover with advisor gates (default). "
                        "carry = hold the higher real-yielding currency, which "
                        "also applies the carry exit preset")
    g.add_argument("--policy-rates", default=None,
                   help="JSON file of dated policy rates, required by --strategy carry. "
                        "See policy_rates.example.json for the shape")
    g.add_argument("--reentry-cooldown-hours", type=float, default=None,
                   help="carry only: hours to wait after an exit before re-opening "
                        "the same pair (default: 72)")
    g.add_argument("--fast-ema", type=int, default=None, help="(default: 20)")
    g.add_argument("--slow-ema", type=int, default=None, help="(default: 60)")
    g.add_argument("--confirm-bars", type=int, default=None, help="(default: 2)")
    g.add_argument("--deadband-pips", type=float, default=None, help="(default: 1.0)")
    g.add_argument("--no-rsi", action="store_true", help="disable the RSI gate")
    g.add_argument("--no-macd", action="store_true", help="disable the MACD gate")

    g = p.add_argument_group("exits")
    g.add_argument("--atr-stop-mult", type=float, default=None, help="(default: 1.8)")
    g.add_argument("--reward-risk", type=float, default=None, help="(default: 2.0)")
    g.add_argument("--max-hold-hours", type=float, default=None,
                   help="market hours, not wall clock (default: 72)")
    g.add_argument("--no-trailing", action="store_true")
    g.add_argument("--hold-over-weekend", action="store_true",
                   help="do not flatten into the Friday close (accepts gap risk)")

    g = p.add_argument_group("costs")
    g.add_argument("--spread-mult", type=float, default=None,
                   help="scale the spread assumption; try 2.0 as a stress test (default: 1.0)")
    g.add_argument("--slippage-pips", type=float, default=None, help="(default: 0.2)")
    g.add_argument("--stop-slippage-pips", type=float, default=None, help="(default: 1.0)")
    g.add_argument("--commission-per-lot", type=float, default=None, help="per side (default: 0.0)")

    g = p.add_argument_group("macro")
    g.add_argument("--no-macro", action="store_true", help="disable carry bias and the event gate")
    g.add_argument("--carry-veto", action="store_true",
                   help="refuse entries against a strong rate differential")

    g = p.add_argument_group("run")
    g.add_argument("--live", action="store_true", help="(not available - see below)")
    g.add_argument("--reset-state", action="store_true", help="clear simulated state before running")
    g.add_argument("--config", help="load a saved JSON config (flags still override)")
    g.add_argument("--save-config", help="write the resolved config to this path and exit")
    g.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    g.add_argument("--quiet", "-q", action="store_true", help="warnings and the report only")
    g.add_argument("--log-bars", action="store_true", help="log every bar (noisy)")
    return p


def config_from_args(args: argparse.Namespace) -> EngineConfig:
    cfg = EngineConfig.load(args.config) if args.config else EngineConfig()

    def put(target, attr, value, scale=1.0):
        """Apply a flag only when it was actually supplied."""
        if value is not None:
            setattr(target, attr, value * scale if scale != 1.0 else value)

    if args.symbols is not None:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    put(cfg, "data_dir", args.data_dir)
    put(cfg, "bar_minutes", args.bar_minutes)
    put(cfg, "account_currency", args.currency)
    put(cfg, "starting_equity", args.equity)
    cfg.simulated = True
    cfg.log_every_bar = args.log_bars

    put(cfg.risk, "risk_per_trade_pct", args.risk_pct, scale=0.01)
    put(cfg.risk, "leverage", args.leverage)
    put(cfg.risk, "max_open_positions", args.max_positions)
    put(cfg.risk, "max_daily_loss_pct", args.max_daily_loss_pct, scale=0.01)
    put(cfg.risk, "max_currency_exposure_pct", args.max_currency_exposure_pct, scale=0.01)

    put(cfg, "strategy", args.strategy)
    put(cfg, "policy_rates_file", args.policy_rates)
    put(cfg.carry, "reentry_cooldown_hours", args.reentry_cooldown_hours)
    put(cfg, "fast_ema", args.fast_ema)
    put(cfg, "slow_ema", args.slow_ema)
    put(cfg.signal, "confirm_bars", args.confirm_bars)
    put(cfg.signal, "deadband_pips", args.deadband_pips)

    put(cfg.exits, "atr_stop_mult", args.atr_stop_mult)
    put(cfg.exits, "reward_risk", args.reward_risk)
    put(cfg.exits, "max_hold_hours", args.max_hold_hours)

    put(cfg.execution, "spread_multiplier", args.spread_mult)
    put(cfg.execution, "slippage_pips", args.slippage_pips)
    put(cfg.execution, "stop_slippage_pips", args.stop_slippage_pips)
    put(cfg.execution, "commission_per_lot", args.commission_per_lot)

    # Switches only ever force the off state. A flag that was not passed must
    # not silently re-enable something a loaded config turned off.
    if args.any_session:
        cfg.risk.require_liquid_session = False
    if args.no_rsi:
        cfg.advisors.enable_rsi = False
    if args.no_macd:
        cfg.advisors.enable_macd = False
    if args.no_trailing:
        cfg.exits.enable_trailing = False
    if args.hold_over_weekend:
        cfg.exits.close_before_weekend = False
    if args.no_macro:
        cfg.macro.enabled = False
    if args.carry_veto:
        cfg.macro.carry_veto_opposing = True

    if cfg.strategy == "carry":
        apply_carry_preset(cfg)
        # Re-apply the exit/cost flags the operator passed explicitly, so the
        # preset is a default rather than an override of their intent.
        put(cfg.exits, "atr_stop_mult", args.atr_stop_mult)
        put(cfg.exits, "reward_risk", args.reward_risk)
        put(cfg.exits, "max_hold_hours", args.max_hold_hours)
        if args.no_trailing:
            cfg.exits.enable_trailing = False
        if args.hold_over_weekend:
            cfg.exits.close_before_weekend = False

    validate(cfg)
    return cfg


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def generate_sample_data(cfg: EngineConfig, bars: int) -> List[str]:
    """Write deterministic synthetic CSVs so a fresh clone can run immediately.

    Synthetic data proves the engine executes. It proves nothing about whether
    a strategy makes money, and the generator has no macro response, no fat
    tails and no session volatility pattern by construction.
    """
    written = []
    start = datetime.now(timezone.utc) - timedelta(minutes=cfg.bar_minutes * bars)
    seeds = {"EUR/USD": (1.0850, 11), "USD/JPY": (151.20, 23), "GBP/USD": (1.2680, 31),
             "AUD/USD": (0.6620, 41), "USD/CHF": (0.8840, 53), "EUR/GBP": (0.8550, 61),
             "EUR/JPY": (163.80, 71)}
    for sym in cfg.instruments():
        inst = get_instrument(sym)
        price, seed = seeds.get(sym, (1.0000, 7))
        series = synthetic_bars(
            symbol=sym, start=start, count=bars, start_price=price,
            minutes=cfg.bar_minutes, drift_pips_per_bar=0.05,
            vol_pips=6.0, pip_size=inst.pip_size, seed=seed,
        )
        path = Path(cfg.data_dir) / ("%s.csv" % sym.replace("/", ""))
        written.append(write_csv(str(path), series))
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args)
    log = logging.getLogger("meridian")

    if args.live:
        log.error(
            "Live trading is not implemented. This engine has no broker order path: "
            "engine/feed.py ships a LiveFeed stub and nothing that places an order. "
            "Wiring one is a deliberate decision, not a flag. See docs/RUNBOOK.md."
        )
        return 2

    cfg = config_from_args(args)

    if args.save_config:
        path = cfg.save(args.save_config)
        log.info("Config written to %s", path)
        return 0

    if args.generate_data:
        for path in generate_sample_data(cfg, args.bars):
            log.info("Wrote %s", path)
        return 0

    files = cfg.resolved_data_files()
    missing = [p for p in files.values() if not Path(p).exists()]
    if missing:
        log.error("Missing data files: %s", ", ".join(missing))
        log.error("Run with --generate-data to create synthetic samples, or point "
                  "--data-dir at your own OHLC CSVs.")
        return 1

    store = Store(simulated=True)
    if args.reset_state:
        store.reset()
        log.info("Simulated state cleared.")

    feed = CsvReplayFeed(files)
    macro = default_macro_view()
    macro.settings = cfg.macro
    if cfg.policy_rates_file:
        try:
            loaded = macro.load_policy_rates(cfg.policy_rates_file)
        except (OSError, ValueError, KeyError) as exc:
            log.error("Could not read policy rates from %s: %s", cfg.policy_rates_file, exc)
            return 1
        log.info("Loaded %d policy rate observations from %s",
                 loaded, cfg.policy_rates_file)
    elif cfg.strategy == "carry":
        log.error(
            "--strategy carry needs --policy-rates <file>. The built-in rates are "
            "undated placeholders, and the engine refuses undated rates rather than "
            "applying today's rate to an older bar. Start from policy_rates.example.json."
        )
        return 1

    engine = Engine(cfg, feed, macro=macro, store=store)

    log.info("Meridian %s - simulated run over %d pair(s), %s account, %.0f:1 leverage",
             MERIDIAN_VERSION, len(cfg.instruments()), cfg.account_currency, cfg.risk.leverage)

    stats = engine.run()
    print(format_report(stats, cfg, engine))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
