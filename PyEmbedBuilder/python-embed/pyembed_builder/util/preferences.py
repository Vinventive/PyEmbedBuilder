"""User preferences and setup configuration persistence for PyEmbedBuilder."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .paths import app_data_dir

_PREFS_FILENAME = "preferences.json"
_SETUPS_DIR_NAME = "setups"

_DEFAULTS: dict[str, Any] = {
    "theme_mode": "Dark",
    "text_size": "Large",
    "arch": "amd64",
    "project_mode": "create",
    "window_geometry": "",
    "ffmpeg_arch": "auto",
    "use_pymanager_components": True,
    "clear_cache": True,
    "auto_analyze_source": True,
    "auto_install_project": True,
}

_SETUP_FIELDS = (
    "project_mode",
    "env_name",
    "target_dir",
    "source_dir",
    "source_url",
    "source_ref",
    "entry_point",
    "window_only",
    "create_desktop_shortcut",
    "create_start_menu_shortcut",
    "install_ffmpeg",
    "ffmpeg_arch",
    "python_mode",
    "python_version",
    "arch",
    "use_requirements",
    "requirements_path",
    "manual_packages",
    "dependency_no_deps",
    "auto_install_project",
    "auto_analyze_source",
    "use_pymanager_components",
    "clear_cache",
)

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


# ── App preferences ──────────────────────────────────────────────────────

def _prefs_path() -> Path:
    return app_data_dir() / _PREFS_FILENAME


def load_preferences() -> dict[str, Any]:
    """Load preferences from disk, falling back to defaults."""
    prefs = dict(_DEFAULTS)
    path = _prefs_path()
    if not path.exists():
        return prefs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in _DEFAULTS:
                if key in data:
                    prefs[key] = data[key]
    except Exception:
        pass
    return prefs


def save_preferences(prefs: dict[str, Any]) -> None:
    """Persist preferences to disk with atomic write."""
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(prefs, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)


# ── Setup configurations ─────────────────────────────────────────────────

def _setups_dir() -> Path:
    d = app_data_dir() / _SETUPS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_setup_name(name: str) -> str:
    """Validate and return a safe setup configuration name."""
    name = name.strip()
    if not name:
        raise ValueError("Setup name cannot be empty.")
    if not _SAFE_FILENAME_RE.fullmatch(name):
        raise ValueError(
            "Setup name must be 1-64 characters, start with a letter or digit, "
            "and contain only letters, digits, spaces, '.', '-', or '_'."
        )
    return name


def _setup_path(name: str) -> Path:
    return _setups_dir() / f"{name}.json"


def save_setup_config(name: str, fields: dict[str, Any]) -> Path:
    """Save a named setup configuration to disk.

    Returns the path to the saved file.
    """
    name = _sanitize_setup_name(name)
    payload: dict[str, Any] = {
        "setup_name": name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for key in _SETUP_FIELDS:
        if key in fields:
            payload[key] = fields[key]

    path = _setup_path(name)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def load_setup_config(name: str) -> dict[str, Any]:
    """Load a named setup configuration from disk."""
    name = _sanitize_setup_name(name)
    path = _setup_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Setup configuration not found: {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid setup configuration file: {name}")
    return data


def delete_setup_config(name: str) -> None:
    """Delete a named setup configuration from disk."""
    name = _sanitize_setup_name(name)
    path = _setup_path(name)
    path.unlink(missing_ok=True)


def list_setup_configs() -> list[str]:
    """Return sorted list of saved setup configuration names."""
    d = _setups_dir()
    if not d.exists():
        return []
    names: list[str] = []
    for p in sorted(d.glob("*.json")):
        if p.is_file():
            names.append(p.stem)
    return names


SETUP_FIELDS = _SETUP_FIELDS
