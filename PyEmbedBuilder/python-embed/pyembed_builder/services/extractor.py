"""
Secure ZIP extraction with path-traversal (zip-slip) protection.
"""
from __future__ import annotations

import stat
import shutil
import zipfile
from pathlib import Path

from ..security import audit, validate_zip_entry


# Safety limits
MAX_EXTRACTED_FILE_SIZE = 2 * 1024 * 1024 * 1024   # 2 GB per file
MAX_TOTAL_EXTRACTED_SIZE = 5 * 1024 * 1024 * 1024   # 5 GB total
MAX_ZIP_ENTRIES = 10_000


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract a ZIP archive with full security validation.

    Security checks performed:
    - Every entry validated for path traversal (zip-slip)
    - Per-file size limit
    - Total extraction size limit
    - Entry count limit
    - Extraction to a temp directory first, then atomic rename
    """
    audit("extract_start", zip=str(zip_path), dest=str(dest_dir))

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = dest_dir.with_name(dest_dir.name + ".extracting")

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            entries = zf.infolist()

            # ── Pre-flight checks (before extracting anything) ────────
            if len(entries) > MAX_ZIP_ENTRIES:
                raise RuntimeError(
                    f"ZIP contains {len(entries):,} entries "
                    f"(limit: {MAX_ZIP_ENTRIES:,})."
                )

            total_size = 0
            for info in entries:
                # Path-traversal check
                validate_zip_entry(info.filename, tmp_dir)

                # Reject symlink entries to avoid link-based traversal tricks.
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise RuntimeError(
                        f"ZIP contains symlink entry: {info.filename}"
                    )

                # Per-file size check
                if info.file_size > MAX_EXTRACTED_FILE_SIZE:
                    raise RuntimeError(
                        f"ZIP entry too large: {info.filename} "
                        f"({info.file_size:,} bytes, "
                        f"limit: {MAX_EXTRACTED_FILE_SIZE:,})."
                    )
                total_size += info.file_size

            # Total size check
            if total_size > MAX_TOTAL_EXTRACTED_SIZE:
                raise RuntimeError(
                    f"Total extracted size ({total_size:,} bytes) exceeds "
                    f"limit ({MAX_TOTAL_EXTRACTED_SIZE:,} bytes)."
                )

            # ── Extract (all entries validated) ───────────────────────
            zf.extractall(tmp_dir)

        # Atomic swap into final location
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        shutil.move(str(tmp_dir), str(dest_dir))

        audit(
            "extract_complete",
            dest=str(dest_dir),
            entries=str(len(entries)),
            total_bytes=str(total_size),
        )

    except Exception:
        # Clean up temp directory on any failure
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
