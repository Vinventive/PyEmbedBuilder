"""
Download service with strict source validation and size limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..security import audit
from .http import http_open_stream


ProgressCb = Callable[[int, int | None], None]

# Maximum download size: 500 MB
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
CHUNK_SIZE = 256 * 1024  # 256 KB


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int


def download_file(
    url: str,
    dest_path: Path,
    *,
    progress_cb: ProgressCb | None = None,
    timeout_s: float = 120.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    source_policy: str | None = None,
) -> DownloadResult:
    """Download a file and move it atomically into place.

    Security:
    - URL validated against domain allowlist and optional source policy
    - Content-Length validated against received bytes
    - Download size limited to prevent resource exhaustion
    - Partial files cleaned up on failure
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a .part temp file to avoid leaving corrupt files on failure
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    tmp_path.unlink(missing_ok=True)

    bytes_done = 0
    bytes_total: int | None = None

    audit("download_start", url=url, dest=str(dest_path))

    try:
        with http_open_stream(
            url,
            timeout_s=timeout_s,
            source_policy=source_policy,
        ) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                bytes_total = int(cl)
                if bytes_total > max_bytes:
                    raise RuntimeError(
                        f"Declared file size ({bytes_total:,} bytes) exceeds "
                        f"the download limit ({max_bytes:,} bytes)."
                    )

            with tmp_path.open("wb") as f:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_done += len(chunk)
                    if bytes_done > max_bytes:
                        raise RuntimeError(
                            f"Download exceeds size limit ({max_bytes:,} bytes)."
                        )
                    f.write(chunk)
                    if progress_cb:
                        progress_cb(bytes_done, bytes_total)

        # ── Content-Length cross-check ───────────────────────────────
        if bytes_total is not None and bytes_done != bytes_total:
            audit(
                "download_size_mismatch",
                url=url,
                expected_bytes=str(bytes_total),
                received_bytes=str(bytes_done),
            )
            raise RuntimeError(
                f"Download size mismatch.\n"
                f"  Expected: {bytes_total:,} bytes\n"
                f"  Received: {bytes_done:,} bytes"
            )

        # ── Atomic finalize ─────────────────────────────────────────
        dest_path.unlink(missing_ok=True)
        os.replace(str(tmp_path), str(dest_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    audit(
        "download_ok",
        url=url,
        size_bytes=str(bytes_done),
    )
    return DownloadResult(
        path=dest_path,
        size_bytes=bytes_done,
    )
