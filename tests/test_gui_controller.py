"""The GUI threading contract and the engine seam it depends on.

These tests exist because the GUI's failure modes are invisible in a normal
run: a leaked log handler double-logs the *next* run, a live object crossing a
thread boundary corrupts state intermittently, and a stop that does not shut
down properly leaves positions unsettled and the ledger disagreeing with the
book. None of those raise at the time they happen.

Runs offscreen (`QT_QPA_PLATFORM=offscreen`, set below), so it needs no display
and touches no operator state -- `MERIDIAN_STATE_DIR` is pointed at a temporary
directory before the engine is imported.

Run directly:

    python tests/test_gui_controller.py
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both must be set before the engine or Qt are imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_STATE = tempfile.mkdtemp(prefix="meridian_gui_test_")
os.environ["MERIDIAN_STATE_DIR"] = _STATE

from _runner import close, run  # noqa: E402

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import logging  # noqa: E402

from engine.config import EngineConfig, validate  # noqa: E402
from engine.engine import Engine  # noqa: E402
from engine.feed import CsvReplayFeed, synthetic_bars, write_csv  # noqa: E402
from engine.instruments import get_instrument  # noqa: E402
from engine.persistence import Store  # noqa: E402
from gui.controller import EngineController, snapshot  # noqa: E402

_app = QApplication.instance() or QApplication([])


def make_config(bars=400):
    """A tiny self-contained run over freshly generated data."""
    from datetime import datetime, timedelta, timezone

    cfg = EngineConfig()
    cfg.symbols = ["EUR/USD"]
    cfg.data_dir = os.path.join(_STATE, "data")
    cfg.simulated = True
    validate(cfg)

    inst = get_instrument("EUR/USD")
    start = datetime(2026, 1, 5, tzinfo=timezone.utc) - timedelta(hours=bars)
    series = synthetic_bars("EUR/USD", start, bars, 1.0850, minutes=60,
                            drift_pips_per_bar=0.4, vol_pips=6.0,
                            pip_size=inst.pip_size, seed=5)
    write_csv(os.path.join(cfg.data_dir, "EURUSD.csv"), series)
    return cfg


def wait_for(controller, timeout_ms=30_000):
    """Spin the event loop until the run ends. Returns the final payload."""
    loop = QEventLoop()
    captured = {}

    def done(payload):
        captured["result"] = payload
        loop.quit()

    def failed(message):
        captured["error"] = message
        loop.quit()

    controller.finished.connect(done)
    controller.failed.connect(failed)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)

    loop.exec()
    guard.stop()

    if "error" in captured:
        raise AssertionError("run failed: %s" % captured["error"])
    assert "result" in captured, "run did not finish within %d ms" % timeout_ms
    return captured["result"]


def test_controller_runs_to_completion_and_reports():
    """The happy path: progress arrives, the run finishes, numbers are coherent."""
    Store(simulated=True).reset()
    cfg = make_config()
    controller = EngineController()

    seen = []
    controller.progress.connect(seen.append)
    controller.start(cfg)
    final = wait_for(controller)

    assert final["bars"] > 0, final
    assert seen, "no progress snapshots were emitted during the run"
    assert final["bars"] >= seen[-1]["bars"], "the final snapshot must not go backwards"
    assert not controller.running, "the worker thread must be torn down"


def test_snapshots_carry_plain_data_only():
    """Nothing that keeps mutating may cross the thread boundary.

    A live Engine or Position handed to the GUI would be read from the main
    thread while the worker is still writing to it. Only primitives, lists and
    dicts are allowed across.
    """
    Store(simulated=True).reset()
    cfg = make_config(bars=200)
    store = Store(simulated=True)
    engine = Engine(cfg, CsvReplayFeed(cfg.resolved_data_files()), store=store)

    logging.disable(logging.CRITICAL)
    try:
        engine.run()
    finally:
        logging.disable(logging.NOTSET)

    snap = snapshot(engine, None)
    allowed = (str, int, float, bool, type(None))

    def check(value, path="snapshot"):
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str), "%s has a non-string key %r" % (path, key)
                check(item, "%s[%r]" % (path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check(item, "%s[%d]" % (path, index))
        else:
            assert isinstance(value, allowed), \
                "%s is %s, which is not safe to cross threads" % (path, type(value).__name__)

    check(snap)


def test_log_handler_is_removed_after_a_run():
    """A leaked handler makes the next run log every line twice.

    Regression guard: the handler is installed on the shared `meridian` logger
    at start, so it must come off in a finally block, including after a failure.
    """
    log = logging.getLogger("meridian")
    before = list(log.handlers)

    Store(simulated=True).reset()
    controller = EngineController()
    controller.start(make_config(bars=200))
    wait_for(controller)

    after = list(log.handlers)
    assert len(after) == len(before), \
        "handler count went %d -> %d; a log bridge was leaked" % (len(before), len(after))


def test_a_failed_run_reports_instead_of_hanging():
    """A missing data file must surface as `failed`, not a silent stall."""
    cfg = make_config(bars=200)
    cfg.data_dir = os.path.join(_STATE, "does_not_exist")

    controller = EngineController()
    errors = []
    controller.failed.connect(errors.append)
    controller.start(cfg)

    loop = QEventLoop()
    controller.failed.connect(loop.quit)
    controller.finished.connect(loop.quit)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(15_000)
    loop.exec()

    assert errors, "a missing data directory must emit `failed`"
    assert not controller.running, "the thread must be torn down after a failure"

    # And the handler still came off, because it is removed in a finally block.
    log = logging.getLogger("meridian")
    assert not any(type(h).__name__ == "QtLogBridge" for h in log.handlers), \
        "the log bridge leaked after a failed run"


def test_stop_still_settles_positions_and_writes_state():
    """A cooperative stop must run the same shutdown as a full run.

    If stopping skipped `_flatten_at_end`, an open position would be left
    unsettled: its exit spread unpaid, and the ledger disagreeing with the book.
    """
    Store(simulated=True).reset()
    cfg = make_config(bars=1200)
    store = Store(simulated=True)
    engine = Engine(cfg, CsvReplayFeed(cfg.resolved_data_files()), store=store)

    def observer(bar, eng):
        if eng.stats.bars >= 300:
            eng.request_stop()

    logging.disable(logging.CRITICAL)
    try:
        stats = engine.run(observer=observer)
    finally:
        logging.disable(logging.NOTSET)

    assert stats.bars < 1200, "the stop request was ignored"
    assert len(engine.book) == 0, "open positions must be settled by the shutdown"
    assert stats.entries == stats.exits, \
        "%d entries but %d exits: something was left open" % (stats.entries, stats.exits)


def test_config_survives_a_round_trip_through_the_widgets():
    """What the panels show must be what the engine receives.

    Two sources of truth for a risk parameter is how an operator ends up
    watching one number while the engine uses another.
    """
    from gui.widgets.config_tab import ConfigTab
    from gui.widgets.setup_tab import SetupTab

    setup, config = SetupTab(), ConfigTab()

    source = EngineConfig()
    source.symbols = ["EUR/USD", "USD/JPY"]
    source.starting_equity = 25_000.0
    source.risk.risk_per_trade_pct = 0.0075
    source.risk.leverage = 20.0
    source.exits.atr_stop_mult = 2.4
    source.advisors.enable_macd = False
    source.execution.spread_multiplier = 2.0
    validate(source)

    setup.load_from(source)
    config.load_from(source)

    result = EngineConfig()
    setup.apply_to(result)
    config.apply_to(result)

    assert result.symbols == source.symbols, (result.symbols, source.symbols)
    assert close(result.starting_equity, 25_000.0)
    assert close(result.risk.risk_per_trade_pct, 0.0075, tol=1e-9), \
        "percent knobs must survive the fraction/percent conversion"
    assert close(result.risk.leverage, 20.0)
    assert close(result.exits.atr_stop_mult, 2.4, tol=1e-9)
    assert result.advisors.enable_macd is False
    assert close(result.execution.spread_multiplier, 2.0)


def test_generating_data_asks_before_overwriting():
    """Generating must not silently replace real market data with a random walk.

    Regression: it overwrote whatever was in the data directory with synthetic
    bars and said nothing. A run afterwards still looks like a market and still
    produces confident numbers, so the substitution is invisible in the result
    -- it is only detectable by noticing the bar count changed.

    QMessageBox is stubbed, since a modal would block forever with no user.
    """
    from gui.widgets import setup_tab as module
    from gui.widgets.setup_tab import SetupTab

    tab = SetupTab()
    cfg = make_config(bars=50)          # writes a real CSV into cfg.data_dir
    tab.load_from(cfg)
    target = pathlib.Path(cfg.resolved_data_files()["EUR/USD"])
    original = target.read_bytes()
    assert original, "fixture data missing"

    class Stub:
        Yes, Cancel, No, Warning = 1, 2, 3, 4
        asked = []

        @classmethod
        def warning(cls, *args, **kwargs):
            cls.asked.append(args[1] if len(args) > 1 else "")
            return cls.Cancel          # the operator declines

        @classmethod
        def critical(cls, *args, **kwargs):
            return cls.Yes

    real = module.QMessageBox
    module.QMessageBox = Stub
    try:
        tab._generate()
        assert Stub.asked, "generating over existing data must prompt first"
        assert target.read_bytes() == original,             "declining the prompt must leave the existing data untouched"
    finally:
        module.QMessageBox = real


def test_gui_ranges_are_not_stricter_than_the_engine():
    """A spinbox floor above what validate() accepts hides a legitimate setting.

    Regression: the spread multiplier bottomed out at 0.10 while the engine
    accepts any positive value, which put the near-zero-cost run -- the one
    that separates a strategy's raw edge from the spread it pays -- out of
    reach from the GUI entirely. Nothing errors; the control simply refuses to
    go where the operator needs it.

    Knobs that validate() adjusts against a *partner* knob are excluded, since
    testing them in isolation says nothing.
    """
    import logging

    from engine.config import EngineConfig, validate
    from gui.widgets.config_tab import SPEC, _get, _set

    paired = {"exits.min_stop_pips", "exits.max_stop_pips", "fast_ema", "slow_ema",
              "macd_fast", "macd_slow", "risk.risk_per_trade_pct",
              "risk.max_total_risk_pct", "execution.slippage_pips",
              "execution.stop_slippage_pips"}

    offenders = []
    logging.disable(logging.CRITICAL)
    try:
        for _group, rows in SPEC:
            for path, _label, kind, spec, _tip in rows:
                if kind != "float" or path in paired:
                    continue
                lo, hi, _step, _dec, scale = spec
                for bound in (lo, hi):
                    cfg = EngineConfig()
                    _set(cfg, path, bound / scale)
                    validate(cfg)
                    if abs(_get(cfg, path) - bound / scale) > 1e-9:
                        offenders.append("%s at %.4f -> %.4f" % (path, bound / scale,
                                                                 _get(cfg, path)))
    finally:
        logging.disable(logging.NOTSET)

    assert not offenders, "GUI offers values the engine rejects: %s" % "; ".join(offenders)


def test_spread_multiplier_reaches_the_near_zero_cost_run():
    """The specific experiment that must stay reachable from the GUI."""
    from gui.widgets.config_tab import SPEC

    for _group, rows in SPEC:
        for path, _label, _kind, spec, _tip in rows:
            if path == "execution.spread_multiplier":
                assert spec[0] <= 0.01,                     "spread multiplier floor is %.2f; cannot show costs-off" % spec[0]
                return
    raise AssertionError("spread multiplier knob missing from the config tab")


def test_no_control_exists_for_disabling_the_stop():
    """The one rule that must not become configurable through a back door."""
    from gui.widgets.config_tab import SPEC

    paths = [path for _group, rows in SPEC for (path, *_rest) in rows]
    for path in paths:
        leaf = path.split(".")[-1].lower()
        assert "enable_stop" not in leaf, "found a stop toggle at %s" % path
        assert not (leaf.startswith("no_stop") or leaf == "use_stop"), path


if __name__ == "__main__":
    raise SystemExit(run(globals()))
