"""Filesystem paths used by PyEmbedBuilder."""
from __future__ import annotations

from pathlib import Path


APP_NAME = "PyEmbedBuilder"
EMBED_ROOT_DIRNAME = "python-embed"
APP_DATA_DIRNAME = ".pyembed_builder"
OUTPUT_BASE_DIRNAME = "My Projects"


def project_root() -> Path:
    """Project root (folder containing the builder package)."""
    try:
        # pyembed_builder/util/paths.py -> project root is two levels up
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def app_data_dir() -> Path:
    """Root data directory for PyEmbedBuilder (relative to project root)."""
    d = project_root() / APP_DATA_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """Cache directory for downloaded archives."""
    d = app_data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def envs_dir() -> Path:
    """Default directory for created environments (sibling of builder)."""
    d = output_base_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """Directory for build and audit logs."""
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_base_dir() -> Path:
    """Base folder for new environments (sibling to builder folder)."""
    d = project_root().parent / OUTPUT_BASE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d
