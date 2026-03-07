"""
Embedded Python _pth file patcher.

Correctly configures the _pth file so pip, site-packages, and Scripts work
in a Windows embedded Python distribution.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..security import audit


@dataclass(frozen=True)
class PthPatchResult:
    pth_path: Path
    zip_name: str


def find_pth_file(py_root: Path) -> Path:
    """Find the pythonXY._pth file in the embedded Python root."""
    matches = sorted(py_root.glob("python*._pth"))
    if not matches:
        raise FileNotFoundError(
            f"No pythonXY._pth file found in: {py_root}\n"
            "This may not be a valid embedded Python distribution."
        )
    if len(matches) > 1:
        audit("pth_multiple_found", count=str(len(matches)), using=matches[0].name)
    return matches[0]


def _detect_stdlib_zip(py_root: Path, pth_path: Path) -> str:
    """Determine the pythonXY.zip name from the _pth file or filesystem."""
    # Try reading from existing _pth content
    try:
        for line in pth_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.lower().endswith(".zip") and "python" in s.lower():
                if (py_root / s).exists():
                    return s
    except OSError:
        pass

    # Fallback: find python*.zip on disk
    candidates = sorted(py_root.glob("python*.zip"))
    if candidates:
        return candidates[0].name

    raise FileNotFoundError(
        f"No pythonXY.zip found in {py_root}. Cannot configure _pth."
    )


def patch_embedded_pth(py_root: Path) -> PthPatchResult:
    """Configure embedded Python _pth for pip and site-packages.

    This is the critical step that makes pip and third-party packages
    work in an embedded Python environment:

    - Enables ``import site`` (required for site-packages discovery)
    - Adds Lib, Lib/site-packages, and Scripts to the search path
    - Ensures necessary directories exist on disk
    """
    pth = find_pth_file(py_root)
    zip_name = _detect_stdlib_zip(py_root, pth)

    # Ensure required subdirectories exist
    for subdir in ("DLLs", "Lib", "Lib\\site-packages", "Scripts"):
        (py_root / subdir).mkdir(parents=True, exist_ok=True)

    # Write the corrected _pth
    lines = [
        zip_name,
        r".\DLLs",
        r".\Lib",
        r".\Lib\site-packages",
        r".\Scripts",
        ".",
        "import site",
        "",  # trailing newline
    ]
    pth.write_text("\n".join(lines), encoding="utf-8")

    audit("pth_patched", path=str(pth), stdlib_zip=zip_name)
    return PthPatchResult(pth_path=pth, zip_name=zip_name)
