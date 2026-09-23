# engine/macro.py
"""Macro layer: rate differentials, growth/inflation tilt, and the event gate.

This is where the drivers from the source material live. A currency pair is a
relative price between two economies, so the things that move it are relative:

  * Interest rate differential -- capital chases yield, and holding the higher
    yielder earns financing. This is the carry score.
  * Inflation -- erodes purchasing power, and shapes what rates do next.
  * Growth -- attracts investment, and shapes the same expectation.
  * Surprise, not level -- price moves on data differing from the forecast
    that was already in the price. An expected hike is already priced; the gap
    between actual and consensus is the tradable part.

Two hard limits are worth stating in the code rather than the docs:

  1. Carry is a real, documented FX return source over long horizons, and it
     crashes -- the high yielder unwinds violently, wiping out months of drift.
     So carry is used here only as a *bias*, never as a standalone entry, and
     it can never remove or widen a stop.
  2. The engine cannot forecast a release. It can only refuse to be holding a
     fresh position through one. That is the calendar gate, and it is the
     honest use of this layer.

All adjustments are bounded, gradual, and never touch the entry logic itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .instruments import Instrument
from .position import FLAT, LONG, SHORT

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"


@dataclass
class MacroSettings:
    enabled: bool = True

    # Carry
    use_carry_bias: bool = True
    carry_min_differential: float = 1.0     # percentage points before it counts
    carry_veto_opposing: bool = False       # refuse entries against a strong carry
    carry_max_differential: float = 5.0     # cap, so one outlier cannot dominate

    # Event gate
    use_event_gate: bool = True
    block_before_minutes: int = 30
    block_after_minutes: int = 15
    block_impact: Tuple[str, ...] = (HIGH,)
    flatten_before_high_impact: bool = False  # close open positions, not just block

    # Surprise
    surprise_decay_hours: float = 12.0      # how long a surprise keeps tilting
    surprise_weight: float = 0.5            # bounded contribution to the score


@dataclass
class PolicyRate:
    """One central bank rate observation, effective from a date.

    `effective` is what makes a carry backtest honest. A single undated rate
    applied across a two-year replay means the engine "knew" today's policy
    rate two years ago -- lookahead bias of the purest kind, which produces a
    confident equity curve built on information that did not exist at the time.
    An undated point is therefore usable for inspection only, and can never
    satisfy a dated query.
    """
    currency: str
    rate: float                  # annualised policy rate, percent
    inflation: Optional[float] = None
    growth: Optional[float] = None
    effective: Optional[str] = None      # ISO date; None means undated
    source: str = ""
    updated: str = ""

    @property
    def effective_dt(self) -> Optional[datetime]:
        if not self.effective:
            return None
        try:
            dt = datetime.fromisoformat(str(self.effective))
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @property
    def real_rate(self) -> Optional[float]:
        """Policy rate less inflation. A high nominal rate with higher
        inflation is not attractive yield -- it is currency debasement."""
        if self.inflation is None:
            return None
        return self.rate - self.inflation


class PolicyRateSeries:
    """The rate history for one currency, queried as of a date.

    Lookup returns the most recent observation effective on or before the
    requested moment, or None when the moment predates the history. None must
    propagate to a refusal, never to a fallback on the newest rate -- the same
    refuse-rather-than-guess rule the sizer follows for conversion rates.
    """

    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)

    def __init__(self, currency: str):
        self.currency = currency.upper()
        self._points: List[PolicyRate] = []

    def __len__(self) -> int:
        return len(self._points)

    def add(self, point: PolicyRate) -> None:
        self._points.append(point)
        # Undated points sort last; only an undated query can reach them.
        self._points.sort(key=lambda p: (p.effective_dt is None,
                                         p.effective_dt or self._EPOCH))

    def asof(self, when: Optional[datetime]) -> Optional[PolicyRate]:
        if not self._points:
            return None
        if when is None:
            return self._points[-1]      # inspection mode
        chosen = None
        for point in self._points:
            dt = point.effective_dt
            if dt is None:
                break                    # undated points cannot answer a dated query
            if dt <= when:
                chosen = point
            else:
                break
        return chosen

    @property
    def points(self) -> List[PolicyRate]:
        return list(self._points)

    @property
    def first_effective(self) -> Optional[datetime]:
        for point in self._points:
            if point.effective_dt is not None:
                return point.effective_dt
        return None

    @property
    def is_dated(self) -> bool:
        return self.first_effective is not None


@dataclass
class EconomicEvent:
    """A scheduled release. `actual` and `forecast` are filled in after print."""
    when_utc: str
    currency: str
    name: str
    impact: str = HIGH
    forecast: Optional[float] = None
    actual: Optional[float] = None
    higher_is_stronger: bool = True   # CPI/GDP/payrolls yes; unemployment no

    @property
    def dt(self) -> Optional[datetime]:
        try:
            d = datetime.fromisoformat(self.when_utc)
        except (TypeError, ValueError):
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    @property
    def surprise(self) -> Optional[float]:
        """Actual less forecast, signed so positive always means 'stronger'.

        This is the quantity that actually moves price. A hot print against a
        hot forecast is not news; the same print against a cold forecast is.
        """
        if self.actual is None or self.forecast is None:
            return None
        delta = self.actual - self.forecast
        return delta if self.higher_is_stronger else -delta


class MacroView:
    """Holds policy rates and the calendar, and answers bias/gate questions."""

    def __init__(
        self,
        settings: Optional[MacroSettings] = None,
        rates: Optional[Dict[str, PolicyRate]] = None,
        calendar: Optional[List[EconomicEvent]] = None,
    ):
        self.settings = settings or MacroSettings()
        self.policy: Dict[str, PolicyRateSeries] = {}
        for value in (rates or {}).values():
            self.add_policy_rate(value)
        self.calendar: List[EconomicEvent] = list(calendar or [])

    # --- ingestion ----------------------------------------------------------

    def add_policy_rate(self, point: PolicyRate) -> None:
        cur = point.currency.upper()
        self.policy.setdefault(cur, PolicyRateSeries(cur)).add(point)

    def set_policy_rate(self, currency: str, rate: float, **kw) -> None:
        """Add one rate observation. Pass `effective="YYYY-MM-DD"` to date it.

        Without `effective` the point is undated and can only be read in
        inspection mode -- a dated query, which is what the engine issues on
        every bar, will not see it.
        """
        cur = currency.upper()
        self.add_policy_rate(PolicyRate(currency=cur, rate=float(rate), **kw))

    def load_policy_rates(self, path) -> int:
        """Load a dated rate history from JSON. Returns the number of points.

        Expected shape:

            {"USD": [{"effective": "2024-09-18", "rate": 5.00, "inflation": 2.4},
                     {"effective": "2024-11-07", "rate": 4.75, "inflation": 2.6}],
             "JPY": [...]}
        """
        import json
        import logging
        from pathlib import Path

        path = Path(path)
        if "example" in path.name.lower():
            logging.getLogger("meridian").warning(
                "Loading %s: these are ILLUSTRATIVE rates, not real central bank "
                "data. Any carry result from them is meaningless. Copy it to "
                "policy_rates.json and fill in published rates and decision dates.",
                path.name,
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        for currency, entries in raw.items():
            if currency.startswith("_"):
                continue                      # allow "_comment" keys in the file
            for entry in entries:
                self.add_policy_rate(PolicyRate(
                    currency=currency.upper(),
                    rate=float(entry["rate"]),
                    inflation=entry.get("inflation"),
                    growth=entry.get("growth"),
                    effective=entry.get("effective"),
                    source=entry.get("source", str(path)),
                ))
                count += 1
        return count

    def add_event(self, event: EconomicEvent) -> None:
        self.calendar.append(event)

    # --- coverage -----------------------------------------------------------

    def rate_asof(self, currency: str, when: Optional[datetime]) -> Optional[PolicyRate]:
        series = self.policy.get(currency.upper())
        return series.asof(when) if series else None

    def coverage(self, symbols) -> Dict[str, Optional[datetime]]:
        """First dated observation per currency touched by `symbols`.

        None means that currency has no dated history at all, so every carry
        decision involving it will refuse. Reported at startup so the operator
        learns this from a log line rather than from an empty trade list.
        """
        out: Dict[str, Optional[datetime]] = {}
        for symbol in symbols:
            base, quote = symbol.upper().split("/")
            for currency in (base, quote):
                series = self.policy.get(currency)
                out[currency] = series.first_effective if series else None
        return out

    # --- carry --------------------------------------------------------------

    def differential(
        self,
        inst: Instrument,
        asof: Optional[datetime] = None,
        use_real: bool = True,
    ) -> Optional[float]:
        """Base rate less quote rate as of `asof`, in percentage points.

        Positive means holding the pair long earns the differential, which is
        the mechanism behind both the carry trade and the swap line in PnL.

        Returns None when either leg has no observation effective by `asof`.
        The caller must refuse to trade on None rather than reach for the
        newest rate, which would be lookahead.
        """
        b = self.rate_asof(inst.base, asof)
        q = self.rate_asof(inst.quote, asof)
        if b is None or q is None:
            return None
        if use_real and b.real_rate is not None and q.real_rate is not None:
            return b.real_rate - q.real_rate
        return b.rate - q.rate

    def carry_bias(
        self,
        inst: Instrument,
        asof: Optional[datetime] = None,
    ) -> Tuple[str, float, str]:
        """(direction, strength 0..1, human reason).

        Strength is normalised and capped so a single extreme differential
        cannot dominate the decision.
        """
        if not (self.settings.enabled and self.settings.use_carry_bias):
            return FLAT, 0.0, "carry bias disabled"
        diff = self.differential(inst, asof=asof)
        if diff is None:
            if asof is not None:
                return FLAT, 0.0, "no_rate_asof: no %s or %s rate effective by %s" % (
                    inst.base, inst.quote, asof.date())
            return FLAT, 0.0, "no policy rates for %s" % inst.symbol
        if abs(diff) < self.settings.carry_min_differential:
            return FLAT, 0.0, "differential %.2f below %.2f threshold" % (
                diff, self.settings.carry_min_differential)
        strength = min(1.0, abs(diff) / max(self.settings.carry_max_differential, 1e-9))
        side = LONG if diff > 0 else SHORT
        return side, strength, "%s carry %+.2f pts favours %s" % (
            inst.symbol, diff, side.lower())

    # --- surprises ----------------------------------------------------------

    def surprise_bias(self, inst: Instrument, now_utc: datetime) -> Tuple[str, float, str]:
        """Recent data surprises, decayed over time, netted across both legs.

        A strong US print is bullish USD, which is bearish EUR/USD -- the sign
        flips depending on which leg of the pair the currency sits on.
        """
        if not self.settings.enabled:
            return FLAT, 0.0, "macro disabled"
        score = 0.0
        notes = []
        for ev in self.calendar:
            s = ev.surprise
            dt = ev.dt
            if s is None or dt is None or dt > now_utc:
                continue
            age_h = (now_utc - dt).total_seconds() / 3600.0
            if age_h > self.settings.surprise_decay_hours:
                continue
            decay = 1.0 - (age_h / self.settings.surprise_decay_hours)
            cur = ev.currency.upper()
            if cur == inst.base:
                leg = 1.0
            elif cur == inst.quote:
                leg = -1.0
            else:
                continue
            contribution = leg * (1.0 if s > 0 else -1.0) * decay
            score += contribution
            notes.append("%s %s %+.2f" % (cur, ev.name, s))
        if not notes:
            return FLAT, 0.0, "no recent surprises"
        strength = min(1.0, abs(score)) * self.settings.surprise_weight
        side = LONG if score > 0 else SHORT
        return side, strength, "surprise tilt %s (%s)" % (side.lower(), ", ".join(notes[:3]))

    # --- event gate ---------------------------------------------------------

    def blocking_event(self, inst: Instrument, now_utc: datetime) -> Optional[EconomicEvent]:
        """The high-impact release, if any, whose blackout window we are inside.

        This is the layer's most useful output. Around a release the spread
        widens, the first move frequently reverses, and a stop is filled
        wherever liquidity happens to be -- conditions where the engine's own
        cost assumptions stop holding.
        """
        if not (self.settings.enabled and self.settings.use_event_gate):
            return None
        before = timedelta(minutes=self.settings.block_before_minutes)
        after = timedelta(minutes=self.settings.block_after_minutes)
        for ev in self.calendar:
            if ev.impact.upper() not in self.settings.block_impact:
                continue
            cur = ev.currency.upper()
            if cur not in (inst.base, inst.quote):
                continue
            dt = ev.dt
            if dt is None:
                continue
            if (dt - before) <= now_utc <= (dt + after):
                return ev
        return None

    # --- combined -----------------------------------------------------------

    def evaluate(self, inst: Instrument, side: str, now_utc: datetime) -> Tuple[bool, float, str]:
        """(allowed, bias_score, reason) for a proposed entry.

        bias_score is positive when macro agrees with `side`, negative when it
        disagrees. It is advisory: the engine may use it to size down or to
        require a stronger technical signal, never to skip a stop or to enter
        without a trigger.
        """
        if not self.settings.enabled:
            return True, 0.0, "macro disabled"

        ev = self.blocking_event(inst, now_utc)
        if ev is not None:
            return False, 0.0, "blackout: %s %s" % (ev.currency, ev.name)

        # asof=now_utc, never the latest rate: on a replay the engine must only
        # see the policy rate that was actually in force at the bar it is on.
        carry_side, carry_strength, carry_reason = self.carry_bias(inst, asof=now_utc)
        surp_side, surp_strength, surp_reason = self.surprise_bias(inst, now_utc)

        score = 0.0
        if carry_side != FLAT:
            score += carry_strength * (1.0 if carry_side == side else -1.0)
        if surp_side != FLAT:
            score += surp_strength * (1.0 if surp_side == side else -1.0)

        if (self.settings.carry_veto_opposing and carry_side != FLAT
                and carry_side != side and carry_strength >= 0.5):
            return False, score, "against strong carry: %s" % carry_reason

        return True, score, "%s; %s" % (carry_reason, surp_reason)


def default_macro_view() -> MacroView:
    """A starting policy-rate table, deliberately **undated**.

    These are placeholders in the right shape, not live data, and leaving them
    undated is the safety property rather than an omission: an undated point
    cannot satisfy a dated query, so the engine refuses every carry decision
    built on them instead of quietly applying today's rate to a bar from two
    years ago.

    To trade carry for real, load a dated history with
    `MacroView.load_policy_rates(path)` -- see `policy_rates.example.json` --
    filled in from each central bank's published decision dates.
    """
    view = MacroView()
    for cur, rate, infl in (
        ("USD", 4.00, 2.6),
        ("EUR", 2.15, 2.0),
        ("GBP", 4.00, 3.4),
        ("JPY", 0.50, 2.8),
        ("CHF", 0.00, 0.2),
        ("AUD", 3.60, 3.0),
    ):
        view.set_policy_rate(cur, rate, inflation=infl, updated="placeholder")
    return view
