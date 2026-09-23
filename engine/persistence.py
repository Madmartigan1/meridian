# engine/persistence.py
"""State serialisation.

Two invariants:

1. Simulated and live state never share a file. The path set comes from
   `constants.state_paths(simulated)` and is never assembled by hand, so a
   simulated run cannot corrupt a live ledger even if a caller is confused
   about which mode it is in.
2. Writes are atomic. A run interrupted mid-write must leave the previous
   state readable, not a truncated file -- an engine that starts up unable to
   read its own positions is worse than one that did not start.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import state_paths
from .position import ClosedLeg, PositionBook
from .risk import RiskState

log = logging.getLogger(__name__)

TRADE_COLUMNS = [
    "closed_at", "opened_at", "symbol", "side", "lots",
    "entry_price", "exit_price", "pips", "swap_pips", "net_pips",
    "pnl", "commission", "entry_reason", "exit_reason", "equity_after",
]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    """All on-disk state for one run mode."""

    def __init__(self, simulated: bool = True):
        self.simulated = bool(simulated)
        self.paths = state_paths(self.simulated)
        try:
            Path(self.paths["root"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Store: cannot create state dir %s (%s); running without "
                        "persistence.", self.paths["root"], exc)

    # --- positions ----------------------------------------------------------

    def save_positions(self, book: PositionBook) -> None:
        try:
            _atomic_write(Path(self.paths["positions"]), json.dumps(book.to_list(), indent=2))
        except OSError as exc:
            log.warning("Store: failed to save positions (%s).", exc)

    def load_positions(self, book: PositionBook) -> int:
        path = Path(self.paths["positions"])
        if not path.exists():
            return 0
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Store: positions file unreadable (%s); starting flat.", exc)
            return 0
        book.load_list(rows)
        return len(book)

    # --- account ------------------------------------------------------------

    def save_account(self, data: Dict[str, Any]) -> None:
        try:
            _atomic_write(Path(self.paths["account"]), json.dumps(data, indent=2))
        except OSError as exc:
            log.warning("Store: failed to save account (%s).", exc)

    def load_account(self) -> Dict[str, Any]:
        path = Path(self.paths["account"])
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # --- daily risk ---------------------------------------------------------

    def save_risk_state(self, state: RiskState) -> None:
        try:
            _atomic_write(Path(self.paths["daily_risk"]), json.dumps(state.to_dict(), indent=2))
        except OSError as exc:
            log.warning("Store: failed to save risk state (%s).", exc)

    def load_risk_state(self) -> RiskState:
        path = Path(self.paths["daily_risk"])
        if not path.exists():
            return RiskState()
        try:
            return RiskState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return RiskState()

    # --- trade ledger -------------------------------------------------------

    def append_trade(
        self,
        leg: ClosedLeg,
        pnl: float,
        commission: float,
        equity_after: float,
    ) -> None:
        path = Path(self.paths["trades_csv"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
                if is_new:
                    writer.writeheader()
                writer.writerow({
                    "closed_at": leg.closed_at,
                    "opened_at": leg.opened_at,
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "lots": "%.2f" % leg.lots,
                    "entry_price": "%.6f" % leg.entry_price,
                    "exit_price": "%.6f" % leg.exit_price,
                    "pips": "%.1f" % leg.pips,
                    "swap_pips": "%.2f" % leg.swap_pips,
                    "net_pips": "%.1f" % (leg.pips + leg.swap_pips),
                    "pnl": "%.2f" % pnl,
                    "commission": "%.2f" % commission,
                    "entry_reason": leg.entry_reason,
                    "exit_reason": leg.exit_reason,
                    "equity_after": "%.2f" % equity_after,
                })
        except OSError as exc:
            log.warning("Store: failed to append trade (%s).", exc)

    def read_trades(self) -> List[Dict[str, str]]:
        path = Path(self.paths["trades_csv"])
        if not path.exists():
            return []
        try:
            with path.open("r", newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
        except OSError:
            return []

    def reset(self) -> None:
        """Delete this mode's state. Only ever offered for simulated runs."""
        if not self.simulated:
            raise RuntimeError("refusing to reset live state")
        for key in ("positions", "trades_csv", "account", "daily_risk"):
            try:
                Path(self.paths[key]).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Store: could not remove %s (%s).", self.paths[key], exc)
