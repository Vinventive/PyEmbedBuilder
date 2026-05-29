"""Shared utilities for PyEmbedBuilder service modules."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from pathlib import Path


def cache_key(value: str) -> str:
    """Generate a short deterministic cache key from an arbitrary string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def rmtree_onerror(func, path: str, _exc_info) -> None:
    """Error handler for shutil.rmtree that retries after removing read-only."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:
        pass
    try:
        func(path)
    except Exception:
        pass


def wipe_dir(path: Path, *, retries: int = 4, delay_s: float = 0.12) -> None:
    """Remove a directory tree with retries for Windows file-lock issues."""
    if not path.exists():
        return
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            shutil.rmtree(path, onerror=rmtree_onerror)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(delay_s)
    if path.exists():
        raise RuntimeError(f"Failed to remove directory: {path}") from last_exc


def reset_cache_dir(path: Path) -> None:
    """Wipe and re-create a cache directory."""
    wipe_dir(path)
    path.mkdir(parents=True, exist_ok=True)
