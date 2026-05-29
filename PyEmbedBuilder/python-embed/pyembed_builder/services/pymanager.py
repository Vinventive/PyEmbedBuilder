"""
Extract stdlib + tools from official per-version MSI packages on python.org.

python.org publishes individual MSI packages for every Windows release at::

    https://www.python.org/ftp/python/{version}/{arch}/

Each architecture subdirectory contains MSI files such as:

    core.msi   - Core runtime (python.exe, DLLs, .pyd modules)
    lib.msi    - Standard library (Lib/)
    tcltk.msi  - Tcl/Tk runtime (tcl/, _tkinter.pyd)
    dev.msi    - Development headers & import libraries (include/, libs/)
    tools.msi  - Scripts and tools (Scripts/)

We download only the MSIs we need for the exact version the user picked,
extract them with the Windows built-in ``msiexec /a`` (admin install, no
elevation required), and copy the desired directories into the embedded
environment's ``python-embed/`` root.

This is entirely optional and best-effort - if the MSI files aren't
available or extraction fails, the build continues without them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import ctypes
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..security import audit
from ..util.paths import cache_dir
from ..util.versioning import Version
from .downloader import download_file


_FTP_BASE = "https://www.python.org/ftp/python"

# Map our arch names to python.org FTP subdirectory names
_ARCH_FTP_DIR = {"amd64": "amd64", "win32": "win32", "arm64": "arm64"}

# Directories we want to copy from the extracted MSIs into py_root
_COMPONENT_DIRS = ("Scripts", "DLLs", "tcl", "Lib", "libs", "include")

# MSI packages to download  (filename, human description)
_MSI_PACKAGES: list[tuple[str, str]] = [
    ("core.msi",  "Core runtime (DLLs, .pyd)"),
    ("lib.msi",   "Standard library (Lib/)"),
    ("tcltk.msi", "Tcl/Tk (tcl/)"),
    ("dev.msi",   "Headers & libs (include/, libs/)"),
    ("tools.msi", "Scripts & tools (Scripts/)"),
]


@dataclass(frozen=True)
class PyManagerResult:
    status: str                          # "ok" | "skipped"
    msix_version: str = ""
    msix_url: str = ""
    pythoncore_zip: str = ""
    extracted_dirs: list[str] = field(default_factory=list)
    reason: str = ""


# ── helpers ───────────────────────────────────────────────────────────────

def _short_path(path: Path) -> str:
    """Return Windows short path (8.3) when available; fallback to full path."""
    p = str(path)
    if os.name != "nt":
        return p
    try:
        buf = ctypes.create_unicode_buffer(260)
        r = ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf))
        return buf.value if r else p
    except Exception:
        return p


def _resolve_msiexec() -> str | None:
    """Resolve msiexec to an absolute trusted path when possible."""
    if os.name == "nt":
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msiexec.exe"
        if system32.exists():
            return str(system32)
    found = shutil.which("msiexec")
    return str(Path(found).resolve()) if found else None


def _extract_msi(msi_path: Path, target_dir: Path, log_cb) -> bool:
    """Extract an MSI using Windows built-in msiexec (admin install mode).

    ``msiexec /a`` performs a network/administrative install which simply
    extracts the MSI contents to *target_dir* without registering anything
    in the system.  No elevation required.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # Use clean absolute paths (short-path when available to avoid space issues).
    abs_target = _short_path(target_dir.resolve())
    abs_msi = _short_path(msi_path.resolve())

    # Primary: administrative install (extract without system install).
    msiexec_exe = _resolve_msiexec()
    if not msiexec_exe:
        log_cb("    msiexec not found - is this a Windows system?")
        return False

    cmd = [
        msiexec_exe,
        "/a", abs_msi,
        "/qn",
        f"TARGETDIR={abs_target}",
        f"INSTALLDIR={abs_target}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""
            detail = stderr or stdout or "(no details)"
            log_cb(f"    msiexec exit {result.returncode}: {detail}")
            audit(
                "pymanager_msiexec_fail",
                level="WARNING",
                msi=msi_path.name,
                exit_code=str(result.returncode),
            )

            return False
        return True
    except FileNotFoundError:
        log_cb("    msiexec executable disappeared during launch.")
        return False
    except subprocess.TimeoutExpired:
        log_cb(f"    msiexec timed out extracting {msi_path.name}")
        return False
    except Exception as exc:
        log_cb(f"    msiexec error: {exc}")
        return False


def _flatten_platform_dirs(root: Path, log_cb) -> None:
    """Flatten MSI platform subdirectories throughout the extraction tree.

    MSI packages (especially tools.msi) organise scripts into::

        Scripts/common/   - cross-platform scripts
        Scripts/nt/       - Windows-specific scripts
        Scripts/posix/    - Unix-specific scripts (not needed)

    We merge ``common/`` and ``nt/`` contents up into the parent and
    remove all three subdirectories.  This mirrors what the real
    installer does during a normal install on Windows.
    """
    # Subdirs whose contents should be moved up into the parent
    _MERGE = ("common", "nt")
    # Subdirs to delete entirely (wrong platform)
    _DISCARD = ("posix",)

    # Search the whole extraction tree for any directory that has
    # these platform sub-dirs (could be Scripts/, or nested deeper).
    for parent in list(root.rglob("common")):
        if not parent.is_dir():
            continue
        container = parent.parent  # e.g. .../Scripts
        # Only act if this looks like a platform-split directory
        if not (container / "nt").is_dir():
            continue
        log_cb(f"  Flattening platform dirs in {container.name}/")
        for subname in _MERGE:
            sub = container / subname
            if sub.is_dir():
                for item in sub.iterdir():
                    dest = container / item.name
                    if item.is_dir():
                        if dest.is_dir():
                            shutil.copytree(item, dest, dirs_exist_ok=True)
                        else:
                            shutil.move(str(item), str(dest))
                    else:
                        shutil.move(str(item), str(dest))
                shutil.rmtree(sub, ignore_errors=True)
        for subname in _DISCARD:
            sub = container / subname
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)


# Files and directories from the MSI that serve no purpose in an embedded
# Python environment (venv scaffolding, idle shell, etc.).
_JUNK_FILES: set[str] = {
    # venv activation / launcher - embedded Python IS the environment
    "activate",
    "activate.bat",
    "activate.fish",
    "Activate.ps1",
    "deactivate.bat",
    "venvlauncher.exe",
    "venvwlauncher.exe",
    # duplicates when tools.msi is merged into Scripts/
    "python.exe",
    "pythonw.exe",
    "wheel.exe",
}
_JUNK_DIRS: set[str] = {
    # idlelib is 3+ MB and not useful for headless/embedded apps
    "idlelib",
    # test suite is large and not needed at runtime
    "test",
    "tests",
}


def _purge_embed_junk(py_root: Path, log_cb) -> None:
    """Remove files and directories that are useless in an embedded env."""
    removed: list[str] = []

    # Files in Scripts/
    scripts = py_root / "Scripts"
    if scripts.is_dir():
        for fname in _JUNK_FILES:
            f = scripts / fname
            if f.exists():
                f.unlink(missing_ok=True)
                removed.append(f"Scripts/{fname}")

    # Large directories in Lib/
    lib = py_root / "Lib"
    if lib.is_dir():
        for dname in _JUNK_DIRS:
            d = lib / dname
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed.append(f"Lib/{dname}")

    if removed:
        log_cb(f"  Pruned {len(removed)} unneeded items: {', '.join(removed)}")


def _find_dir_recursive(root: Path, name: str, max_depth: int = 3) -> Path | None:
    """Find a directory called *name* under *root*, up to *max_depth* levels."""
    if not root.is_dir():
        return None
    candidate = root / name
    if candidate.is_dir():
        return candidate
    if max_depth <= 0:
        return None
    try:
        for child in root.iterdir():
            if child.is_dir() and child.name != name:
                found = _find_dir_recursive(child, name, max_depth - 1)
                if found:
                    return found
    except PermissionError:
        pass
    return None


# ── public API ────────────────────────────────────────────────────────────

def augment_from_pymanager(
    *,
    version: Version,
    arch: str,
    py_root: Path,
    log_cb,
    cancel_event: threading.Event | None = None,
) -> PyManagerResult:
    """Download per-version MSI packages from python.org and extract components.

    Flow:
      1. For each MSI (core, lib, tcltk, dev, tools):
         - Construct URL: https://www.python.org/ftp/python/{ver}/{arch}/{msi}
         - Download to cache (skip on 404 / error)
      2. Extract each MSI using ``msiexec /a``
      3. Copy Scripts, DLLs, tcl, Lib, libs, include into py_root
    """
    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("MSI augmentation cancelled by user.")

    ftp_arch = _ARCH_FTP_DIR.get(arch)
    if not ftp_arch:
        audit("pymanager_skip", level="WARNING", reason=f"unsupported_arch_{arch}")
        log_cb(f"Unsupported architecture for MSI packages: {arch}")
        return PyManagerResult(status="skipped", reason="unsupported_arch")

    base_url = f"{_FTP_BASE}/{version}/{ftp_arch}"
    audit("pymanager_start", version=str(version), arch=arch, base_url=base_url)
    log_cb(f"MSI source: {base_url}/")

    msi_cache = cache_dir() / "msi" / str(version) / ftp_arch
    msi_cache.mkdir(parents=True, exist_ok=True)

    # ── 1. Download MSI packages ──────────────────────────────────────
    _check_cancel()
    downloaded: list[Path] = []
    for msi_name, desc in _MSI_PACKAGES:
        _check_cancel()
        url = f"{base_url}/{msi_name}"
        msi_path = msi_cache / msi_name
        has_cached = msi_path.exists() and msi_path.stat().st_size > 0

        try:
            if has_cached:
                log_cb(f"  {msi_name}: refreshing from source ({desc})...")
            else:
                log_cb(f"  {msi_name}: downloading ({desc})...")
            res = download_file(
                url,
                msi_path,
                source_policy="python_msi",
            )
            downloaded.append(msi_path)
            audit(
                "pymanager_msi_ok",
                name=msi_name,
                url=url,
                size_bytes=str(res.size_bytes),
            )
        except Exception as exc:
            if has_cached:
                downloaded.append(msi_path)
                log_cb(f"  {msi_name}: source download failed, using cached copy")
                audit(
                    "pymanager_msi_cache_fallback",
                    level="WARNING",
                    name=msi_name,
                    url=url,
                    error=str(exc),
                )
            else:
                audit(
                    "pymanager_msi_unavailable",
                    level="WARNING",
                    name=msi_name,
                    url=url,
                    error=str(exc),
                )
                log_cb(f"  {msi_name}: not available")

    if not downloaded:
        audit("pymanager_no_msi", level="WARNING", base_url=base_url)
        log_cb("No MSI packages available for this version/arch - skipping.")
        return PyManagerResult(status="skipped", reason="no_msi_available")

    log_cb(f"Downloaded {len(downloaded)}/{len(_MSI_PACKAGES)} MSI packages")

    # ── 2. Extract each MSI ───────────────────────────────────────────
    tmp_extract = msi_cache / "_extract.tmp"
    if tmp_extract.exists():
        shutil.rmtree(tmp_extract, ignore_errors=True)

    _check_cancel()
    ok_count = 0
    for msi_path in downloaded:
        _check_cancel()
        log_cb(f"  Extracting {msi_path.name}...")
        if _extract_msi(msi_path, tmp_extract, log_cb):
            ok_count += 1
            audit("pymanager_extract_ok", name=msi_path.name)
        else:
            audit("pymanager_extract_fail", level="WARNING", name=msi_path.name)

    if ok_count == 0:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        audit("pymanager_extract_all_failed", level="ERROR")
        log_cb("All MSI extractions failed - skipping.")
        return PyManagerResult(status="skipped", reason="extraction_failed")

    # ── 3. Flatten MSI platform sub-dirs before copying ─────────────
    # MSI packages (especially tools.msi) store scripts under platform
    # subdirectories: Scripts/common/, Scripts/nt/, Scripts/posix/.
    # The real installer picks the right ones; we merge common + nt
    # (Windows) into the parent and discard posix.
    _flatten_platform_dirs(tmp_extract, log_cb)

    # ── 4. Locate and copy target directories ─────────────────────────
    # msiexec /a may nest files under SourceDir or a version folder;
    # we search a few levels deep to handle any extraction layout.
    copied: list[str] = []
    for dirname in _COMPONENT_DIRS:
        src = _find_dir_recursive(tmp_extract, dirname)
        if src:
            dest = py_root / dirname
            shutil.copytree(src, dest, dirs_exist_ok=True)
            copied.append(dirname)
            log_cb(f"  {dirname}: merged into {py_root.name}/")
        else:
            log_cb(f"  {dirname}: not found in extracted MSIs")

    # ── 5. Remove files useless in an embedded environment ──────────
    _purge_embed_junk(py_root, log_cb)

    # ── 6. Cleanup temp extraction + stray MSI copies ─────────────
    shutil.rmtree(tmp_extract, ignore_errors=True)

    for leftover in py_root.glob("*.msi"):
        leftover.unlink(missing_ok=True)

    audit(
        "pymanager_complete",
        version=str(version),
        arch=arch,
        dirs=",".join(copied),
        msi_count=str(len(downloaded)),
    )
    log_cb(f"Components added: {', '.join(copied) or 'none'}")

    return PyManagerResult(
        status="ok",
        msix_version=str(version),
        msix_url=base_url,
        pythoncore_zip=", ".join(p.name for p in downloaded),
        extracted_dirs=copied,
    )
