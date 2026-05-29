"""Data models shared across PyEmbedBuilder modules."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .util.versioning import Version

ProjectMode = Literal["create", "import", "git", "zip"]
StepStatus = Literal["pending", "running", "ok", "error"]
Architecture = Literal["amd64", "win32"]
FfmpegArch = Literal["auto", "x64", "x86"]


@dataclass(frozen=True)
class BuildPlan:
    """Everything needed to create an embedded Python environment."""
    env_name: str
    target_dir: Path
    project_mode: ProjectMode
    entry_point_rel: str
    version: Version
    arch: Architecture
    source_path: Path | None = None
    source_url: str = ""
    source_ref: str = ""
    window_only: bool = False
    create_desktop_shortcut: bool = False
    create_start_menu_shortcut: bool = False
    install_ffmpeg: bool = False
    ffmpeg_arch: FfmpegArch = "auto"
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
    status: StepStatus
    detail: str = ""
