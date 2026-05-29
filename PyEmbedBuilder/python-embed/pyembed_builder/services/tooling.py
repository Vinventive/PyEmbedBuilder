"""Optional portable FFmpeg tooling installer."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Callable

from ..models import BuildPlan
from ..security import audit
from ..util.paths import cache_dir
from ._common import wipe_dir
from .downloader import MAX_DOWNLOAD_BYTES, download_file
from .extractor import extract_zip
from .http import http_get


LogCb = Callable[[str], None]
ProgressCb = Callable[[int, int | None], None]
CheckCb = Callable[[], None]

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA256_URL = FFMPEG_ZIP_URL + ".sha256"
TOOL_ENV_BAT = "_pyembed_env.bat"

_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def install_optional_tools(
    plan: BuildPlan,
    env_dir: Path,
    *,
    log_cb: LogCb,
    progress_cb: ProgressCb | None = None,
    check_cb: CheckCb | None = None,
) -> dict:
    """Install requested optional tooling into the portable environment."""
    artifacts: dict = {
        "ffmpeg": {"status": "skipped", "reason": "not requested"},
        "env_bat": "",
    }
    installed_any = False

    if plan.install_ffmpeg:
        _check(check_cb)
        artifacts["ffmpeg"] = _install_ffmpeg(
            plan,
            env_dir,
            log_cb=log_cb,
            progress_cb=progress_cb,
            check_cb=check_cb,
        )
        installed_any = True

    if installed_any:
        env_bat = _write_tool_env_bat(env_dir)
        artifacts["env_bat"] = env_bat.name
        audit("tooling_env_bat_written", path=str(env_bat))
        log_cb(f"Wrote optional tooling environment hook: {env_bat.name}")

    return artifacts


def _install_ffmpeg(
    plan: BuildPlan,
    env_dir: Path,
    *,
    log_cb: LogCb,
    progress_cb: ProgressCb | None,
    check_cb: CheckCb | None,
) -> dict:
    arch = "x64" if plan.ffmpeg_arch in {"auto", "x64"} else "x86"
    if arch == "x86":
        raise RuntimeError(
            "gyan.dev currently publishes 64-bit FFmpeg builds only. "
            "Choose FFmpeg arch 'auto' or 'x64'."
        )

    log_cb("Fetching FFmpeg SHA256 checksum from gyan.dev...")
    sha_text = http_get(
        FFMPEG_SHA256_URL,
        source_policy="ffmpeg_build_hash",
        max_bytes=16 * 1024,
    ).decode("utf-8", errors="replace")
    expected_hash = _parse_sha256_text(sha_text)

    zip_path = cache_dir() / Path(FFMPEG_ZIP_URL).name
    _download_verified(
        url=FFMPEG_ZIP_URL,
        dest_path=zip_path,
        expected_sha256=expected_hash,
        source_policy="ffmpeg_build_zip",
        tool_name="ffmpeg",
        log_cb=log_cb,
        progress_cb=progress_cb,
        check_cb=check_cb,
    )

    ffmpeg_dir = env_dir / "tools" / "ffmpeg"
    unpack_dir = env_dir / "_pyembed_tool_extract" / "ffmpeg"
    _extract_archive_payload(zip_path, unpack_dir, ffmpeg_dir, log_cb=log_cb)

    return {
        "status": "ok",
        "arch": arch,
        "path": _rel_path(ffmpeg_dir, env_dir),
        "url": FFMPEG_ZIP_URL,
        "sha256": expected_hash,
        "bin": _rel_path(ffmpeg_dir / "bin", env_dir),
    }


def _download_verified(
    *,
    url: str,
    dest_path: Path,
    expected_sha256: str,
    source_policy: str,
    tool_name: str,
    log_cb: LogCb,
    progress_cb: ProgressCb | None,
    check_cb: CheckCb | None,
) -> dict[str, object]:
    expected = expected_sha256.lower()
    if not _SHA256_RE.fullmatch(expected):
        raise RuntimeError(f"Invalid SHA256 for {tool_name}: {expected_sha256!r}")

    if dest_path.exists() and dest_path.is_file():
        try:
            actual = _sha256_file(dest_path)
        except OSError:
            actual = ""
        if actual == expected:
            size = dest_path.stat().st_size
            log_cb(f"Using cached {tool_name} archive; SHA256 verified.")
            audit(
                "tool_hash_verified",
                tool=tool_name,
                source="cache",
                sha256=actual,
            )
            return {"path": dest_path, "size_bytes": size, "sha256": actual}
        dest_path.unlink(missing_ok=True)
        log_cb(f"Cached {tool_name} archive hash mismatch. Re-downloading.")

    _check(check_cb)
    result = download_file(
        url,
        dest_path,
        progress_cb=progress_cb,
        max_bytes=MAX_DOWNLOAD_BYTES,
        source_policy=source_policy,
    )
    actual = _sha256_file(result.path)
    if actual != expected:
        result.path.unlink(missing_ok=True)
        audit(
            "tool_hash_mismatch",
            level="ERROR",
            tool=tool_name,
            expected_sha256=expected,
            actual_sha256=actual,
        )
        raise RuntimeError(
            f"{tool_name} SHA256 mismatch.\n"
            f"  Expected: {expected}\n"
            f"  Actual:   {actual}"
        )
    audit(
        "tool_hash_verified",
        tool=tool_name,
        source="download",
        sha256=actual,
    )
    log_cb(f"Verified {tool_name} SHA256: {actual}")
    return {"path": result.path, "size_bytes": result.size_bytes, "sha256": actual}


def _extract_archive_payload(
    zip_path: Path,
    unpack_dir: Path,
    dest_dir: Path,
    *,
    log_cb: LogCb,
    wipe_dest: bool = True,
) -> None:
    wipe_dir(unpack_dir)
    extract_zip(zip_path, unpack_dir)
    payload_root = _payload_root(unpack_dir)
    if wipe_dest:
        wipe_dir(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _merge_tree(payload_root, dest_dir)
    wipe_dir(unpack_dir)
    log_cb(f"Extracted optional tool archive to: {dest_dir}")


def _payload_root(unpack_dir: Path) -> Path:
    children = [p for p in unpack_dir.iterdir() if p.name not in {".", ".."}]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return unpack_dir


def _merge_tree(src: Path, dest: Path) -> None:
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _write_tool_env_bat(env_dir: Path) -> Path:
    path = env_dir / TOOL_ENV_BAT
    path.write_text(
        "@echo off\r\n"
        'set "PYEMBED_TOOL_ROOT=%~dp0tools"\r\n'
        'if exist "%PYEMBED_TOOL_ROOT%\\ffmpeg\\bin" set "PATH=%PYEMBED_TOOL_ROOT%\\ffmpeg\\bin;%PATH%"\r\n',
        encoding="utf-8",
    )
    return path


def _parse_sha256_text(value: str) -> str:
    match = _SHA256_RE.search(value)
    if not match:
        raise RuntimeError("SHA256 checksum text did not contain a 64-character hash.")
    return match.group(1).lower()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _rel_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base)).replace("/", "\\")
    except ValueError:
        return str(path)


def _check(check_cb: CheckCb | None) -> None:
    if check_cb is not None:
        check_cb()
