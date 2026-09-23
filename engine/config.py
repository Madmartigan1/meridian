# engine/config.py
"""Canonical configuration and validation.

This file is the baseline source of truth. CLI flags override it for a single
invocation; nothing else may introduce another precedence layer.

`validate()` coerces out-of-range values to a safe envelope and warns, rather
than raising. A config typo should downgrade the run, not abort it mid-session
with an open position. The one thing it will not do is soften a risk control
past its hard ceiling.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from .exits import ExitSettings
from .execution import ExecutionSettings
from .instruments import DEFAULT_INSTRUMENTS, Instrument
from .macro import MacroSettings
from .risk import RiskSettings
from .strategy import AdvisorSettings, CarrySettings, SignalSettings

log = logging.getLogger(__name__)

# Hard ceilings. Config may be more conservative than these, never less.
MAX_RISK_PER_TRADE_PCT = 0.02      # 2% of equity on one trade
MAX_LEVERAGE = 50.0
MAX_TOTAL_RISK_PCT = 0.06
MIN_STOP_PIPS_FLOOR = 1.0


@dataclass
class EngineConfig:
    """Everything one run needs."""

    # --- account ---
    account_currency: str = "USD"
    starting_equity: float = 10_000.0

    # --- universe ---
    symbols: List[str] = field(default_factory=lambda: ["EUR/USD", "USD/JPY", "GBP/USD"])
    bar_minutes: int = 60

    # --- strategy ---
    strategy: str = "trend"          # "trend" or "carry"
    policy_rates_file: str = ""      # dated rate history; carry refuses without one

    # --- run mode ---
    simulated: bool = True          # live execution is not implemented; see feed.py
    log_level: str = "INFO"
    log_every_bar: bool = False

    # --- data ---
    data_dir: str = "data"
    data_files: Dict[str, str] = field(default_factory=dict)

    # --- indicator periods (in bars of bar_minutes) ---
    fast_ema: int = 20
    slow_ema: int = 60
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14

    # --- component settings ---
    signal: SignalSettings = field(default_factory=SignalSettings)
    carry: CarrySettings = field(default_factory=CarrySettings)
    advisors: AdvisorSettings = field(default_factory=AdvisorSettings)
    exits: ExitSettings = field(default_factory=ExitSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    macro: MacroSettings = field(default_factory=MacroSettings)

    # --- macro bias wiring ---
    macro_size_scaling: bool = True     # let macro agreement scale size, bounded
    macro_size_floor: float = 0.5       # never below half the risk-based size
    macro_size_ceiling: float = 1.0     # never above it -- macro cannot add risk

    def instruments(self) -> Dict[str, Instrument]:
        """The instrument table restricted to the configured universe."""
        table: Dict[str, Instrument] = {}
        for sym in self.symbols:
            key = sym.upper()
            if key not in DEFAULT_INSTRUMENTS:
                log.warning("Config: unknown symbol %s; dropping from universe.", key)
                continue
            table[key] = DEFAULT_INSTRUMENTS[key]
        return table

    def resolved_data_files(self) -> Dict[str, str]:
        """Map symbol to CSV path, defaulting to <data_dir>/<SYMBOL>.csv."""
        out: Dict[str, str] = {}
        for sym in self.instruments():
            explicit = self.data_files.get(sym) or self.data_files.get(sym.replace("/", ""))
            if explicit:
                out[sym] = explicit
                continue
            out[sym] = str(Path(self.data_dir) / ("%s.csv" % sym.replace("/", "")))
        return out

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return str(p)

    @classmethod
    def load(cls, path: str) -> "EngineConfig":
        """Load a saved config. Unknown keys are ignored, missing keys default."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = cls()
        nested = {
            "signal": SignalSettings, "advisors": AdvisorSettings,
            "carry": CarrySettings, "exits": ExitSettings,
            "execution": ExecutionSettings, "risk": RiskSettings,
            "macro": MacroSettings,
        }
        for key, value in raw.items():
            if key in nested and isinstance(value, dict):
                klass = nested[key]
                allowed = {k: v for k, v in value.items() if k in klass.__dataclass_fields__}
                if klass is MacroSettings and "block_impact" in allowed:
                    allowed["block_impact"] = tuple(allowed["block_impact"])
                setattr(cfg, key, klass(**allowed))
            elif key in cls.__dataclass_fields__:
                setattr(cfg, key, value)
        validate(cfg)
        return cfg


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def apply_carry_preset(cfg: "EngineConfig") -> None:
    """Exit profile for carry. Without it, carry cannot work at all.

    Trend exits actively destroy a carry position:

      * `max_hold_hours` closes it long before the differential has earned
        anything -- carry pays out over weeks, in daily increments.
      * `enable_stagnation` closes precisely the positions that are working. A
        carry position going nowhere on price *is* the thesis: you are being
        paid the rate differential to hold it, not to be right on direction.
      * `close_before_weekend` forfeits the roll, and the Wednesday roll pays
        triple. Flattening every Friday means paying the spread weekly to
        avoid collecting the thing you came for.

    The stop stays mandatory, and is widened rather than removed: carry's real
    risk is the violent unwind, which is exactly what a stop is for. Trailing
    is turned off because it would ratchet a slow-drifting position out on the
    first normal retracement.
    """
    cfg.exits.enable_max_hold = False
    cfg.exits.enable_stagnation = False
    cfg.exits.close_before_weekend = False
    cfg.exits.enable_trailing = False
    cfg.exits.enable_take_profit = False      # carry has no price target
    cfg.exits.atr_stop_mult = 6.0             # wide: survive noise, catch an unwind
    cfg.exits.max_stop_pips = 300.0

    # Direction comes from the differential, so trend confirmations must not
    # gate it. Leaving them on silently turns carry back into a trend strategy.
    cfg.advisors.enable_rsi = False
    cfg.advisors.enable_macd = False

    # The entry is the carry view itself; the bias must not also shrink it.
    cfg.macro_size_scaling = False
    cfg.macro.enabled = True
    cfg.macro.use_carry_bias = True


def validate(cfg: EngineConfig) -> bool:
    """Coerce to a safe envelope. Returns False if anything had to be changed."""
    safe = True

    strategy = str(getattr(cfg, "strategy", "trend") or "trend").strip().lower()
    if strategy not in ("trend", "carry"):
        log.warning("Config: strategy=%r invalid; defaulting to 'trend'.", strategy)
        strategy = "trend"
        safe = False
    cfg.strategy = strategy

    if cfg.carry.reentry_cooldown_hours < 0:
        log.warning("Config: reentry_cooldown_hours cannot be negative; using 0.")
        cfg.carry.reentry_cooldown_hours = 0.0
        safe = False

    cfg.account_currency = str(cfg.account_currency or "USD").upper().strip()

    if cfg.starting_equity <= 0:
        log.warning("Config: starting_equity must be positive; using 10000.")
        cfg.starting_equity = 10_000.0
        safe = False

    cfg.symbols = [s.upper().strip() for s in cfg.symbols if "/" in s]
    if not cfg.symbols:
        log.warning("Config: no valid symbols; defaulting to EUR/USD.")
        cfg.symbols = ["EUR/USD"]
        safe = False

    if not (1 <= cfg.bar_minutes <= 1440):
        log.warning("Config: bar_minutes out of [1,1440]; clamping.")
        cfg.bar_minutes = int(_clamp(cfg.bar_minutes, 1, 1440))
        safe = False

    # --- risk: the ceilings that may not be raised --------------------------
    r = cfg.risk
    if not (0.0 < r.risk_per_trade_pct <= MAX_RISK_PER_TRADE_PCT):
        log.warning("Config: risk_per_trade_pct %.4f outside (0, %.2f]; clamping.",
                    r.risk_per_trade_pct, MAX_RISK_PER_TRADE_PCT)
        r.risk_per_trade_pct = _clamp(r.risk_per_trade_pct or 0.005, 0.0001, MAX_RISK_PER_TRADE_PCT)
        safe = False

    if not (0.0 < r.leverage <= MAX_LEVERAGE):
        log.warning("Config: leverage %.1f outside (0, %.0f]; clamping.", r.leverage, MAX_LEVERAGE)
        r.leverage = _clamp(r.leverage or 30.0, 1.0, MAX_LEVERAGE)
        safe = False

    if not (0.0 < r.max_total_risk_pct <= MAX_TOTAL_RISK_PCT):
        log.warning("Config: max_total_risk_pct outside (0, %.2f]; clamping.", MAX_TOTAL_RISK_PCT)
        r.max_total_risk_pct = _clamp(r.max_total_risk_pct or 0.02, 0.001, MAX_TOTAL_RISK_PCT)
        safe = False

    if r.max_total_risk_pct < r.risk_per_trade_pct:
        log.warning("Config: max_total_risk_pct below risk_per_trade_pct; raising to match.")
        r.max_total_risk_pct = r.risk_per_trade_pct
        safe = False

    if not (1 <= r.max_open_positions <= 20):
        log.warning("Config: max_open_positions out of [1,20]; clamping.")
        r.max_open_positions = int(_clamp(r.max_open_positions, 1, 20))
        safe = False

    if not (0.0 < r.max_daily_loss_pct <= 0.20):
        log.warning("Config: max_daily_loss_pct out of (0,0.20]; clamping.")
        r.max_daily_loss_pct = _clamp(r.max_daily_loss_pct or 0.03, 0.005, 0.20)
        safe = False

    if not (0.1 <= r.max_currency_exposure_pct <= 50.0):
        log.warning("Config: max_currency_exposure_pct %.2f outside [0.1, 50]; clamping. "
                    "Values near 1.0 refuse every leveraged trade, not just the "
                    "concentrated ones.", r.max_currency_exposure_pct)
        r.max_currency_exposure_pct = _clamp(r.max_currency_exposure_pct or 5.0, 0.1, 50.0)
        safe = False

    if not (0.0 < r.max_margin_utilisation <= 1.0):
        log.warning("Config: max_margin_utilisation out of (0,1]; clamping.")
        r.max_margin_utilisation = _clamp(r.max_margin_utilisation or 0.3, 0.01, 1.0)
        safe = False

    # --- exits: the stop envelope -------------------------------------------
    e = cfg.exits
    if e.min_stop_pips < MIN_STOP_PIPS_FLOOR:
        log.warning("Config: min_stop_pips below %.1f floor; raising. A stop inside "
                    "the spread is not a stop.", MIN_STOP_PIPS_FLOOR)
        e.min_stop_pips = MIN_STOP_PIPS_FLOOR
        safe = False

    if e.max_stop_pips < e.min_stop_pips:
        log.warning("Config: max_stop_pips below min_stop_pips; swapping.")
        e.min_stop_pips, e.max_stop_pips = e.max_stop_pips, e.min_stop_pips
        safe = False

    if not (0.1 <= e.atr_stop_mult <= 10.0):
        log.warning("Config: atr_stop_mult out of [0.1,10]; clamping.")
        e.atr_stop_mult = _clamp(e.atr_stop_mult, 0.1, 10.0)
        safe = False

    if not (0.1 <= e.reward_risk <= 20.0):
        log.warning("Config: reward_risk out of [0.1,20]; clamping.")
        e.reward_risk = _clamp(e.reward_risk, 0.1, 20.0)
        safe = False

    if e.enable_trailing and e.trail_distance_r <= 0:
        log.warning("Config: trail_distance_r must be positive; disabling trailing.")
        e.enable_trailing = False
        safe = False

    if e.enable_max_hold and not (0 < e.max_hold_hours <= 24 * 30):
        log.warning("Config: max_hold_hours out of (0,720]; clamping.")
        e.max_hold_hours = _clamp(e.max_hold_hours or 72.0, 1.0, 720.0)
        safe = False

    # --- execution ----------------------------------------------------------
    x = cfg.execution
    if x.slippage_pips < 0 or x.stop_slippage_pips < 0:
        log.warning("Config: slippage cannot be negative; clamping to zero.")
        x.slippage_pips = max(0.0, x.slippage_pips)
        x.stop_slippage_pips = max(0.0, x.stop_slippage_pips)
        safe = False

    if x.stop_slippage_pips < x.slippage_pips:
        log.warning("Config: stop slippage below normal slippage. Stops fill in the "
                    "worst liquidity; raising to match.")
        x.stop_slippage_pips = x.slippage_pips
        safe = False

    if x.spread_multiplier <= 0:
        log.warning("Config: spread_multiplier must be positive; using 1.0.")
        x.spread_multiplier = 1.0
        safe = False

    if x.max_spread_pips <= 0:
        log.warning("Config: max_spread_pips must be positive; using 3.5.")
        x.max_spread_pips = 3.5
        safe = False

    # --- signal / advisors --------------------------------------------------
    s = cfg.signal
    if not (1 <= s.confirm_bars <= 20):
        log.warning("Config: confirm_bars out of [1,20]; clamping.")
        s.confirm_bars = int(_clamp(s.confirm_bars, 1, 20))
        safe = False

    if s.deadband_pips < 0:
        log.warning("Config: deadband_pips cannot be negative; using 0.")
        s.deadband_pips = 0.0
        safe = False

    if cfg.fast_ema >= cfg.slow_ema:
        log.warning("Config: fast_ema (%d) must be shorter than slow_ema (%d); "
                    "resetting to 20/60.", cfg.fast_ema, cfg.slow_ema)
        cfg.fast_ema, cfg.slow_ema = 20, 60
        safe = False

    if cfg.macd_fast >= cfg.macd_slow:
        log.warning("Config: macd_fast must be shorter than macd_slow; resetting to 12/26.")
        cfg.macd_fast, cfg.macd_slow = 12, 26
        safe = False

    for name in ("rsi_period", "macd_signal", "atr_period"):
        if getattr(cfg, name) < 2:
            log.warning("Config: %s must be at least 2; clamping.", name)
            setattr(cfg, name, 2)
            safe = False

    a = cfg.advisors
    if not (0.0 <= a.rsi_long_max <= 100.0) or not (0.0 <= a.rsi_short_min <= 100.0):
        log.warning("Config: RSI bounds out of [0,100]; resetting to 72/28.")
        a.rsi_long_max, a.rsi_short_min = 72.0, 28.0
        safe = False

    # --- macro sizing -------------------------------------------------------
    if cfg.macro_size_ceiling > 1.0:
        log.warning("Config: macro_size_ceiling above 1.0 would let a macro view add "
                    "risk beyond the per-trade budget; clamping to 1.0.")
        cfg.macro_size_ceiling = 1.0
        safe = False

    cfg.macro_size_floor = _clamp(cfg.macro_size_floor, 0.0, cfg.macro_size_ceiling)

    if not safe:
        log.warning("Config: one or more values were coerced. Review the warnings above.")
    return safe
