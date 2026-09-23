# engine/strategy.py
"""Signal generation and advisory gates.

The structure deliberately mirrors a captain-and-advisors split: one indicator
decides direction, the rest may only veto. That keeps the reason for every
trade expressible in one line of log, which is what makes the engine
supervisable rather than a black box.

The difference from a long-only engine is that direction is symmetric here.
A bearish crossover is not "exit" -- it is a short entry candidate, and it must
pass the mirror image of the same gates a long would.

Two signal sources live here and share one interface, `evaluate_gated(ctx)`:

  * `TrendSignal` -- EMA crossover with a deadband and confirmation, gated by
    the RSI/MACD/ATR advisors.
  * `CarrySignal` -- hold the higher real-yielding currency, directed by the
    rate differential in force at the bar being processed.

They are interchangeable from the engine's point of view, which is what keeps
swapping strategies a one-file change rather than an architectural one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .indicators import IndicatorSet
from .instruments import Instrument, price_to_pips
from .position import FLAT, LONG, SHORT

if TYPE_CHECKING:                     # import only for type checkers, so the
    from .macro import MacroView      # runtime import graph stays acyclic


@dataclass
class Signal:
    """A proposed direction with the reason it was proposed or refused."""
    side: str            # LONG, SHORT or FLAT
    reason: str
    vetoed_by: str = ""

    @property
    def actionable(self) -> bool:
        return self.side in (LONG, SHORT) and not self.vetoed_by


@dataclass
class AdvisorSettings:
    """Gate parameters. Each gate is two-sided and independently disableable."""
    enable_rsi: bool = True
    rsi_long_max: float = 72.0     # do not buy into an exhausted push
    rsi_short_min: float = 28.0    # do not sell into an exhausted flush

    enable_macd: bool = True
    macd_normalize: bool = True    # express histogram in pips of the pair
    macd_long_min: float = 0.0     # histogram must be at least this to go long
    macd_short_max: float = 0.0    # histogram must be at most this to go short

    enable_atr_floor: bool = True
    min_atr_pips: float = 4.0      # refuse to trade a range smaller than costs


@dataclass
class SignalSettings:
    """Entry trigger parameters."""
    confirm_bars: int = 2          # bars the crossover must persist
    deadband_pips: float = 1.0     # EMA separation required to call it a cross


def _macd_metric(inst: Instrument, hist: Optional[float], normalize: bool) -> Optional[float]:
    """MACD histogram, optionally converted to pips so thresholds are portable.

    Raw MACD magnitude scales with price level, so a threshold tuned on EUR/USD
    at 1.08 is meaningless on USD/JPY at 150. Expressing it in pips of the pair
    makes one number valid across the whole universe.
    """
    if hist is None:
        return None
    if not normalize:
        return hist
    return price_to_pips(inst, hist)


def advisor_allows(
    side: str,
    ind: IndicatorSet,
    inst: Instrument,
    settings: AdvisorSettings,
) -> str:
    """Return the name of the vetoing gate, or "" when the entry may proceed."""
    if settings.enable_rsi and ind.rsi.value is not None:
        if side == LONG and ind.rsi.value > settings.rsi_long_max:
            return "rsi_overbought"
        if side == SHORT and ind.rsi.value < settings.rsi_short_min:
            return "rsi_oversold"

    if settings.enable_macd:
        m = _macd_metric(inst, ind.macd.hist, settings.macd_normalize)
        if m is not None:
            if side == LONG and m < settings.macd_long_min:
                return "macd_not_confirming"
            if side == SHORT and m > settings.macd_short_max:
                return "macd_not_confirming"

    if settings.enable_atr_floor and ind.atr.value is not None:
        atr_pips = price_to_pips(inst, ind.atr.value)
        if atr_pips < settings.min_atr_pips:
            return "atr_below_floor"

    return ""


class TrendSignal:
    """EMA crossover with a deadband and persistence requirement.

    Two guards against the classic failure of crossover systems in FX, where
    most of the day is noise around an unchanged rate:

    - deadband: the EMAs must separate by a minimum pip distance, so a spread
      that oscillates around zero does not fire repeatedly.
    - confirm_bars: the separation must hold for N consecutive closed bars.
    """

    def __init__(self, settings: Optional[SignalSettings] = None):
        self.settings = settings or SignalSettings()
        self._streak_side: str = FLAT
        self._streak = 0

    def reset(self) -> None:
        self._streak_side = FLAT
        self._streak = 0

    def evaluate(self, ind: IndicatorSet, inst: Instrument) -> Signal:
        if not ind.ready:
            return Signal(FLAT, "warmup:%d bars left" % ind.warmup_left)

        spread_pips = price_to_pips(inst, ind.spread)
        band = self.settings.deadband_pips

        if spread_pips > band:
            side = LONG
        elif spread_pips < -band:
            side = SHORT
        else:
            self._streak_side, self._streak = FLAT, 0
            return Signal(FLAT, "ema separation %.1f pips inside deadband" % spread_pips)

        if side == self._streak_side:
            self._streak += 1
        else:
            self._streak_side, self._streak = side, 1

        if self._streak < self.settings.confirm_bars:
            return Signal(
                FLAT,
                "%s forming (%d/%d bars)" % (side.lower(), self._streak, self.settings.confirm_bars),
            )

        return Signal(side, "ema %s by %.1f pips, held %d bars" % (
            "up" if side == LONG else "down", abs(spread_pips), self._streak))

    def evaluate_gated(self, ctx: "SignalContext") -> Signal:
        """Full decision: trigger first, then gates. Never loses the reason."""
        sig = self.evaluate(ctx.indicators, ctx.inst)
        if sig.side == FLAT:
            return sig
        veto = advisor_allows(sig.side, ctx.indicators, ctx.inst, ctx.advisors)
        if veto:
            return Signal(sig.side, sig.reason, vetoed_by=veto)
        return sig


@dataclass
class SignalContext:
    """Everything a signal source may look at on one bar.

    A single context object keeps the engine loop indifferent to which strategy
    is loaded: trend reads the indicators, carry reads the macro view and the
    bar's timestamp, and neither needs the engine to know the difference.
    """
    inst: Instrument
    indicators: IndicatorSet
    now: datetime
    advisors: AdvisorSettings
    macro: Optional["MacroView"] = None
    last_exit_utc: Optional[datetime] = None


@dataclass
class CarrySettings:
    """Carry entry parameters.

    The size threshold is deliberately absent: it lives on `MacroSettings.
    carry_min_differential` and is applied by `MacroView.carry_bias`. Two knobs
    for one threshold is how an operator ends up reading a number the engine is
    not using.
    """
    reentry_cooldown_hours: float = 72.0
    require_atr_floor: bool = True


class CarrySignal:
    """Hold the higher real-yielding currency of the pair.

    The direction comes from the inflation-adjusted policy rate differential as
    it stood *at the bar being processed*, never the latest known rate. A
    missing rate for that date produces a refusal (`no_rate_asof`), not a
    fallback -- see `engine/macro.py` for why that distinction is the whole
    difference between a carry backtest and a fabricated one.

    Two behaviours that are not optional:

    - **Re-entry cooldown.** A carry signal is persistent by construction: the
      differential does not change because a stop was hit. Without a cooldown
      the very next bar re-opens the identical position and the stop becomes a
      fee paid over and over.
    - **No trend gates.** RSI and MACD are trend confirmations. Applying them
      here would quietly turn carry back into a trend strategy wearing a carry
      label. Only the ATR floor applies, and only because stop distance is
      derived from ATR.
    """

    def __init__(self, settings: Optional[CarrySettings] = None):
        self.settings = settings or CarrySettings()

    def reset(self) -> None:
        pass          # stateless: the cooldown is carried on the context

    def evaluate_gated(self, ctx: "SignalContext") -> Signal:
        ind = ctx.indicators
        if not ind.atr.ready:
            return Signal(FLAT, "warmup:%d bars left" % ind.atr.warmup_left)

        if ctx.macro is None:
            return Signal(FLAT, "no_macro: carry needs a macro view")

        if ctx.last_exit_utc is not None:
            cooldown = self.settings.reentry_cooldown_hours
            elapsed = (ctx.now - ctx.last_exit_utc).total_seconds() / 3600.0
            if elapsed < cooldown:
                return Signal(
                    FLAT,
                    "cooldown: %.0f of %.0f hours since last exit" % (elapsed, cooldown),
                )

        side, strength, reason = ctx.macro.carry_bias(ctx.inst, asof=ctx.now)
        if side == FLAT:
            return Signal(FLAT, reason)

        if self.settings.require_atr_floor and ind.atr.value is not None:
            atr_pips = price_to_pips(ctx.inst, ind.atr.value)
            if atr_pips < ctx.advisors.min_atr_pips:
                return Signal(side, reason, vetoed_by="atr_below_floor")

        return Signal(side, "carry %.2f strength | %s" % (strength, reason))


def build_signal(strategy: str, signal_settings, carry_settings):
    """Construct the signal source named by `strategy`."""
    name = (strategy or "trend").strip().lower()
    if name == "carry":
        return CarrySignal(carry_settings)
    if name == "trend":
        return TrendSignal(signal_settings)
    raise ValueError("unknown strategy %r; expected 'trend' or 'carry'" % (strategy,))
