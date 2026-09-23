# gui/widgets/dashboard_tab.py
"""Dashboard: account state, open positions, and why the engine refused things.

The refusal panel is not decoration. When a run produces no trades, the reason
is always in there, and without it the operator is left guessing at a silent
engine. It is given the same visual weight as the PnL.

Every update arrives as a plain dict snapshot from the controller. This widget
never touches an engine object.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.theme import FLAT, LOSS, PROFIT, TEXT, TEXT_DIM, WARN, pnl_colour

POSITION_COLUMNS = ["Pair", "Side", "Lots", "Entry", "Price", "Stop", "Target",
                    "Pips", "Swap", "Bars"]


class StatTile(QWidget):
    """One labelled number. Colour carries sign, never decoration."""

    def __init__(self, label: str, suffix: str = "", parent=None):
        super().__init__(parent)
        self._suffix = suffix
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self.value = QLabel("--")
        self.value.setObjectName("statValue")
        self.caption = QLabel(label.upper())
        self.caption.setObjectName("statLabel")

        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set(self, text: str, colour: str = TEXT) -> None:
        self.value.setText(text + self._suffix)
        self.value.setStyleSheet("color: %s;" % colour)


class DashboardTab(QWidget):
    """Live account state and open positions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._currency = "USD"
        self._build()

    # --- construction -------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        root.addWidget(self._stats_group())

        self.halt_banner = QLabel("")
        self.halt_banner.setObjectName("banner")
        self.halt_banner.setVisible(False)
        self.halt_banner.setWordWrap(True)
        root.addWidget(self.halt_banner)

        root.addWidget(self._positions_group(), 1)

        row = QHBoxLayout()
        row.addWidget(self._refusals_group(), 1)
        row.addWidget(self._exits_group(), 1)
        root.addLayout(row)

    def _stats_group(self) -> QGroupBox:
        box = QGroupBox("Account")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(6)

        self.tiles = {
            "equity": StatTile("Equity"),
            "realised": StatTile("Realised PnL"),
            "drawdown": StatTile("Max drawdown"),
            "trades": StatTile("Closed trades"),
            "win_rate": StatTile("Win rate", "%"),
            "open": StatTile("Open positions"),
            "pips": StatTile("Gross pips"),
            "bars": StatTile("Bars processed"),
        }
        for index, key in enumerate(("equity", "realised", "drawdown", "trades",
                                     "win_rate", "open", "pips", "bars")):
            grid.addWidget(self.tiles[key], index // 4, index % 4)
        return box

    def _positions_group(self) -> QGroupBox:
        box = QGroupBox("Open positions")
        layout = QVBoxLayout(box)

        self.table = QTableWidget(0, len(POSITION_COLUMNS))
        self.table.setHorizontalHeaderLabels(POSITION_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        self.empty_hint = QLabel("Flat. No positions open.")
        self.empty_hint.setObjectName("hint")
        layout.addWidget(self.empty_hint)
        return box

    def _refusals_group(self) -> QGroupBox:
        box = QGroupBox("Why entries were refused")
        layout = QVBoxLayout(box)
        self.refusals = QLabel("--")
        self.refusals.setObjectName("hint")
        self.refusals.setWordWrap(True)
        self.refusals.setAlignment(Qt.AlignTop)
        self.refusals.setToolTip(
            "When a run produces no trades, the explanation is here. Codes are "
            "documented in docs/RUNBOOK.md."
        )
        layout.addWidget(self.refusals)
        layout.addStretch(1)
        return box

    def _exits_group(self) -> QGroupBox:
        box = QGroupBox("How positions closed")
        layout = QVBoxLayout(box)
        self.exits = QLabel("--")
        self.exits.setObjectName("hint")
        self.exits.setWordWrap(True)
        self.exits.setAlignment(Qt.AlignTop)
        self.exits.setToolTip(
            "Mostly stops means the stop sits inside the noise, or the signal has "
            "no edge. Mostly stagnation means entries are going nowhere."
        )
        layout.addWidget(self.exits)
        layout.addStretch(1)
        return box

    # --- updates ------------------------------------------------------------

    def reset(self) -> None:
        for tile in self.tiles.values():
            tile.set("--", TEXT_DIM)
        self.table.setRowCount(0)
        self.empty_hint.setVisible(True)
        self.refusals.setText("--")
        self.exits.setText("--")
        self.halt_banner.setVisible(False)

    def update_from(self, snap: dict) -> None:
        self._currency = snap.get("currency", "USD")

        self.tiles["equity"].set("{:,.2f}".format(snap["equity"]), TEXT)
        self.tiles["realised"].set("{:+,.2f}".format(snap["realised"]),
                                   pnl_colour(snap["realised"]))
        self.tiles["drawdown"].set("{:,.2f}".format(snap["max_drawdown"]),
                                   WARN if snap["max_drawdown"] > 0 else TEXT_DIM)
        closed = snap["wins"] + snap["losses"]
        self.tiles["trades"].set("%d" % closed, TEXT)
        self.tiles["win_rate"].set("%.1f" % snap["win_rate"], TEXT_DIM)
        self.tiles["open"].set("%d" % len(snap["positions"]), TEXT)
        self.tiles["pips"].set("{:+,.1f}".format(snap["gross_pips"]),
                               pnl_colour(snap["gross_pips"]))
        self.tiles["bars"].set("{:,}".format(snap["bars"]), TEXT_DIM)

        if snap.get("halted"):
            self.halt_banner.setText("TRADING HALTED FOR THE DAY - %s"
                                     % snap.get("halt_reason", ""))
            self.halt_banner.setVisible(True)
        else:
            self.halt_banner.setVisible(False)

        self._fill_positions(snap["positions"])
        self.refusals.setText(self._format_counts(snap.get("veto_counts", {}),
                                                  "Nothing refused yet."))
        self.exits.setText(self._format_counts(snap.get("exit_counts", {}),
                                               "Nothing closed yet."))

    def _fill_positions(self, positions: list) -> None:
        self.empty_hint.setVisible(not positions)
        self.table.setRowCount(len(positions))
        for row, pos in enumerate(positions):
            target = pos.get("take_profit")
            cells = [
                (pos["symbol"], TEXT),
                (pos["side"], PROFIT if pos["side"] == "LONG" else LOSS),
                ("%.2f" % pos["lots"], TEXT),
                ("%.5f" % pos["entry"], TEXT_DIM),
                ("%.5f" % pos["price"], TEXT),
                ("%.5f" % pos["stop"], WARN),
                ("%.5f" % target if target else "--", TEXT_DIM),
                ("%+.1f" % pos["pips"], pnl_colour(pos["pips"])),
                ("%+.2f" % pos["swap_pips"], pnl_colour(pos["swap_pips"])),
                ("%d" % pos["bars_held"], TEXT_DIM),
            ]
            for col, (text, colour) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(Qt.NoBrush if colour == TEXT else _brush(colour))
                if col > 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)

    @staticmethod
    def _format_counts(counts: dict, empty: str) -> str:
        if not counts:
            return empty
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
        width = max(len(name) for name, _ in ranked)
        return "\n".join("%-*s  %d" % (width, name, value) for name, value in ranked)


def _brush(colour: str):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(colour))
