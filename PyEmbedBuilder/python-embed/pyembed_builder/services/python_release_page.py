"""
Resolve download URL for embeddable Python ZIPs.

The download URL is deterministic::

    https://www.python.org/ftp/python/{ver}/python-{ver}-embed-{arch}.zip

Integrity relies on HTTPS transport plus strict trusted-source URL policies.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..security import audit
from ..util.versioning import Version
from .python_catalog import has_embeddable_archive


_FTP_BASE = "https://www.python.org/ftp/python"


@dataclass(frozen=True)
class PythonEmbeddableInfo:
    version: Version
    arch: str          # "amd64" or "win32"
    filename: str
    url: str
# ── public API ────────────────────────────────────────────────────────────

def get_embeddable_info(version: Version, arch: str) -> PythonEmbeddableInfo:
    """Resolve the official download URL for an embeddable ZIP."""
    if arch not in ("amd64", "win32"):
        raise ValueError(f"Unsupported architecture: {arch!r}")
    if not has_embeddable_archive(version, arch):
        raise ValueError(
            f"No embeddable ZIP published for Python {version} ({arch})."
        )

    filename = f"python-{version}-embed-{arch}.zip"
    url = f"{_FTP_BASE}/{version}/{filename}"

    audit("release_resolve", version=str(version), arch=arch)

    audit(
        "release_resolved",
        filename=filename,
        url=url,
    )

    return PythonEmbeddableInfo(
        version=version,
        arch=arch,
        filename=filename,
        url=url,
    )
