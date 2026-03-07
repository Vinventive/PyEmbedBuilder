"""
Export helpers for packaging built environments for end users.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..security import audit


def export_portable_zip(env_dir: Path, zip_path: Path) -> Path:
    """Create a portable ZIP archive from a built environment directory."""
    if not env_dir.exists() or not env_dir.is_dir():
        raise FileNotFoundError(f"Environment directory not found: {env_dir}")

    zip_path = zip_path.with_suffix(".zip")
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    base_name = zip_path.with_suffix("")
    audit("export_zip_start", env=str(env_dir), zip=str(zip_path))

    archive_path = Path(
        shutil.make_archive(
            base_name=str(base_name),
            format="zip",
            root_dir=str(env_dir),
            base_dir=".",
        )
    )

    # make_archive already writes to {base_name}.zip; normalize anyway.
    if archive_path.resolve() != zip_path.resolve():
        zip_path.unlink(missing_ok=True)
        archive_path.replace(zip_path)

    audit("export_zip_complete", env=str(env_dir), zip=str(zip_path))
    return zip_path
