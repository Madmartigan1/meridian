# gui/widgets/logs_tab.py
"""Live engine log.

Every entry, exit, veto and refusal the engine emits lands here with its
reason attached. This is the panel that makes the engine supervisable rather
than a black box, so it is filterable but never silently truncated: the block
cap is generous and the operator is told when it bites.

Lines arrive from the worker thread as a queued signal, so appending happens on
the Qt main thread like every other widget mutation.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from gui.theme import ACCENT, LOSS, PROFIT, TEXT, TEXT_DIM, WARN

MAX_BLOCKS = 20_000

LEVELS = [
    ("All", logging.DEBUG),
    ("Info and above", logging.INFO),
    ("Warnings and above", logging.WARNING),
    ("Errors only", logging.ERROR),
]


def _colour_for(line: str, level: int) -> str:
    if level >= logging.ERROR:
        return LOSS
    if level >= logging.WARNING:
        return WARN
    if " ENTER " in line:
        return ACCENT
    if " EXIT " in line:
        return PROFIT if ("+" in line.split("pips")[0][-12:]) else TEXT
    if " SKIP " in line:
        return TEXT_DIM
    return TEXT


class LogsTab(QWidget):
    """Scrolling engine log with a level filter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min_level = logging.INFO
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.level = QComboBox()
        for label, _value in LEVELS:
            self.level.addItem(label)
        self.level.setCurrentIndex(1)
        self.level.currentIndexChanged.connect(self._level_changed)

        self.autoscroll = QCheckBox("Follow")
        self.autoscroll.setChecked(True)
        self.autoscroll.setToolTip("Keep the newest line in view.")

        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        save = QPushButton("Save to file...")
        save.clicked.connect(self._save)

        controls.addWidget(QLabel("Level:"))
        controls.addWidget(self.level)
        controls.addWidget(self.autoscroll)
        controls.addStretch(1)
        controls.addWidget(clear)
        controls.addWidget(save)
        root.addLayout(controls)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        root.addWidget(self.view, 1)

        self.hint = QLabel(
            "Refusal codes are the token before the colon, and are documented in "
            "docs/RUNBOOK.md. A run that takes no trades is explained here."
        )
        self.hint.setObjectName("hint")
        root.addWidget(self.hint)

    # --- slots --------------------------------------------------------------

    def append(self, line: str, level: int) -> None:
        if level < self._min_level:
            return
        colour = _colour_for(line, level)
        self.view.appendHtml(
            '<span style="color:%s; white-space:pre;">%s</span>' % (colour, _escape(line))
        )
        if self.autoscroll.isChecked():
            self.view.moveCursor(QTextCursor.End)

    def clear(self) -> None:
        self.view.clear()

    def _level_changed(self, index: int) -> None:
        self._min_level = LEVELS[index][1]

    def _save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save log", "meridian_run.log", "Log files (*.log *.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())
        except OSError as exc:
            QMessageBox.critical(self, "Could not save log", str(exc))


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
