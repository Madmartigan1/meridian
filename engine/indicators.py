# engine/indicators.py
"""Streaming indicators.

Each indicator consumes one closed bar at a time and keeps O(1) state, so the
engine can run on a live feed and on a CSV replay through identical code. Every
indicator exposes `ready` and `warmup_left` so the engine can report honestly
why it is not trading yet rather than emitting signals from half-formed state.
"""
from __future__ import annotations

from typing import Optional, Tuple


class EMA:
    """Exponential moving average, seeded with a simple average of the first N."""

    def __init__(self, period: int):
        if period <= 0:
            raise ValueError("EMA period must be positive")
        self.period = int(period)
        self.alpha = 2.0 / (self.period + 1.0)
        self.value: Optional[float] = None
        self._seed_sum = 0.0
        self._seen = 0

    @property
    def ready(self) -> bool:
        return self.value is not None

    @property
    def warmup_left(self) -> int:
        return max(0, self.period - self._seen)

    def reset(self) -> None:
        self.value = None
        self._seed_sum = 0.0
        self._seen = 0

    def update(self, price: float) -> Optional[float]:
        p = float(price)
        if self.value is None:
            self._seed_sum += p
            self._seen += 1
            if self._seen >= self.period:
                self.value = self._seed_sum / self.period
            return self.value
        self.value += self.alpha * (p - self.value)
        self._seen += 1
        return self.value


class RSI:
    """Wilder's RSI over closes."""

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("RSI period must be positive")
        self.period = int(period)
        self.value: Optional[float] = None
        self._prev: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._deltas = 0

    @property
    def ready(self) -> bool:
        return self.value is not None

    @property
    def warmup_left(self) -> int:
        return max(0, self.period - self._deltas)

    def reset(self) -> None:
        self.value = None
        self._prev = None
        self._avg_gain = None
        self._avg_loss = None
        self._deltas = 0

    def update(self, price: float) -> Optional[float]:
        p = float(price)
        if self._prev is None:
            self._prev = p
            return None
        change = p - self._prev
        self._prev = p
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        if self._deltas < self.period:
            self._avg_gain = gain if self._avg_gain is None else self._avg_gain + gain
            self._avg_loss = loss if self._avg_loss is None else self._avg_loss + loss
            self._deltas += 1
            if self._deltas == self.period:
                self._avg_gain /= self.period
                self._avg_loss /= self.period
                self.value = self._rsi(self._avg_gain, self._avg_loss)
            return self.value

        n = self.period
        self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
        self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        self._deltas += 1
        self.value = self._rsi(self._avg_gain, self._avg_loss)
        return self.value

    @staticmethod
    def _rsi(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0.0 and avg_gain == 0.0:
            return 50.0
        if avg_loss == 0.0:
            return 100.0
        if avg_gain == 0.0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


class MACD:
    """MACD line, signal line and histogram."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        if fast >= slow:
            raise ValueError("MACD fast period must be shorter than slow")
        self.fast_ema = EMA(fast)
        self.slow_ema = EMA(slow)
        self.signal_ema = EMA(signal)
        self.macd: Optional[float] = None
        self.signal: Optional[float] = None
        self.hist: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.hist is not None

    @property
    def warmup_left(self) -> int:
        return max(self.slow_ema.warmup_left, self.signal_ema.warmup_left)

    def reset(self) -> None:
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.signal_ema.reset()
        self.macd = self.signal = self.hist = None

    def update(self, price: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        f = self.fast_ema.update(price)
        s = self.slow_ema.update(price)
        if f is None or s is None:
            return None, None, None
        self.macd = f - s
        self.signal = self.signal_ema.update(self.macd)
        self.hist = None if self.signal is None else self.macd - self.signal
        return self.macd, self.signal, self.hist


class ATR:
    """Wilder's average true range, in price units.

    ATR is how stop distance is set: a fixed pip stop is either too tight for a
    volatile session or too wide for a quiet one, and stop distance is the
    input that decides position size.
    """

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("ATR period must be positive")
        self.period = int(period)
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._sum = 0.0
        self._seen = 0

    @property
    def ready(self) -> bool:
        return self.value is not None

    @property
    def warmup_left(self) -> int:
        return max(0, self.period - self._seen)

    def reset(self) -> None:
        self.value = None
        self._prev_close = None
        self._sum = 0.0
        self._seen = 0

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        h, l, c = float(high), float(low), float(close)
        if self._prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._prev_close = c

        if self._seen < self.period:
            self._sum += tr
            self._seen += 1
            if self._seen == self.period:
                self.value = self._sum / self.period
            return self.value

        self.value = (self.value * (self.period - 1) + tr) / self.period
        self._seen += 1
        return self.value


class IndicatorSet:
    """The per-symbol indicator bundle the engine carries."""

    def __init__(
        self,
        fast_ema: int = 20,
        slow_ema: int = 60,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        atr_period: int = 14,
    ):
        self.fast = EMA(fast_ema)
        self.slow = EMA(slow_ema)
        self.rsi = RSI(rsi_period)
        self.macd = MACD(macd_fast, macd_slow, macd_signal)
        self.atr = ATR(atr_period)
        self.prev_spread: Optional[float] = None   # fast - slow on the prior bar
        self.spread: Optional[float] = None
        self.bars = 0

    @property
    def ready(self) -> bool:
        return (
            self.fast.ready and self.slow.ready and self.rsi.ready
            and self.macd.ready and self.atr.ready and self.prev_spread is not None
        )

    @property
    def warmup_left(self) -> int:
        return max(
            self.fast.warmup_left, self.slow.warmup_left, self.rsi.warmup_left,
            self.macd.warmup_left, self.atr.warmup_left,
        )

    def update(self, high: float, low: float, close: float) -> None:
        """Consume exactly one closed bar."""
        self.fast.update(close)
        self.slow.update(close)
        self.rsi.update(close)
        self.macd.update(close)
        self.atr.update(high, low, close)
        self.bars += 1
        if self.fast.ready and self.slow.ready:
            self.prev_spread = self.spread
            self.spread = self.fast.value - self.slow.value
