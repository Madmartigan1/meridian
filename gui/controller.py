# gui/controller.py
"""Engine lifecycle and the only bridge between the engine and the GUI.

Threading contract, which nothing in `gui/` may break:

  * The engine runs on one worker thread. It never touches a widget.
  * Every widget mutation happens on the Qt main thread.
  * Everything the engine wants to say crosses through here, as a Qt signal.

Cross-thread signal connections are queued by default, so a slot connected to
one of these signals runs on the main thread even though it was emitted from
the worker. That is what makes the contract enforceable rather than aspirational.

Two things the worker must never hand across: a live `Engine`, and a live
`Position`. Both keep mutating after the signal is emitted, so the GUI would be
reading a half-updated object from another thread. Only plain snapshots cross.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine.config import EngineConfig
from engine.engine import Engine
from engine.feed import CsvReplayFeed
from engine.macro import default_macro_view
from engine.persistence import Store

log = logging.getLogger("meridian")

# How often the worker publishes a dashboard snapshot. Emitting on every bar
# floods the event loop on a fast replay and the GUI falls behind the engine.
SNAPSHOT_EVERY_BARS = 20
SNAPSHOT_MIN_INTERVAL = 0.05   # seconds


class QtLogBridge(logging.Handler):
    """Routes engine log records to the GUI as a signal.

    Installed when a run starts and removed when it ends. Leaving it attached
    would double-log every subsequent run, once per handler still bound to the
    `meridian` logger.
    """

    def __init__(self, emit_line):
        super().__init__()
        self._emit_line = emit_line
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit_line(self.format(record), record.levelno)
        except Exception:      # noqa: BLE001 - a logging failure must not kill a run
            pass


def snapshot(engine: Engine, bar) -> dict:
    """Flatten the engine's live state into plain data safe to cross threads."""
    stats = engine.stats
    positions = []
    for pos in engine.book.all():
        inst = engine.instruments.get(pos.symbol)
        price = pos.last_price or pos.entry_price
        positions.append({
            "symbol": pos.symbol,
            "side": pos.side,
            "lots": pos.lots,
            "entry": pos.entry_price,
            "price": price,
            "stop": pos.stop_price,
            "take_profit": pos.take_profit_price,
            "pips": pos.unrealized_pips(inst, price) if inst else 0.0,
            "swap_pips": pos.swap_pips,
            "bars_held": pos.bars_held,
        })
    closed = stats.wins + stats.losses
    return {
        "bars": stats.bars,
        "ts": bar.ts.isoformat(timespec="minutes") if bar is not None else "",
        "symbol": bar.symbol if bar is not None else "",
        "equity": engine.equity,
        "balance": engine.balance,
        "realised": stats.realised,
        "max_drawdown": stats.max_drawdown,
        "entries": stats.entries,
        "exits": stats.exits,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": (100.0 * stats.wins / closed) if closed else 0.0,
        "gross_pips": stats.gross_pips,
        "swap_pips": stats.swap_pips,
        "signals": stats.signals,
        "vetoed": stats.vetoed,
        "blocked": stats.blocked,
        "positions": positions,
        "veto_counts": dict(stats.veto_counts),
        "exit_counts": dict(stats.exit_counts),
        "currency": engine.rates.account_currency,
        "halted": engine.risk.state.halted,
        "halt_reason": engine.risk.state.halt_reason,
    }


class EngineWorker(QObject):
    """Runs one engine pass on the worker thread."""

    progress = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)
    log_line = Signal(str, int)

    def __init__(self, cfg: EngineConfig):
        super().__init__()
        self._cfg = cfg
        self._engine: Optional[Engine] = None
        self._bridge: Optional[QtLogBridge] = None
        self._last_emit = 0.0

    def stop(self) -> None:
        """Ask the engine to finish after the current bar. Called from the GUI thread."""
        if self._engine is not None:
            self._engine.request_stop()

    def run(self) -> None:
        cfg = self._cfg
        self._bridge = QtLogBridge(self.log_line.emit)
        self._bridge.setLevel(logging.DEBUG if cfg.log_every_bar else logging.INFO)
        log.addHandler(self._bridge)
        log.setLevel(logging.DEBUG if cfg.log_every_bar else logging.INFO)

        try:
            files = cfg.resolved_data_files()
            store = Store(simulated=True)
            macro = default_macro_view()
            macro.settings = cfg.macro

            self._engine = Engine(cfg, CsvReplayFeed(files), macro=macro, store=store)
            self._engine.run(observer=self._on_bar)

            final = snapshot(self._engine, None)
            final["stopped_early"] = self._engine._stop_requested
            self.finished.emit(final)
        except Exception as exc:                       # noqa: BLE001
            log.exception("Run failed")
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))
        finally:
            # Always remove the handler, including on failure. A leaked handler
            # makes the next run log every line twice.
            log.removeHandler(self._bridge)
            self._bridge = None

    def _on_bar(self, bar, engine: Engine) -> None:
        """Observer called by the engine after each processed bar."""
        if engine.stats.bars % SNAPSHOT_EVERY_BARS:
            return
        now = time.monotonic()
        if now - self._last_emit < SNAPSHOT_MIN_INTERVAL:
            return
        self._last_emit = now
        self.progress.emit(snapshot(engine, bar))


class EngineController(QObject):
    """What the window talks to. Owns the worker thread and its lifecycle."""

    started = Signal()
    progress = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)
    log_line = Signal(str, int)
    state_changed = Signal(bool)     # True while a run is in progress

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[EngineWorker] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, cfg: EngineConfig) -> None:
        if self.running:
            return

        self._thread = QThread()
        self._worker = EngineWorker(cfg)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.log_line.connect(self.log_line)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

        self._thread.start()
        self.state_changed.emit(True)
        self.started.emit()

    def stop(self) -> None:
        """Cooperative stop. The engine finishes its current bar and shuts down
        properly, so open positions are still settled and state still written."""
        if self._worker is not None:
            self._worker.stop()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop and wait. Called when the window closes."""
        self.stop()
        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(timeout_ms):
                log.warning("Engine thread did not finish within %d ms.", timeout_ms)

    def _on_finished(self, payload: dict) -> None:
        self._teardown()
        self.finished.emit(payload)

    def _on_failed(self, message: str) -> None:
        self._teardown()
        self.failed.emit(message)

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
        self.state_changed.emit(False)
