"""Data models shared across PyEmbedBuilder modules."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util.versioning import Version


@dataclass(frozen=True)
class BuildPlan:
    """Everything needed to create an embedded Python environment."""
    env_name: str
    target_dir: Path
    project_mode: str  # "create" | "import" | "git" | "zip"
    entry_point_rel: str
    version: Version
    arch: str  # "amd64" or "win32"
    source_path: Path | None = None
    source_url: str = ""
    source_ref: str = ""
    window_only: bool = False
    dependency_no_deps: bool = False
    auto_install_project: bool = True
    requirements_txt: Path | None = None
    manual_packages: tuple[str, ...] = ()
    use_pymanager_components: bool = False
    clear_cache_on_success: bool = False


@dataclass(frozen=True)
class StepUpdate:
    """A single build-step status update for the UI."""
    step_id: str
    title: str
    status: str  # "pending" | "running" | "ok" | "error"
    detail: str = ""
