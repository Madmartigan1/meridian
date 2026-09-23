#!/usr/bin/env python3
"""Meridian entrypoint.

    python meridian.py --generate-data
    python meridian.py --help

Kept as a thin shim so the package can be run from a clone without installing
anything, and so the CLI has one documented door.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
