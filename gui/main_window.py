# gui/main_window.py
"""Main window: tabs, run controls, and the simulation disclaimer.

The banner is not boilerplate. This app models leveraged FX, where the
documented retail outcome is that most accounts lose money, and where a
simulated equity curve is the easiest thing in the world to mistake for
evidence. So the window states what it is at all times, not only in a README
nobody has open while they are reading a number.

All engine communication arrives through `gui/controller.py` as queued signals,
so every slot here already runs on the Qt main thread.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from engine.config import EngineConfig, validate
from engine.constants import MERIDIAN_VERSION
from gui.controller import EngineController
from gui.widgets.config_tab import ConfigTab
from gui.widgets.dashboard_tab import DashboardTab
from gui.widgets.logs_tab import LogsTab
from gui.widgets.setup_tab import SetupTab

log = logging.getLogger("meridian")

BANNER = ("SIMULATION ONLY  —  no broker is connected and no order is ever placed. "
          "Results depend entirely on the cost assumptions you set. "
          "Leveraged FX loses money for most retail traders.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Meridian %s  —  FX decision engine (simulated)" % MERIDIAN_VERSION)
        self.resize(1180, 820)

        self.controller = EngineController(self)
        self._build()
        self._wire()

        self._cfg = EngineConfig()
        self.setup_tab.load_from(self._cfg)
        self.config_tab.load_from(self._cfg)
        self.statusBar().showMessage("Idle. Configure a run, then press Start.")

    # --- construction -------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        banner = QLabel(BANNER)
        banner.setObjectName("banner")
        banner.setWordWrap(True)
        root.addWidget(banner)

        root.addLayout(self._controls())

        self.tabs = QTabWidget()
        self.dashboard_tab = DashboardTab()
        self.setup_tab = SetupTab()
        self.config_tab = ConfigTab()
        self.logs_tab = LogsTab()

        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.setup_tab, "Setup")
        self.tabs.addTab(self.config_tab, "Strategy && Risk")
        self.tabs.addTab(self.logs_tab, "Logs")
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self._menus()

    def _controls(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.start_button = QPushButton("Start run")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumWidth(130)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Finishes the current bar, settles open positions and writes state. "
            "Never cuts a run off mid-bar."
        )

        self.run_state = QLabel("Idle")
        self.run_state.setObjectName("hint")

        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addSpacing(16)
        row.addWidget(self.run_state)
        row.addStretch(1)
        return row

    def _menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        load = QAction("&Load config...", self)
        load.setShortcut(QKeySequence.Open)
        load.triggered.connect(self._load_config)
        file_menu.addAction(load)

        save = QAction("&Save config...", self)
        save.setShortcut(QKeySequence.Save)
        save.triggered.connect(self._save_config)
        file_menu.addAction(save)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About Meridian", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _wire(self) -> None:
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)

        self.controller.progress.connect(self.dashboard_tab.update_from)
        self.controller.log_line.connect(self.logs_tab.append)
        self.controller.finished.connect(self._on_finished)
        self.controller.failed.connect(self._on_failed)
        self.controller.state_changed.connect(self._on_state_changed)

        self.setup_tab.state_reset.connect(self.dashboard_tab.reset)

    # --- run lifecycle ------------------------------------------------------

    def _collect_config(self) -> EngineConfig:
        cfg = EngineConfig()
        self.setup_tab.apply_to(cfg)
        self.config_tab.apply_to(cfg)
        cfg.simulated = True
        # Show the operator whatever validation changed, rather than leaving the
        # panel displaying a value the engine has already overridden.
        validate(cfg)
        self.setup_tab.load_from(cfg)
        self.config_tab.refresh_after_validation(cfg)
        return cfg

    def _start(self) -> None:
        if self.controller.running:
            return

        cfg = self._collect_config()
        if not cfg.symbols:
            QMessageBox.warning(self, "No pairs selected",
                                "Select at least one pair on the Setup tab.")
            return

        missing = [p for p in cfg.resolved_data_files().values() if not Path(p).exists()]
        if missing:
            QMessageBox.warning(
                self, "Missing data",
                "No data for:\n\n%s\n\nGenerate sample data on the Setup tab, or "
                "point the data directory at your own OHLC CSVs."
                % "\n".join(Path(p).name for p in missing),
            )
            return

        self._cfg = cfg
        self.dashboard_tab.reset()
        self.tabs.setCurrentWidget(self.dashboard_tab)
        self.controller.start(cfg)

    def _stop(self) -> None:
        self.stop_button.setEnabled(False)
        self.run_state.setText("Stopping after the current bar...")
        self.controller.stop()

    def _on_state_changed(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # Anything the engine samples once at startup is locked while it runs,
        # so nobody can change a number that quietly does nothing.
        self.setup_tab.set_locked(running)
        self.config_tab.set_locked(running)
        self.run_state.setText("Running" if running else "Idle")
        if running:
            self.statusBar().showMessage("Run in progress.")

    def _on_finished(self, snap: dict) -> None:
        self.dashboard_tab.update_from(snap)
        closed = snap["wins"] + snap["losses"]
        stopped = " (stopped early)" if snap.get("stopped_early") else ""
        self.run_state.setText("Finished%s" % stopped)
        self.statusBar().showMessage(
            "%d bars, %d closed trades, net %+.2f %s, max drawdown %.2f%s"
            % (snap["bars"], closed, snap["realised"], snap["currency"],
               snap["max_drawdown"], stopped)
        )
        if closed == 0:
            QMessageBox.information(
                self, "No trades",
                "The run produced no closed trades.\n\nThe Dashboard's refusal "
                "panel names which gate refused entries, and the Logs tab has "
                "the detail. Codes are documented in docs/RUNBOOK.md.",
            )

    def _on_failed(self, message: str) -> None:
        self.run_state.setText("Failed")
        self.statusBar().showMessage("Run failed.")
        QMessageBox.critical(self, "Run failed",
                             "%s\n\nSee the Logs tab for the traceback." % message)

    # --- config files -------------------------------------------------------

    def _load_config(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Load config", "", "JSON (*.json)")
        if not path:
            return
        try:
            cfg = EngineConfig.load(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not load config", str(exc))
            return
        self._cfg = cfg
        self.setup_tab.load_from(cfg)
        self.config_tab.load_from(cfg)
        self.statusBar().showMessage("Loaded %s" % Path(path).name)

    def _save_config(self) -> None:
        path, _f = QFileDialog.getSaveFileName(self, "Save config", "meridian_config.json",
                                               "JSON (*.json)")
        if not path:
            return
        cfg = self._collect_config()
        try:
            cfg.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save config", str(exc))
            return
        self.statusBar().showMessage("Saved %s" % Path(path).name)

    def _about(self) -> None:
        QMessageBox.information(
            self, "About Meridian",
            "Meridian %s\n\n"
            "A simulation-only FX decision engine. It models spread, slippage, "
            "financing, the 24x5 calendar and leverage so that a strategy's "
            "behaviour can be inspected honestly.\n\n"
            "There is no broker integration and no code that places an order.\n\n"
            "A simulated result is a behaviour check against assumptions you set, "
            "not an estimate of profit. See docs/FX_PRIMER.md and "
            "RISK_DISCLOSURE.txt." % MERIDIAN_VERSION,
        )

    # --- shutdown -----------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.controller.running:
            answer = QMessageBox.question(
                self, "Stop the run?",
                "A run is in progress. Closing will stop it after the current bar.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.controller.shutdown()
        event.accept()
