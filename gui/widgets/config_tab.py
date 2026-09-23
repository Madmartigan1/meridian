# gui/widgets/config_tab.py
"""Strategy, risk, exit, cost and macro parameters.

Every control is declared once in `SPEC` and bound by attribute path to the
same `EngineConfig` the CLI builds. Adding a knob means adding a row, not
writing another load/apply pair that can drift out of step with the engine.

Percentage knobs are stored as fractions on the config and shown as percents
here; the `scale` column is the single place that conversion happens.

There is no control for disabling the stop, because no such setting exists.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from engine.config import EngineConfig, validate

# (attribute path, label, kind, spec, tooltip)
#   kind "int"   -> spec (min, max, step)
#   kind "float" -> spec (min, max, step, decimals, scale)
#   kind "bool"  -> spec None
SPEC = [
    ("Signal", [
        ("fast_ema", "Fast EMA (bars)", "int", (2, 500, 1),
         "Shorter moving average. On H1 bars, 20 is about a trading day."),
        ("slow_ema", "Slow EMA (bars)", "int", (3, 1000, 1),
         "Longer moving average. Must exceed the fast EMA or validation resets both."),
        ("signal.confirm_bars", "Confirm bars", "int", (1, 20, 1),
         "How many consecutive closed bars the separation must hold before entry. "
         "Raising this cuts whipsaw and entry count together."),
        ("signal.deadband_pips", "Deadband (pips)", "float", (0.0, 50.0, 0.1, 1, 1.0),
         "Minimum EMA separation before a crossover counts. Set at or above your "
         "typical spread; below it the engine fires on noise it cannot profit from."),
    ]),
    ("Advisor gates (veto only)", [
        ("advisors.enable_rsi", "RSI gate", "bool", None,
         "Blocks entries into an already exhausted move."),
        ("advisors.rsi_long_max", "RSI long max", "float", (50.0, 100.0, 1.0, 0, 1.0),
         "Refuse longs above this RSI. Raise toward 80 to stop vetoing strong trends."),
        ("advisors.rsi_short_min", "RSI short min", "float", (0.0, 50.0, 1.0, 0, 1.0),
         "Refuse shorts below this RSI."),
        ("advisors.enable_macd", "MACD gate", "bool", None,
         "Requires the histogram to agree with the EMA direction. Usually the most "
         "active gate; check the refusal counts in the dashboard."),
        ("advisors.enable_atr_floor", "ATR floor", "bool", None,
         "Refuse to trade a range too small to clear costs."),
        ("advisors.min_atr_pips", "Min ATR (pips)", "float", (0.0, 100.0, 0.5, 1, 1.0),
         "Volatility below which no entry is taken."),
    ]),
    ("Risk", [
        ("risk.risk_per_trade_pct", "Risk per trade (%)", "float", (0.01, 2.0, 0.05, 2, 100.0),
         "The amount lost when a stop is hit. Position size is derived from this and "
         "the stop distance, so this is the knob that actually sets size. Hard "
         "ceiling is 2%."),
        ("risk.max_open_positions", "Max open positions", "int", (1, 20, 1), ""),
        ("risk.max_total_risk_pct", "Max total open risk (%)", "float", (0.1, 6.0, 0.1, 2, 100.0),
         "What every open stop hitting at once would cost. Hard ceiling is 6%."),
        ("risk.max_currency_exposure_pct", "Max currency exposure (%)", "float",
         (10.0, 5000.0, 25.0, 0, 100.0),
         "Per-currency notional as a percent of equity. This is a CONCENTRATION "
         "limit, not a leverage limit: one leveraged position is normally 300-400% "
         "of equity in notional on its own, so values near 100% refuse everything."),
        ("risk.max_daily_loss_pct", "Daily loss halt (%)", "float", (0.5, 20.0, 0.5, 2, 100.0),
         "New entries stop for the New York day past this realised loss. Open "
         "positions still manage their own exits."),
        ("risk.max_margin_utilisation", "Max margin use (%)", "float", (1.0, 100.0, 5.0, 0, 100.0),
         "Share of equity that may be committed to margin."),
        ("risk.require_liquid_session", "London/New York hours only", "bool", None,
         "Outside these hours spreads widen beyond the assumption in the instrument "
         "table, so the cost model understates reality."),
    ]),
    ("Exits (the stop is mandatory)", [
        ("exits.atr_stop_mult", "ATR stop multiple", "float", (0.1, 10.0, 0.1, 2, 1.0),
         "Stop distance = ATR x this. Because size follows from stop distance, this "
         "knob sets position size as much as the risk percent does."),
        ("exits.min_stop_pips", "Min stop (pips)", "float", (1.0, 200.0, 1.0, 1, 1.0),
         "Floor. A stop inside the spread is a fee, not a stop."),
        ("exits.max_stop_pips", "Max stop (pips)", "float", (2.0, 500.0, 5.0, 1, 1.0),
         "Ceiling. Caps how small a single trade can be sized."),
        ("exits.enable_take_profit", "Take profit", "bool", None, ""),
        ("exits.reward_risk", "Reward : risk", "float", (0.1, 20.0, 0.1, 2, 1.0),
         "Take profit at this multiple of the stop distance."),
        ("exits.enable_trailing", "Trailing stop", "bool", None,
         "Only ever tightens. R is fixed at entry, so the trail keeps a constant "
         "distance behind the peak."),
        ("exits.trail_start_r", "Trail starts at (R)", "float", (0.1, 10.0, 0.1, 2, 1.0), ""),
        ("exits.trail_distance_r", "Trail distance (R)", "float", (0.1, 10.0, 0.1, 2, 1.0),
         "How far behind the peak the stop sits. Tight trails turn winners into scratches."),
        ("exits.enable_stagnation", "Stagnation exit", "bool", None, ""),
        ("exits.stagnation_bars", "Stagnation bars", "int", (2, 2000, 1), ""),
        ("exits.stagnation_pips", "Stagnation threshold (pips)", "float", (0.1, 100.0, 0.5, 1, 1.0),
         "Progress below which the position is treated as dead."),
        ("exits.enable_max_hold", "Max hold", "bool", None, ""),
        ("exits.max_hold_hours", "Max hold (market hours)", "float", (1.0, 720.0, 6.0, 0, 1.0),
         "Counted in MARKET hours, so a weekend does not age a position."),
        ("exits.close_before_weekend", "Flat into the weekend", "bool", None,
         "Price gaps over the weekend and a stop cannot protect against a gap. "
         "Disabling this accepts that risk."),
    ]),
    ("Costs", [
        # Minimum tracks what validate() actually accepts (any positive value),
        # not a rounder number. A GUI floor stricter than the engine's silently
        # puts a legitimate experiment out of reach -- here, the near-zero-cost
        # run that shows how much of a result is signal and how much is spread.
        ("execution.spread_multiplier", "Spread multiplier", "float", (0.01, 10.0, 0.1, 2, 1.0),
         "Scale the spread assumption. Run at 2.0 as a stress test: a result that "
         "inverts under doubled costs was living inside its cost assumption. "
         "Set it near 0.01, with both slippage knobs at 0, to see the strategy's "
         "raw edge before costs."),
        ("execution.slippage_pips", "Slippage (pips)", "float", (0.0, 20.0, 0.1, 2, 1.0),
         "Always adverse."),
        ("execution.stop_slippage_pips", "Stop slippage (pips)", "float", (0.0, 50.0, 0.1, 2, 1.0),
         "Larger than normal slippage, because stops fill in the worst liquidity. "
         "Validation refuses to let this sit below the normal figure."),
        ("execution.commission_per_lot", "Commission per lot", "float", (0.0, 200.0, 0.5, 2, 1.0),
         "Charged per side. Zero on spread-only accounts."),
        ("execution.max_spread_pips", "Max spread (pips)", "float", (0.1, 50.0, 0.5, 1, 1.0),
         "Refuse to open when the spread is blown out. This is the guard that "
         "matters around data releases."),
    ]),
    ("Macro", [
        ("macro.enabled", "Macro layer", "bool", None,
         "Carry bias and the release blackout."),
        ("macro.use_carry_bias", "Carry bias", "bool", None,
         "Rate differential, inflation-adjusted."),
        ("macro.carry_min_differential", "Carry threshold (pts)", "float", (0.0, 10.0, 0.25, 2, 1.0),
         "Percentage points of differential before it counts as a bias."),
        ("macro.carry_veto_opposing", "Veto against strong carry", "bool", None,
         "Off by default: carry crashes, and a technical signal against it is often "
         "the early warning rather than a mistake."),
        ("macro.use_event_gate", "Release blackout", "bool", None,
         "No new entries in the window around a high-impact release, for either "
         "currency in the pair."),
        ("macro.block_before_minutes", "Blackout before (min)", "int", (0, 240, 5), ""),
        ("macro.block_after_minutes", "Blackout after (min)", "int", (0, 240, 5), ""),
    ]),
]


def _get(obj, path: str):
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set(obj, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


class ConfigTab(QWidget):
    """Strategy and risk knobs, bound to EngineConfig by attribute path."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets = {}      # path -> (widget, kind, scale)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(12)

        for title, rows in SPEC:
            group = QGroupBox(title)
            form = QFormLayout(group)
            for path, label, kind, spec, tooltip in rows:
                widget = self._make(kind, spec)
                if tooltip:
                    widget.setToolTip(tooltip)
                self._widgets[path] = (widget, kind, (spec[4] if kind == "float" else 1.0))
                form.addRow(label, widget)
            layout.addWidget(group)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        row = QHBoxLayout()
        note = QLabel(
            "Values outside a safe range are clamped when the run starts, with a "
            "warning in the log. Hard ceilings on risk, leverage and the stop floor "
            "cannot be raised here."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        restore = QPushButton("Restore defaults")
        restore.clicked.connect(lambda: self.load_from(EngineConfig()))
        row.addWidget(note, 1)
        row.addWidget(restore)
        outer.addLayout(row)

    @staticmethod
    def _make(kind: str, spec):
        if kind == "bool":
            return QCheckBox()
        if kind == "int":
            widget = QSpinBox()
            widget.setRange(spec[0], spec[1])
            widget.setSingleStep(spec[2])
            return widget
        widget = QDoubleSpinBox()
        widget.setRange(spec[0], spec[1])
        widget.setSingleStep(spec[2])
        widget.setDecimals(spec[3])
        return widget

    # --- config binding -----------------------------------------------------

    def load_from(self, cfg: EngineConfig) -> None:
        for path, (widget, kind, scale) in self._widgets.items():
            value = _get(cfg, path)
            if kind == "bool":
                widget.setChecked(bool(value))
            elif kind == "int":
                widget.setValue(int(value))
            else:
                widget.setValue(float(value) * scale)

    def apply_to(self, cfg: EngineConfig) -> None:
        for path, (widget, kind, scale) in self._widgets.items():
            if kind == "bool":
                _set(cfg, path, widget.isChecked())
            elif kind == "int":
                _set(cfg, path, widget.value())
            else:
                _set(cfg, path, widget.value() / scale)

    def refresh_after_validation(self, cfg: EngineConfig) -> None:
        """Show the operator what `validate()` actually did to their input.

        Without this the panel keeps displaying a value the engine already
        overrode, which is exactly the situation where someone believes they
        are risking one amount while the engine risks another.
        """
        self.load_from(cfg)

    def set_locked(self, locked: bool) -> None:
        for widget, _kind, _scale in self._widgets.values():
            widget.setEnabled(not locked)
