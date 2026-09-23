# gui/widgets/setup_tab.py
"""Setup: account, universe, data, and the simulated-state controls.

Everything here writes into the same `EngineConfig` the CLI builds, and the
same `validate()` runs over it. There is deliberately no parallel set of GUI
defaults -- two sources of truth for a risk parameter is how an operator ends
up looking at one number while the engine uses another.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from engine.config import EngineConfig
from engine.instruments import DEFAULT_INSTRUMENTS
from engine.persistence import Store


class SetupTab(QWidget):
    """Account, universe and data configuration."""

    data_generated = Signal(str)
    state_reset = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._symbol_boxes = {}
        self._build()

    # --- construction -------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)

        root.addWidget(self._account_group())
        root.addWidget(self._universe_group())
        root.addWidget(self._data_group())
        root.addWidget(self._state_group())
        root.addStretch(1)

    def _account_group(self) -> QGroupBox:
        box = QGroupBox("Account")
        form = QFormLayout(box)

        self.equity = QDoubleSpinBox()
        self.equity.setRange(100.0, 10_000_000.0)
        self.equity.setDecimals(2)
        self.equity.setSingleStep(1000.0)
        self.equity.setGroupSeparatorShown(True)
        form.addRow("Starting equity", self.equity)

        self.currency = QComboBox()
        self.currency.addItems(["USD", "EUR", "GBP", "JPY", "CHF", "AUD"])
        self.currency.setToolTip(
            "PnL is converted into this currency. A pair whose quote currency "
            "cannot be converted to it will refuse to size rather than guess."
        )
        form.addRow("Account currency", self.currency)

        self.leverage = QDoubleSpinBox()
        self.leverage.setRange(1.0, 50.0)
        self.leverage.setDecimals(0)
        self.leverage.setToolTip(
            "30:1 is the ESMA retail cap on majors. Higher leverage does not "
            "improve expectancy, only the probability of ruin."
        )
        form.addRow("Leverage (x:1)", self.leverage)
        return box

    def _universe_group(self) -> QGroupBox:
        box = QGroupBox("Universe")
        outer = QVBoxLayout(box)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)

        for index, symbol in enumerate(sorted(DEFAULT_INSTRUMENTS)):
            inst = DEFAULT_INSTRUMENTS[symbol]
            check = QCheckBox(symbol)
            check.setToolTip(
                "pip %s | typical spread %.1f pips | swap long %+.2f / short %+.2f pips per day"
                % (inst.pip_size, inst.typical_spread_pips,
                   inst.swap_long_pips_per_day, inst.swap_short_pips_per_day)
            )
            self._symbol_boxes[symbol] = check
            grid.addWidget(check, index // 4, index % 4)
        outer.addLayout(grid)

        hint = QLabel(
            "Spread and swap values shipped with each pair are placeholders. "
            "Replace them with your broker's published contract specs before "
            "drawing any conclusion from a result."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return box

    def _data_group(self) -> QGroupBox:
        box = QGroupBox("Data")
        outer = QVBoxLayout(box)
        form = QFormLayout()

        row = QHBoxLayout()
        self.data_dir = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_dir)
        row.addWidget(self.data_dir, 1)
        row.addWidget(browse)
        form.addRow("Data directory", row)

        self.bar_minutes = QSpinBox()
        self.bar_minutes.setRange(1, 1440)
        self.bar_minutes.setToolTip(
            "Must match the interval of your CSV files. Nothing verifies this "
            "for you, and if it is wrong every time-based rule is wrong with it."
        )
        form.addRow("Bar interval (minutes)", self.bar_minutes)
        outer.addLayout(form)

        row = QHBoxLayout()
        self.gen_bars = QSpinBox()
        self.gen_bars.setRange(200, 200_000)
        self.gen_bars.setValue(2000)
        self.gen_bars.setGroupSeparatorShown(True)
        generate = QPushButton("Generate sample data")
        generate.clicked.connect(self._generate)
        row.addWidget(QLabel("Bars:"))
        row.addWidget(self.gen_bars)
        row.addWidget(generate)
        row.addStretch(1)
        outer.addLayout(row)

        hint = QLabel(
            "Sample data is a deterministic random walk: no fat tails, no session "
            "volatility pattern, no reaction to news. It shows the engine runs. It "
            "says nothing about whether a strategy works."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        return box

    def _state_group(self) -> QGroupBox:
        box = QGroupBox("Simulated state")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        reset = QPushButton("Clear simulated state")
        reset.setObjectName("danger")
        reset.clicked.connect(self._reset_state)
        row.addWidget(reset)
        row.addStretch(1)
        outer.addLayout(row)

        hint = QLabel(
            "Positions, the trade ledger, the account balance and the daily risk "
            "counters persist between runs. Clear them to start from a known "
            "baseline, otherwise a second run continues from wherever the first "
            "one stopped. Live state is never touched by this button."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return box

    # --- actions ------------------------------------------------------------

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select data directory",
                                                  self.data_dir.text() or ".")
        if chosen:
            self.data_dir.setText(chosen)

    def _generate(self) -> None:
        from engine.main import generate_sample_data

        cfg = EngineConfig()
        self.apply_to(cfg)
        if not cfg.symbols:
            QMessageBox.warning(self, "No pairs selected",
                                "Select at least one pair before generating data.")
            return

        # Generating overwrites whatever is in data/ with a random walk. If the
        # operator has real market data there, replacing it silently turns every
        # subsequent result into scaffolding while still looking like a market.
        existing = [Path(p) for p in cfg.resolved_data_files().values() if Path(p).exists()]
        if existing:
            confirm = QMessageBox.warning(
                self, "Overwrite existing data?",
                "This replaces %d file(s) in %s with synthetic random-walk bars:\n\n%s\n\n"
                "If that is real market data, it will be lost and every result "
                "afterwards will be scaffolding rather than a market."
                % (len(existing), cfg.data_dir,
                   "\n".join("  " + p.name for p in existing[:8])),
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                self.status.setText("Generation cancelled. Existing data kept.")
                return
        try:
            written = generate_sample_data(cfg, self.gen_bars.value())
        except OSError as exc:
            QMessageBox.critical(self, "Could not write data", str(exc))
            return
        self.status.setText("Wrote %d file(s): %s"
                            % (len(written), ", ".join(Path(p).name for p in written)))
        self.data_generated.emit(self.data_dir.text())

    def _reset_state(self) -> None:
        confirm = QMessageBox.question(
            self, "Clear simulated state?",
            "This deletes simulated positions, the trade ledger, the account "
            "balance and the daily risk counters.\n\nLive state is not affected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        Store(simulated=True).reset()
        self.status.setText("Simulated state cleared.")
        self.state_reset.emit()

    # --- config binding -----------------------------------------------------

    def load_from(self, cfg: EngineConfig) -> None:
        self.equity.setValue(cfg.starting_equity)
        self.currency.setCurrentText(cfg.account_currency)
        self.leverage.setValue(cfg.risk.leverage)
        self.data_dir.setText(cfg.data_dir)
        self.bar_minutes.setValue(cfg.bar_minutes)
        selected = {s.upper() for s in cfg.symbols}
        for symbol, box in self._symbol_boxes.items():
            box.setChecked(symbol in selected)

    def apply_to(self, cfg: EngineConfig) -> None:
        cfg.starting_equity = self.equity.value()
        cfg.account_currency = self.currency.currentText()
        cfg.risk.leverage = self.leverage.value()
        cfg.data_dir = self.data_dir.text().strip() or "data"
        cfg.bar_minutes = self.bar_minutes.value()
        cfg.symbols = [s for s, box in sorted(self._symbol_boxes.items()) if box.isChecked()]

    def set_locked(self, locked: bool) -> None:
        """Lock inputs the engine samples once at startup while a run is live."""
        for widget in self.findChildren((QDoubleSpinBox, QSpinBox, QComboBox,
                                         QLineEdit, QCheckBox, QPushButton)):
            widget.setEnabled(not locked)
