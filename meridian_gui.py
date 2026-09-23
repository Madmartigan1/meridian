#!/usr/bin/env python3
"""Meridian GUI entrypoint.

    python meridian_gui.py

Thin shim so the desktop app has one documented door, mirroring meridian.py
for the CLI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
