"""PyEmbedBuilder application entry point."""
from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path


def _setup_dpi_awareness() -> None:
    """Enable DPI awareness on Windows 10/11 for crisp rendering."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _setup_tcl_tk_env() -> None:
    """Ensure TCL_LIBRARY and TK_LIBRARY point to the bundled Tcl/Tk runtime."""
    try:
        py_root = Path(__file__).resolve().parents[1]
    except Exception:
        return

    tcl_root = py_root / "tcl"
    if not tcl_root.is_dir():
        return

    if not os.environ.get("TCL_LIBRARY"):
        for name in ("tcl8.7", "tcl8.6", "tcl8.5"):
            cand = tcl_root / name
            if (cand / "init.tcl").exists():
                os.environ["TCL_LIBRARY"] = str(cand)
                break

    if not os.environ.get("TK_LIBRARY"):
        for name in ("tk8.7", "tk8.6", "tk8.5"):
            cand = tcl_root / name
            if (cand / "tk.tcl").exists():
                os.environ["TK_LIBRARY"] = str(cand)
                break

    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is not None:
        for p in (py_root, py_root / "DLLs"):
            if p.exists():
                try:
                    add_dll(str(p))
                except OSError:
                    pass


def _write_crash_log(tb_text: str) -> Path | None:
    """Write crash traceback to a file next to the application."""
    try:
        from .util.paths import logs_dir
        log_path = logs_dir() / "crash.log"
    except Exception:
        try:
            log_path = Path(__file__).resolve().parents[1] / "crash.log"
        except Exception:
            return None
    try:
        with log_path.open("a", encoding="utf-8") as f:
            import datetime
            f.write(f"\n{'='*72}\n")
            f.write(f"Crash at {datetime.datetime.now().isoformat()}\n")
            f.write(tb_text)
            f.write("\n")
        return log_path
    except Exception:
        return None


def main() -> None:
    """Launch the PyEmbedBuilder GUI."""
    _setup_dpi_awareness()
    _setup_tcl_tk_env()

    try:
        from .ui.main_window import PyEmbedBuilderApp

        app = PyEmbedBuilderApp()
        app.mainloop()
    except Exception:
        tb_text = traceback.format_exc()
        crash_path = _write_crash_log(tb_text)

        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            detail = f"Details written to:\n{crash_path}" if crash_path else ""
            messagebox.showerror(
                "PyEmbedBuilder - Fatal Error",
                f"An unexpected error occurred and the application must close.\n\n"
                f"{tb_text[:1200]}\n\n{detail}",
            )
            root.destroy()
        except Exception:
            print(tb_text, file=sys.stderr)
        sys.exit(1)
