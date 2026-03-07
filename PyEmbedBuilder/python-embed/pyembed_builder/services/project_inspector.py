"""
Project source inspection helpers for setup-time metadata detection.
"""
from __future__ import annotations

import hashlib
import re
import os
import stat
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from ..security import normalize_project_git_source, validate_project_source_url
from ..util.paths import cache_dir
from ..util.versioning import Version
from .downloader import download_file
from .extractor import extract_zip
from .python_catalog import list_stable_versions, resolve_embeddable_at_or_above
from .subprocess_runner import run_command_stream


LogCb = Callable[[str], None]

_SPEC_PART_RE = re.compile(
    r"^(?P<op><=|>=|==|!=|<|>|~=)\s*"
    r"(?P<ver>\d+(?:\.\d+){0,2})"
    r"(?P<wildcard>\.\*)?$"
)

_REQ_COMMENT_RE = re.compile(r"\s+#.*$")
_REQ_SKIP_PREFIXES = (
    "-r ",
    "--requirement ",
    "-c ",
    "--constraint ",
    "--find-links ",
    "-f ",
    "--index-url ",
    "--extra-index-url ",
    "--trusted-host ",
)
_REQ_SKIP_EXACT = {"-e", "--editable"}

_REQUIREMENTS_CANDIDATES: tuple[str, ...] = (
    "requirements.txt",
    "requirements/base.txt",
    "requirements/main.txt",
    "requirements/prod.txt",
    "requirements-dev.txt",
)


@dataclass(frozen=True)
class ProjectSourceAnalysis:
    mode: str
    source_root: Path
    requirements_file: Path | None
    pyproject_file: Path | None
    dependency_source: str  # "requirements" | "pyproject" | "none"
    detected_dependencies: tuple[str, ...]
    requires_python: str
    requested_python: Version | None
    suggested_python: Version | None
    python_note: str

    @property
    def requirements_rel(self) -> str:
        if not self.requirements_file:
            return ""
        try:
            return str(self.requirements_file.resolve().relative_to(self.source_root.resolve()))
        except Exception:
            return self.requirements_file.name

    @property
    def pyproject_rel(self) -> str:
        if not self.pyproject_file:
            return ""
        try:
            return str(self.pyproject_file.resolve().relative_to(self.source_root.resolve()))
        except Exception:
            return self.pyproject_file.name


def _cache_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _rmtree_onerror(func, path: str, _exc_info) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:
        pass
    try:
        func(path)
    except Exception:
        pass


def _wipe_dir(path: Path, *, retries: int = 4, delay_s: float = 0.12) -> None:
    if not path.exists():
        return
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            shutil.rmtree(path, onerror=_rmtree_onerror)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(delay_s)
    if path.exists():
        raise RuntimeError(f"Failed to remove directory: {path}") from last_exc


def _reset_git_analysis_cache_dir(path: Path) -> None:
    _wipe_dir(path)
    path.mkdir(parents=True, exist_ok=True)


def _prepare_git_snapshot(
    *,
    source_url: str,
    source_ref: str,
    log_cb: LogCb,
    cancel_event: threading.Event | None,
) -> Path:
    url = normalize_project_git_source(source_url)
    validate_project_source_url(url, "git")

    key = _cache_key(f"{url}|{source_ref}")
    root = cache_dir() / "source_analysis" / "git" / key
    log_cb("Resetting cached Git analysis source for this attempt...")
    _reset_git_analysis_cache_dir(root)
    clone_dir = root / "_clone"

    source_ref = source_ref.strip()
    if source_ref:
        log_cb(f"Analyzing Git source: cloning ref '{source_ref}' from {url}")
        try:
            run_command_stream(
                ["git", "clone", "--depth", "1", "--branch", source_ref, url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
        except Exception:
            log_cb("Shallow ref clone failed; retrying full clone + checkout for analysis.")
            _reset_git_analysis_cache_dir(root)
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
                    f"Failed to checkout Git ref '{source_ref}' during analysis. "
                    "Verify the branch, tag, or commit exists and is accessible.\n\n"
                    f"{exc}"
                ) from exc
    else:
        log_cb(f"Analyzing Git source: cloning {url}")
        try:
            run_command_stream(
                ["git", "clone", "--depth", "1", url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
        except Exception:
            log_cb("Shallow clone failed; retrying full clone for analysis.")
            _reset_git_analysis_cache_dir(root)
            run_command_stream(
                ["git", "clone", url, str(clone_dir)],
                log_cb=log_cb,
                cancel_event=cancel_event,
            )

    return clone_dir


def _prepare_zip_snapshot(*, source_url: str, log_cb: LogCb) -> Path:
    validate_project_source_url(source_url, "zip")
    key = _cache_key(source_url)
    root = cache_dir() / "source_analysis" / "zip" / key
    zip_path = root / "source.zip"
    extract_dir = root / "extract"
    root.mkdir(parents=True, exist_ok=True)

    log_cb(f"Analyzing ZIP source: downloading {source_url}")
    download_file(
        source_url,
        zip_path,
        source_policy="project_zip",
    )

    shutil.rmtree(extract_dir, ignore_errors=True)
    extract_zip(zip_path, extract_dir)

    entries = [p for p in extract_dir.iterdir() if p.exists()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def _normalize_dep(dep: str) -> str:
    return dep.strip()


def _parse_requirements_packages(path: Path) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _REQ_COMMENT_RE.sub("", line).strip()
        if not line:
            continue
        lower = line.lower()
        if lower in _REQ_SKIP_EXACT:
            continue
        if any(lower.startswith(prefix) for prefix in _REQ_SKIP_PREFIXES):
            continue
        token = _normalize_dep(line)
        if not token:
            continue
        if token not in seen:
            seen.add(token)
            items.append(token)
    return tuple(items)


def find_requirements_file(source_root: Path) -> Path | None:
    for rel in _REQUIREMENTS_CANDIDATES:
        p = source_root / rel
        if p.exists() and p.is_file():
            return p

    root_matches = sorted(source_root.glob("requirements*.txt"))
    if root_matches:
        return root_matches[0]

    req_dir = source_root / "requirements"
    if req_dir.exists() and req_dir.is_dir():
        sub_matches = sorted(req_dir.glob("*.txt"))
        if sub_matches:
            return sub_matches[0]
    return None


def _poetry_dep_to_pip(name: str, raw: object) -> str:
    if isinstance(raw, str):
        spec = raw.strip()
        if not spec or spec == "*":
            return name
        if spec[0].isdigit():
            return f"{name}=={spec}"
        if spec.startswith((">", "<", "=", "!", "~")):
            # '^' and '~' constraints are Poetry-only; install by name in that case.
            if spec.startswith("^"):
                return name
            return f"{name}{spec}"
        return name
    if isinstance(raw, dict):
        version = raw.get("version")
        if isinstance(version, str):
            return _poetry_dep_to_pip(name, version)
        return name
    return name


def _parse_pyproject(
    pyproject_path: Path,
) -> tuple[tuple[str, ...], str]:
    if tomllib is None:
        return (), ""

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return (), ""

    deps: list[str] = []
    requires_python = ""

    project = data.get("project")
    if isinstance(project, dict):
        rp = project.get("requires-python")
        if isinstance(rp, str):
            requires_python = rp.strip()
        raw_deps = project.get("dependencies")
        if isinstance(raw_deps, list):
            for item in raw_deps:
                if isinstance(item, str) and item.strip():
                    deps.append(item.strip())

    # Optional Poetry fallback for non-PEP621 projects.
    if not deps:
        tool = data.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict):
                poetry_deps = poetry.get("dependencies")
                if isinstance(poetry_deps, dict):
                    py_spec = poetry_deps.get("python")
                    if not requires_python and isinstance(py_spec, str):
                        requires_python = py_spec.strip()
                    for name, raw in poetry_deps.items():
                        if not isinstance(name, str):
                            continue
                        if name.strip().lower() == "python":
                            continue
                        dep = _poetry_dep_to_pip(name.strip(), raw).strip()
                        if dep:
                            deps.append(dep)

    uniq: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        if dep in seen:
            continue
        seen.add(dep)
        uniq.append(dep)
    return tuple(uniq), requires_python


def detect_pyproject_dependencies(source_root: Path) -> tuple[str, ...]:
    """Return installable dependency strings from pyproject.toml when available."""
    pyproject = source_root / "pyproject.toml"
    if not pyproject.exists() or not pyproject.is_file():
        return ()
    deps, _ = _parse_pyproject(pyproject)
    return deps


def detect_requires_python(source_root: Path) -> str:
    """Return requires-python style metadata from pyproject.toml when available."""
    pyproject = source_root / "pyproject.toml"
    if not pyproject.exists() or not pyproject.is_file():
        return ""
    _, spec = _parse_pyproject(pyproject)
    return spec


def _parse_version_parts(raw: str) -> tuple[int, ...]:
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if not parts:
        raise ValueError(f"Invalid version: {raw!r}")
    nums = tuple(int(p) for p in parts)
    if len(nums) > 3:
        raise ValueError(f"Invalid version: {raw!r}")
    return nums


def _to_version(parts: tuple[int, ...]) -> Version:
    if len(parts) == 1:
        return Version(parts[0], 0, 0)
    if len(parts) == 2:
        return Version(parts[0], parts[1], 0)
    return Version(parts[0], parts[1], parts[2])


def _compatible_upper(parts: tuple[int, ...]) -> Version:
    if len(parts) <= 2:
        return Version(parts[0] + 1, 0, 0)
    return Version(parts[0], parts[1] + 1, 0)


def _matches_token(version: Version, token: str) -> bool:
    m = _SPEC_PART_RE.match(token)
    if not m:
        return True

    op = m.group("op")
    parts = _parse_version_parts(m.group("ver"))
    wildcard = bool(m.group("wildcard"))

    if wildcard and op not in {"==", "!="}:
        wildcard = False

    if wildcard:
        check = (version.major, version.minor)
        target = (parts[0], parts[1] if len(parts) > 1 else 0)
        is_match = check == target
        return is_match if op == "==" else (not is_match)

    target = _to_version(parts)
    if op == "==":
        return version == target
    if op == "!=":
        return version != target
    if op == ">=":
        return version >= target
    if op == "<=":
        return version <= target
    if op == ">":
        return version > target
    if op == "<":
        return version < target
    if op == "~=":
        upper = _compatible_upper(parts)
        return version >= target and version < upper
    return True


def _matches_spec(version: Version, spec: str) -> bool:
    if not spec.strip():
        return True
    tokens = [tok.strip() for tok in spec.split(",") if tok.strip()]
    if not tokens:
        return True
    return all(_matches_token(version, token) for token in tokens)


def _minimum_candidate(spec: str) -> Version | None:
    token_specs = [tok.strip() for tok in spec.split(",") if tok.strip()]
    candidates: list[Version] = []
    for token in token_specs:
        m = _SPEC_PART_RE.match(token)
        if not m:
            continue
        op = m.group("op")
        parts = _parse_version_parts(m.group("ver"))
        wildcard = bool(m.group("wildcard"))
        if op in {">=", "==", "~="}:
            if wildcard and op == "==":
                candidates.append(_to_version((parts[0], parts[1] if len(parts) > 1 else 0, 0)))
            else:
                candidates.append(_to_version(parts))
    if not candidates:
        return None
    return max(candidates)


def _suggest_python_for_spec(
    requires_python: str,
    *,
    arch: str,
    preferred: Version,
) -> tuple[Version | None, Version | None, str]:
    available_desc = [c.version for c in list_stable_versions(arch=arch, embeddable_only=True)]
    if not available_desc:
        return None, None, "No embeddable versions available for this architecture."

    requested = _minimum_candidate(requires_python)
    available = sorted(available_desc)

    matches = [v for v in available if _matches_spec(v, requires_python)]
    if matches:
        # Use newest version that still satisfies project requirement.
        chosen = matches[-1]
        return requested, chosen, f"Matched requires-python ({requires_python})."

    if requested is not None:
        fallback = resolve_embeddable_at_or_above(requested, arch)
        if fallback is not None:
            return (
                requested,
                fallback,
                f"No exact embeddable match for {requires_python}; using nearest higher {fallback}.",
            )

    pref = resolve_embeddable_at_or_above(preferred, arch)
    if pref is not None:
        return requested, pref, "Used preferred embeddable default."
    return requested, available_desc[0], "Used newest available embeddable version."


def analyze_project_tree(
    source_root: Path,
    *,
    mode: str,
    arch: str,
    preferred_version: Version,
) -> ProjectSourceAnalysis:
    source_root = source_root.resolve()
    req_file = find_requirements_file(source_root)

    pyproject = source_root / "pyproject.toml"
    pyproject_file = pyproject if pyproject.exists() and pyproject.is_file() else None

    pyproject_deps: tuple[str, ...] = ()
    requires_python = ""
    if pyproject_file is not None:
        pyproject_deps, requires_python = _parse_pyproject(pyproject_file)

    dependency_source = "none"
    detected_dependencies: tuple[str, ...] = ()
    if req_file is not None:
        dependency_source = "requirements"
        detected_dependencies = _parse_requirements_packages(req_file)
    elif pyproject_deps:
        dependency_source = "pyproject"
        detected_dependencies = pyproject_deps

    requested_python: Version | None = None
    suggested_python: Version | None = None
    python_note = "No python requirement found; using selected/default version."
    if requires_python:
        requested_python, suggested_python, python_note = _suggest_python_for_spec(
            requires_python,
            arch=arch,
            preferred=preferred_version,
        )

    return ProjectSourceAnalysis(
        mode=mode,
        source_root=source_root,
        requirements_file=req_file,
        pyproject_file=pyproject_file,
        dependency_source=dependency_source,
        detected_dependencies=detected_dependencies,
        requires_python=requires_python,
        requested_python=requested_python,
        suggested_python=suggested_python,
        python_note=python_note,
    )


def analyze_project_source(
    *,
    mode: str,
    source_path: Path | None,
    source_url: str,
    source_ref: str,
    arch: str,
    preferred_version: Version,
    log_cb: LogCb | None = None,
    cancel_event: threading.Event | None = None,
) -> ProjectSourceAnalysis:
    """Inspect the selected project source before build starts."""

    def _log(line: str) -> None:
        if log_cb:
            log_cb(line)

    mode = mode.strip().lower()
    if mode == "create":
        return ProjectSourceAnalysis(
            mode=mode,
            source_root=Path("."),
            requirements_file=None,
            pyproject_file=None,
            dependency_source="none",
            detected_dependencies=(),
            requires_python="",
            requested_python=None,
            suggested_python=None,
            python_note="Create mode has no source metadata yet.",
        )

    if mode == "import":
        if source_path is None:
            raise ValueError("Source path is required for import mode.")
        return analyze_project_tree(
            source_path,
            mode=mode,
            arch=arch,
            preferred_version=preferred_version,
        )

    if mode == "git":
        if not source_url.strip():
            raise ValueError("Repository URL is required for git mode.")
        root = _prepare_git_snapshot(
            source_url=source_url.strip(),
            source_ref=source_ref.strip(),
            log_cb=_log,
            cancel_event=cancel_event,
        )
        return analyze_project_tree(
            root,
            mode=mode,
            arch=arch,
            preferred_version=preferred_version,
        )

    if mode == "zip":
        if not source_url.strip():
            raise ValueError("ZIP URL is required for zip mode.")
        root = _prepare_zip_snapshot(
            source_url=source_url.strip(),
            log_cb=_log,
        )
        return analyze_project_tree(
            root,
            mode=mode,
            arch=arch,
            preferred_version=preferred_version,
        )

    raise ValueError(f"Unsupported project mode: {mode!r}")
