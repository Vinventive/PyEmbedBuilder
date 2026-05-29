"""
Project source preparation for PyEmbedBuilder.

Supports:
- create: keep/create files directly in target folder
- import: copy local project folder into target folder
- git: clone a repository and copy it into target folder
- zip: download a ZIP project archive and copy it into target folder
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import BuildPlan
from ..security import (
    audit,
    normalize_project_git_source,
    validate_project_source_url,
)
from ..util.paths import cache_dir
from .downloader import download_file
from .extractor import extract_zip
from .subprocess_runner import run_command_stream
from ._common import cache_key, rmtree_onerror, wipe_dir, reset_cache_dir


LogCb = Callable[[str], None]


_SKIP_DIRS: set[str] = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "python-embed",
    ".pyembed_builder",
}

_SKIP_FILE_GLOBS: tuple[str, ...] = ("*.pyc", "*.pyo", "*.log")


@dataclass(frozen=True)
class SourcePrepareResult:
    mode: str
    detail: str
    files_copied: int


def _assert_no_overlap(src: Path, dst: Path) -> None:
    src_r = src.resolve()
    dst_r = dst.resolve()
    if src_r == dst_r:
        return
    try:
        dst_r.relative_to(src_r)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Output folder cannot be inside the source project folder."
        )
    try:
        src_r.relative_to(dst_r)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Source project folder cannot be inside the output folder."
        )


def _copy_tree(src: Path, dst: Path, *, log_cb: LogCb) -> int:
    src = src.resolve()
    dst = dst.resolve()
    _assert_no_overlap(src, dst)

    copied = 0
    skipped_symlinks = 0
    dst.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src, topdown=True):
        rel = Path(root).resolve().relative_to(src)
        dirs[:] = [
            d
            for d in dirs
            if d not in _SKIP_DIRS and not (Path(root) / d).is_symlink()
        ]

        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)

        for name in files:
            if any(fnmatch.fnmatch(name, pat) for pat in _SKIP_FILE_GLOBS):
                continue
            in_file = Path(root) / name
            if in_file.is_symlink():
                skipped_symlinks += 1
                continue
            out_file = out_dir / name
            out_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(in_file, out_file)
            copied += 1

    log_cb(f"Copied {copied:,} source file(s) into output folder.")
    if skipped_symlinks:
        log_cb(f"Skipped {skipped_symlinks:,} symlinked file(s) for safety.")
    return copied


def _prepare_from_git(
    *,
    url: str,
    source_ref: str,
    env_dir: Path,
    log_cb: LogCb,
    cancel_event: threading.Event | None,
) -> tuple[str, int]:
    url = normalize_project_git_source(url)
    validate_project_source_url(url, "git")

    key = cache_key(f"{url}|{source_ref}")
    work_root = cache_dir() / "sources" / "git" / key
    log_cb("Resetting cached Git source for this build attempt...")
    reset_cache_dir(work_root)
    clone_dir = work_root / "_clone"

    source_ref = source_ref.strip()
    if source_ref:
        log_cb(f"Cloning repository ref '{source_ref}': {url}")
        try:
            run_command_stream(
                ["git", "clone", "--depth", "1", "--branch", source_ref, url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
        except Exception:
            log_cb("Shallow ref clone failed; retrying full clone + checkout...")
            reset_cache_dir(work_root)
            run_command_stream(
                ["git", "clone", url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
            try:
                run_command_stream(
                    ["git", "-C", str(clone_dir), "checkout", source_ref],
                    log_cb=log_cb,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to checkout Git ref '{source_ref}'. "
                    "Verify the branch, tag, or commit exists and is accessible.\n\n"
                    f"{exc}"
                ) from exc
    else:
        log_cb(f"Cloning repository: {url}")
        try:
            run_command_stream(
                ["git", "clone", "--depth", "1", url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
        except Exception:
            log_cb("Shallow clone failed; retrying full clone...")
            reset_cache_dir(work_root)
            run_command_stream(
                ["git", "clone", url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )

    copied = _copy_tree(clone_dir, env_dir, log_cb=log_cb)
    detail = f"git:{url}" + (f" @ {source_ref}" if source_ref else "")
    return detail, copied


def _prepare_from_zip(*, url: str, env_dir: Path, log_cb: LogCb) -> tuple[str, int]:
    validate_project_source_url(url, "zip")

    key = cache_key(url)
    zip_cache_dir = cache_dir() / "sources" / "zip"
    zip_cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_cache_dir / f"{key}.zip"
    extract_dir = zip_cache_dir / f"{key}.extract"

    log_cb(f"Downloading source ZIP: {url}")
    download_file(url, zip_path, source_policy="project_zip")

    shutil.rmtree(extract_dir, ignore_errors=True)
    extract_zip(zip_path, extract_dir)

    entries = [p for p in extract_dir.iterdir() if p.exists()]
    source_root = extract_dir
    if len(entries) == 1 and entries[0].is_dir():
        source_root = entries[0]

    copied = _copy_tree(source_root, env_dir, log_cb=log_cb)
    return f"zip:{url}", copied


def prepare_project_source(
    *,
    plan: BuildPlan,
    env_dir: Path,
    log_cb: LogCb,
    cancel_event: threading.Event | None = None,
) -> SourcePrepareResult:
    """Prepare project files in *env_dir* based on plan.project_mode."""
    env_dir.mkdir(parents=True, exist_ok=True)
    mode = plan.project_mode

    if mode == "create":
        audit("source_prepare", mode=mode, output=str(env_dir))
        log_cb("Source mode: create (using files in output folder).")
        return SourcePrepareResult(mode=mode, detail="blank", files_copied=0)

    if mode == "import":
        if not plan.source_path:
            raise ValueError("Local source folder is required for import mode.")
        src = plan.source_path.resolve()
        if not src.exists() or not src.is_dir():
            raise FileNotFoundError(f"Source project folder not found: {src}")
        if src == env_dir.resolve():
            audit("source_prepare", mode=mode, source=str(src), output=str(env_dir))
            log_cb("Source and output folders are identical; using in-place project files.")
            return SourcePrepareResult(
                mode=mode,
                detail=f"local_in_place:{src}",
                files_copied=0,
            )
        audit("source_prepare", mode=mode, source=str(src), output=str(env_dir))
        log_cb(f"Copying local project folder: {src}")
        copied = _copy_tree(src, env_dir, log_cb=log_cb)
        return SourcePrepareResult(
            mode=mode,
            detail=f"local:{src}",
            files_copied=copied,
        )

    if mode == "git":
        if not plan.source_url.strip():
            raise ValueError("Repository URL is required for git mode.")
        normalized_url = normalize_project_git_source(plan.source_url.strip())
        detail, copied = _prepare_from_git(
            url=normalized_url,
            source_ref=plan.source_ref.strip(),
            env_dir=env_dir,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )
        audit("source_prepare", mode=mode, source=normalized_url, output=str(env_dir))
        return SourcePrepareResult(mode=mode, detail=detail, files_copied=copied)

    if mode == "zip":
        if not plan.source_url.strip():
            raise ValueError("ZIP URL is required for zip mode.")
        detail, copied = _prepare_from_zip(
            url=plan.source_url.strip(),
            env_dir=env_dir,
            log_cb=log_cb,
        )
        audit("source_prepare", mode=mode, source=plan.source_url, output=str(env_dir))
        return SourcePrepareResult(mode=mode, detail=detail, files_copied=copied)

    raise ValueError(f"Unsupported project mode: {mode!r}")
