"""PyEmbedBuilder application entry point."""
from __future__ import annotations

import ctypes
import sys


def _setup_dpi_awareness() -> None:
    """Enable DPI awareness on Windows 10/11 for crisp rendering."""
    if sys.platform != "win32":
        return
    try:
        # Per-monitor DPI aware (Windows 10 1607+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            # System DPI aware (older Windows)
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main() -> None:
    """Launch the PyEmbedBuilder GUI."""
    _setup_dpi_awareness()

    # Import after DPI setup so GUI modules initialize with correct scaling.
    from .ui.main_window import PyEmbedBuilderApp

    app = PyEmbedBuilderApp()
    app.mainloop()
