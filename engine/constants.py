# engine/constants.py
"""Canonical paths, versions, and fixed numeric conventions.

State layout mirrors the live/simulated split used throughout the engine:
nothing written during a simulated run may land in a live state file.
"""
import os
import sys
import tempfile
from pathlib import Path

MERIDIAN_VERSION = "0.1.0"

# Money is accounted in account currency; pips are reported to 1 decimal.
MONEY_DECIMALS = 2
PIP_DECIMALS = 1
PRICE_DECIMALS = 6


def _user_state_dir() -> Path:
    """User-writable state dir. Windows: %LOCALAPPDATA%/Meridian/state."""
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.getenv("XDG_STATE_HOME")
    if base:
        return Path(base) / "Meridian" / "state"
    return Path(tempfile.gettempdir()) / "Meridian" / "state"


def _is_writable_dir(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


# An explicit MERIDIAN_STATE_DIR always wins.
_env_state = os.getenv("MERIDIAN_STATE_DIR")
if _env_state:
    STATE_DIR = Path(_env_state)
else:
    _repo_default = (Path(__file__).resolve().parent / ".." / ".state").resolve()
    if getattr(sys, "frozen", False):
        STATE_DIR = _user_state_dir()
    else:
        STATE_DIR = _repo_default if _is_writable_dir(_repo_default) else _user_state_dir()

try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# --- Live state (only written when cfg.simulated is False) -------------------
POSITIONS_FILE = STATE_DIR / "positions.json"
TRADES_CSV_FILE = STATE_DIR / "trades.csv"
ACCOUNT_FILE = STATE_DIR / "account.json"
DAILY_RISK_FILE = STATE_DIR / "daily_risk.json"
RUN_LOG_FILE = STATE_DIR / "run_log.txt"

# --- Simulated state (hard-separated subtree) --------------------------------
SIM_STATE_DIR = STATE_DIR / "sim_data"
SIM_POSITIONS_FILE = SIM_STATE_DIR / "sim_positions.json"
SIM_TRADES_CSV_FILE = SIM_STATE_DIR / "sim_trades.csv"
SIM_ACCOUNT_FILE = SIM_STATE_DIR / "sim_account.json"
SIM_DAILY_RISK_FILE = SIM_STATE_DIR / "sim_daily_risk.json"


def state_paths(simulated: bool) -> dict:
    """Return the state file set for the requested run mode.

    The engine never chooses a path by hand; it asks here. That is what keeps
    the live/simulated separation a single-point invariant.
    """
    if simulated:
        return {
            "root": SIM_STATE_DIR,
            "positions": SIM_POSITIONS_FILE,
            "trades_csv": SIM_TRADES_CSV_FILE,
            "account": SIM_ACCOUNT_FILE,
            "daily_risk": SIM_DAILY_RISK_FILE,
        }
    return {
        "root": STATE_DIR,
        "positions": POSITIONS_FILE,
        "trades_csv": TRADES_CSV_FILE,
        "account": ACCOUNT_FILE,
        "daily_risk": DAILY_RISK_FILE,
    }
