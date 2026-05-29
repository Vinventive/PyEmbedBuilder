"""
Fetch and resolve embeddable Python versions from python.org.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..util.paths import cache_dir
from ..util.versioning import Version, is_stable_version_dirname
from .http import http_get


_FTP_INDEX_URL = "https://www.python.org/ftp/python/"
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_VERSION_CACHE_FILENAME = "embeddable_versions_cache.json"
_MIN_EMBEDDABLE_VERSION = Version.parse("3.5.0")
_SEEDED_EMBEDDABLE_VERSION_STRINGS = (
    "3.14.5",
    "3.14.4",
    "3.14.3",
    "3.14.2",
    "3.14.1",
    "3.14.0",
    "3.13.13",
    "3.13.12",
    "3.13.11",
    "3.13.10",
    "3.13.9",
    "3.13.8",
    "3.13.7",
    "3.13.6",
    "3.13.5",
    "3.13.4",
    "3.13.3",
    "3.13.2",
    "3.13.1",
    "3.13.0",
    "3.12.10",
    "3.12.9",
    "3.12.8",
    "3.12.7",
    "3.12.6",
    "3.12.5",
    "3.12.4",
    "3.12.3",
    "3.12.2",
    "3.12.1",
    "3.12.0",
    "3.11.9",
    "3.11.8",
    "3.11.7",
    "3.11.6",
    "3.11.5",
    "3.11.4",
    "3.11.3",
    "3.11.2",
    "3.11.1",
    "3.11.0",
    "3.10.11",
    "3.10.10",
    "3.10.9",
    "3.10.8",
    "3.10.7",
    "3.10.6",
    "3.10.5",
    "3.10.4",
    "3.10.3",
    "3.10.2",
    "3.10.1",
    "3.10.0",
    "3.9.13",
    "3.9.12",
    "3.9.11",
    "3.9.10",
    "3.9.9",
    "3.9.8",
    "3.9.7",
    "3.9.6",
    "3.9.5",
    "3.9.4",
    "3.9.3",
    "3.9.2",
    "3.9.1",
    "3.9.0",
    "3.8.10",
    "3.8.9",
    "3.8.8",
    "3.8.7",
    "3.8.6",
    "3.8.5",
    "3.8.4",
    "3.8.3",
    "3.8.2",
    "3.8.1",
    "3.8.0",
    "3.7.9",
    "3.7.8",
    "3.7.7",
    "3.7.6",
    "3.7.5",
    "3.7.4",
    "3.7.3",
    "3.7.1",
    "3.7.0",
    "3.6.8",
    "3.6.7",
    "3.6.6",
    "3.6.5",
    "3.6.4",
    "3.6.3",
    "3.6.2",
    "3.6.1",
    "3.6.0",
    "3.5.4",
    "3.5.3",
    "3.5.2",
    "3.5.1",
    "3.5.0",
)

_versions_cache: list[Version] | None = None
_versions_cache_ts: float = 0.0
_release_page_cache: dict[str, str] = {}
_embed_cache: dict[tuple[str, str], bool] = {}
_CACHE_TTL_S = 1800.0  # 30 minutes


@dataclass(frozen=True)
class PythonReleaseChoice:
    version: Version

    def __str__(self) -> str:
        return str(self.version)


def _merge_versions(*version_groups: list[Version]) -> list[Version]:
    merged: set[Version] = set()
    for versions in version_groups:
        merged.update(versions)
    return sorted(merged, reverse=True)


def _seeded_embeddable_versions(arch: str) -> list[Version]:
    if arch not in {"amd64", "win32"}:
        raise ValueError(f"Unsupported architecture: {arch!r}")
    return [Version.parse(raw) for raw in _SEEDED_EMBEDDABLE_VERSION_STRINGS]


def _list_all_stable_versions() -> list[Version]:
    global _versions_cache, _versions_cache_ts
    if _versions_cache is not None and (time.monotonic() - _versions_cache_ts) < _CACHE_TTL_S:
        return list(_versions_cache)

    html = http_get(
        _FTP_INDEX_URL,
        source_policy="python_ftp_index",
        audit_level="DEBUG",
    ).decode("utf-8", errors="replace")
    versions: set[Version] = set()

    for m in _HREF_RE.finditer(html):
        href = m.group(1)
        if not href.endswith("/"):
            continue
        if not is_stable_version_dirname(href):
            continue
        try:
            versions.add(Version.parse(href.rstrip("/")))
        except ValueError:
            continue

    _versions_cache = sorted(versions)
    _versions_cache_ts = time.monotonic()
    return list(_versions_cache)


def _release_index_html(version: Version) -> str:
    key = str(version)
    cached = _release_page_cache.get(key)
    if cached is not None:
        return cached

    url = f"{_FTP_INDEX_URL}{version}/"
    html = http_get(
        url,
        source_policy="python_ftp_release_index",
        audit_level="DEBUG",
    ).decode("utf-8", errors="replace")
    _release_page_cache[key] = html
    return html


def has_embeddable_archive(version: Version, arch: str) -> bool:
    """Return True when python.org hosts python-{version}-embed-{arch}.zip."""
    if arch not in {"amd64", "win32"}:
        raise ValueError(f"Unsupported architecture: {arch!r}")

    key = (str(version), arch)
    cached = _embed_cache.get(key)
    if cached is not None:
        return cached

    html = _release_index_html(version)
    expected = f"python-{version}-embed-{arch}.zip".lower()
    has_it = expected in html.lower()
    _embed_cache[key] = has_it
    return has_it


def list_stable_versions(
    min_version: Version | None = None,
    *,
    arch: str | None = None,
    embeddable_only: bool = True,
) -> list[PythonReleaseChoice]:
    """List stable versions from python.org, newest first.

    If *embeddable_only* is True:
    - with *arch*: include versions with an embeddable ZIP for that arch
    - without *arch*: include versions embeddable on at least one Windows arch
    """
    if embeddable_only and (
        min_version is None or min_version < _MIN_EMBEDDABLE_VERSION
    ):
        min_version = _MIN_EMBEDDABLE_VERSION
    versions = _list_all_stable_versions()
    result: list[Version] = []

    for v in versions:
        if min_version is not None and v < min_version:
            continue
        if not embeddable_only:
            result.append(v)
            continue
        if arch:
            if has_embeddable_archive(v, arch):
                result.append(v)
            continue
        if has_embeddable_archive(v, "amd64") or has_embeddable_archive(v, "win32"):
            result.append(v)

    result.sort(reverse=True)
    return [PythonReleaseChoice(v) for v in result]


def resolve_embeddable_at_or_above(version: Version, arch: str) -> Version | None:
    """Return the nearest embeddable version >= *version* for the given arch."""
    if version < _MIN_EMBEDDABLE_VERSION:
        version = _MIN_EMBEDDABLE_VERSION
    versions = _list_all_stable_versions()
    for candidate in versions:
        if candidate < version:
            continue
        if has_embeddable_archive(candidate, arch):
            return candidate
    return None


def newest_embeddable(arch: str) -> Version | None:
    """Return the newest embeddable version for *arch*."""
    versions = list_stable_versions(arch=arch, embeddable_only=True)
    if not versions:
        return None
    return versions[0].version


def _version_cache_path() -> Path:
    return cache_dir() / _VERSION_CACHE_FILENAME


def _load_version_cache_payload() -> dict:
    path = _version_cache_path()
    if not path.exists():
        return {"arches": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"arches": {}}
    if not isinstance(data, dict):
        return {"arches": {}}
    arches = data.get("arches")
    if not isinstance(arches, dict):
        data["arches"] = {}
    return data


def _write_version_cache_payload(payload: dict) -> None:
    path = _version_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def load_cached_embeddable_versions(arch: str) -> list[Version]:
    """Load cached embeddable versions for *arch* from disk plus built-in seeds."""
    if arch not in {"amd64", "win32"}:
        raise ValueError(f"Unsupported architecture: {arch!r}")
    payload = _load_version_cache_payload()
    arches = payload.get("arches", {})
    if not isinstance(arches, dict):
        return _seeded_embeddable_versions(arch)
    entry = arches.get(arch, {})
    if not isinstance(entry, dict):
        return _seeded_embeddable_versions(arch)
    raw_versions = entry.get("versions", [])
    if not isinstance(raw_versions, list):
        return _seeded_embeddable_versions(arch)

    out: list[Version] = []
    seen: set[Version] = set()
    for raw in raw_versions:
        if not isinstance(raw, str):
            continue
        try:
            v = Version.parse(raw)
        except ValueError:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return _merge_versions(_seeded_embeddable_versions(arch), out)


def store_cached_embeddable_versions(arch: str, versions: list[Version]) -> None:
    """Persist embeddable versions for *arch* to disk."""
    if arch not in {"amd64", "win32"}:
        raise ValueError(f"Unsupported architecture: {arch!r}")
    payload = _load_version_cache_payload()
    arches = payload.setdefault("arches", {})
    if not isinstance(arches, dict):
        arches = {}
        payload["arches"] = arches
    uniq_sorted = sorted(set(versions), reverse=True)
    arches[arch] = {
        "updated_at": int(time.time()),
        "versions": [str(v) for v in uniq_sorted],
    }
    _write_version_cache_payload(payload)


def refresh_embeddable_versions_cache(arch: str) -> list[Version]:
    """Fetch embeddable versions for *arch* and update disk cache."""
    versions = [c.version for c in list_stable_versions(arch=arch, embeddable_only=True)]
    versions = _merge_versions(_seeded_embeddable_versions(arch), versions)
    store_cached_embeddable_versions(arch, versions)
    return versions


def warm_embeddable_versions_cache(
    arches: tuple[str, ...] = ("amd64", "win32"),
) -> dict[str, list[Version]]:
    """Refresh and store embeddable version cache for all provided arches."""
    result: dict[str, list[Version]] = {}
    for arch in arches:
        result[arch] = refresh_embeddable_versions_cache(arch)
    return result
