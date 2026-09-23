# gui/theme.py
"""Dark palette and shared styling.

Colour is used to carry meaning, not decoration: profit and loss share a single
pair of tokens so a green number means the same thing on every panel, and the
warning colour is reserved for the simulation banner and refusal states. Both
PnL colours are chosen to stay distinguishable in greyscale, since red/green is
the least accessible pairing available and this app leans on it constantly.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#14161a"
BG_RAISED = "#1b1e24"
BG_INPUT = "#23272e"
BORDER = "#2e333c"
TEXT = "#e4e7eb"
TEXT_DIM = "#8b929c"
ACCENT = "#4a9eda"
PROFIT = "#3fb96b"
LOSS = "#e0564f"
WARN = "#d9a441"
FLAT = "#8b929c"

MONO = "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace"


def pnl_colour(value: float) -> str:
    """One definition of the profit/loss colours, used by every panel."""
    if value > 0:
        return PROFIT
    if value < 0:
        return LOSS
    return FLAT


STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {BG_RAISED};
    top: -1px;
}}
QTabBar::tab {{
    background: {BG};
    color: {TEXT_DIM};
    padding: 8px 18px;
    border: 1px solid {BORDER};
    border-bottom: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {BG_RAISED};
    color: {TEXT};
    border-top: 2px solid {ACCENT};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {TEXT_DIM};
}}
QPushButton {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 7px 16px;
}}
QPushButton:hover:enabled {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; background: {BG}; }}
QPushButton#primary {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #0d1116;
    font-weight: 600;
}}
QPushButton#danger:enabled {{ border-color: {LOSS}; color: {LOSS}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 5px 7px;
    selection-background-color: {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QCheckBox:disabled {{ color: {TEXT_DIM}; }}
QPlainTextEdit {{
    background: #0f1115;
    border: 1px solid {BORDER};
    font-family: {MONO};
    font-size: 12px;
}}
QTableWidget {{
    background: {BG_RAISED};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    alternate-background-color: #1f232a;
}}
QHeaderView::section {{
    background: {BG_INPUT};
    color: {TEXT_DIM};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px;
    font-weight: 600;
}}
QLabel#banner {{
    background: #3a2f16;
    border: 1px solid {WARN};
    border-radius: 3px;
    color: {WARN};
    padding: 8px 12px;
    font-weight: 600;
}}
QLabel#statValue {{ font-family: {MONO}; font-size: 19px; font-weight: 600; }}
QLabel#statLabel {{ color: {TEXT_DIM}; font-size: 11px; }}
QLabel#hint {{ color: {TEXT_DIM}; font-size: 11px; }}
QScrollBar:vertical {{ background: {BG}; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QStatusBar {{ color: {TEXT_DIM}; border-top: 1px solid {BORDER}; }}
"""


def apply(app: QApplication) -> None:
    """Apply the palette and stylesheet to the whole application."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(BG_INPUT))
    palette.setColor(QPalette.AlternateBase, QColor(BG_RAISED))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(BG_INPUT))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#0d1116"))
    palette.setColor(QPalette.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)


def mono_font(size: int = 12) -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(size)
    return font
