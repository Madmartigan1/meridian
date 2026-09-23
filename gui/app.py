# gui/app.py
"""GUI entrypoint.

Console logging stays configured alongside the in-window log: if the GUI fails
to start, or the window log is cleared, the terminal still has the record.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python gui/app.py` from a clone without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication      # noqa: E402

from gui import theme                            # noqa: E402
from gui.main_window import MainWindow           # noqa: E402


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Meridian")
    theme.apply(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
