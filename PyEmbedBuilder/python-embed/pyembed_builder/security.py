"""
Security utilities for PyEmbedBuilder.

Provides URL allowlisting, input sanitization, TLS enforcement,
secure zip extraction validation, and audit logging.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .util.paths import logs_dir, project_root


# ── URL Allowlist ─────────────────────────────────────────────────────────

ALLOWED_DOWNLOAD_DOMAINS: frozenset[str] = frozenset({
    "www.python.org",
    "python.org",
    "bootstrap.pypa.io",
})

ALLOWED_PROJECT_SOURCE_DOMAINS: frozenset[str] = frozenset({
    "github.com",
    "codeload.github.com",
    "gitlab.com",
    "bitbucket.org",
})

ALLOWED_DOWNLOAD_DOMAINS = frozenset(
    set(ALLOWED_DOWNLOAD_DOMAINS) | set(ALLOWED_PROJECT_SOURCE_DOMAINS)
)


_GIT_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_GIT_PATH_RE = re.compile(r"^[A-Za-z0-9._~+%/-]+$")
_SENSITIVE_QS_KEY_RE = re.compile(
    r"(token|secret|pass|password|pwd|auth|api[_-]?key|signature|sig)",
    re.IGNORECASE,
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SCP_IN_TEXT_RE = re.compile(
    r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+(?:\.git)?\b"
)


def validate_download_url(url: str) -> None:
    """Raise ValueError if *url* is not HTTPS or outside the domain allowlist."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(
            f"Only HTTPS URLs are permitted. Got scheme: {parsed.scheme!r}"
        )
    hostname = (parsed.hostname or "").lower().strip(".")
    if hostname not in ALLOWED_DOWNLOAD_DOMAINS:
        raise ValueError(
            f"Domain {hostname!r} is not in the allowed list: "
            f"{', '.join(sorted(ALLOWED_DOWNLOAD_DOMAINS))}"
        )


def _validate_git_repo_path(path: str) -> None:
    path = path.strip("/")
    if not path:
        raise ValueError("Git repository path cannot be empty.")
    if ".." in path.split("/"):
        raise ValueError("Git repository path must not contain '..'.")
    if path.count("/") < 1:
        raise ValueError("Git repository path must be at least owner/repo.")
    if not _GIT_PATH_RE.fullmatch(path):
        raise ValueError("Git repository path contains unsupported characters.")


def _redact_single_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    # Remove userinfo (tokens/passwords in URL authority).
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"

    safe_query_items: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if _SENSITIVE_QS_KEY_RE.search(k):
            safe_query_items.append((k, "REDACTED"))
        else:
            safe_query_items.append((k, v))
    safe_query = urlencode(safe_query_items, doseq=True)

    # Fragments are not needed for transport; drop them to avoid leaks.
    return urlunparse(
        (
            parsed.scheme,
            host,
            parsed.path,
            parsed.params,
            safe_query,
            "",
        )
    )


def redact_url_secrets(value: str) -> str:
    """Redact credentials/tokens from URL-like text."""
    value = _URL_IN_TEXT_RE.sub(lambda m: _redact_single_url(m.group(0)), value)

    # Redact scp-style userinfo when it is not the standard 'git' user.
    def _scp_repl(m: re.Match[str]) -> str:
        txt = m.group(0)
        user, rest = txt.split("@", 1)
        if user.lower() == "git":
            return txt
        return f"<redacted>@{rest}"

    return _SCP_IN_TEXT_RE.sub(_scp_repl, value)


@dataclass(frozen=True)
class SourcePolicy:
    """Strict URL policy for known trusted artifact sources."""
    hosts: frozenset[str]
    path_re: re.Pattern[str]
    allow_query: bool = False


_SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "python_ftp_index": SourcePolicy(
        hosts=frozenset({"python.org", "www.python.org"}),
        path_re=re.compile(r"^/ftp/python/$"),
    ),
    "python_ftp_release_index": SourcePolicy(
        hosts=frozenset({"python.org", "www.python.org"}),
        path_re=re.compile(r"^/ftp/python/\d+\.\d+\.\d+/$"),
    ),
    "python_embed_zip": SourcePolicy(
        hosts=frozenset({"python.org", "www.python.org"}),
        path_re=re.compile(
            r"^/ftp/python/\d+\.\d+\.\d+/python-\d+\.\d+\.\d+-embed-(amd64|win32)\.zip$",
            re.IGNORECASE,
        ),
    ),
    "python_msi": SourcePolicy(
        hosts=frozenset({"python.org", "www.python.org"}),
        path_re=re.compile(
            r"^/ftp/python/\d+\.\d+\.\d+/(amd64|win32|arm64)/(core|lib|tcltk|dev|tools)\.msi$",
            re.IGNORECASE,
        ),
    ),
    "get_pip_script": SourcePolicy(
        hosts=frozenset({"bootstrap.pypa.io"}),
        path_re=re.compile(r"^/(?:pip/)?get-pip\.py$", re.IGNORECASE),
    ),
    "project_zip": SourcePolicy(
        hosts=ALLOWED_PROJECT_SOURCE_DOMAINS,
        path_re=re.compile(r"^/(?:.+\.zip|.+/zip/.+)$", re.IGNORECASE),
        allow_query=True,
    ),
    "project_git": SourcePolicy(
        hosts=ALLOWED_PROJECT_SOURCE_DOMAINS,
        path_re=re.compile(r"^/.+$", re.IGNORECASE),
    ),
}


def validate_trusted_source(url: str, policy: str) -> None:
    """Validate URL against a strict trusted-source policy.

    This is intentionally stronger than domain allowlisting: each policy
    constrains both host and path shape for specific artifact families.
    """
    validate_download_url(url)
    spec = _SOURCE_POLICIES.get(policy)
    if spec is None:
        raise ValueError(f"Unknown trusted-source policy: {policy!r}")

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().strip(".")
    path = parsed.path or "/"

    if hostname not in spec.hosts:
        raise ValueError(
            f"Host {hostname!r} is not allowed for policy {policy!r}. "
            f"Expected one of: {', '.join(sorted(spec.hosts))}"
        )
    if not spec.path_re.fullmatch(path):
        raise ValueError(
            f"Path {path!r} is not allowed for policy {policy!r}."
        )
    if not spec.allow_query and (parsed.query or parsed.fragment):
        raise ValueError(
            f"Query/fragment is not allowed for policy {policy!r}."
        )


def validate_project_source_url(url: str, source_type: str) -> None:
    """Validate user-supplied open-source project URLs."""
    if source_type not in {"git", "zip"}:
        raise ValueError(f"Unsupported project source type: {source_type!r}")
    if source_type == "zip":
        validate_trusted_source(url, "project_zip")
        return

    # git: accept any HTTPS/SSH host, with strict path-shape checks.
    normalized = normalize_project_git_source(url)

    # HTTPS URL: https://host/owner/repo(.git)
    parsed = urlparse(normalized)
    if parsed.scheme.lower() == "https":
        host = (parsed.hostname or "").lower().strip(".")
        if not host or not _GIT_HOST_RE.fullmatch(host):
            raise ValueError("Git host is missing or invalid.")
        if parsed.query or parsed.fragment:
            raise ValueError("Git HTTPS URL must not include query or fragment.")
        _validate_git_repo_path(parsed.path)
        return

    # SSH scp-like: user@host:owner/repo(.git)
    scp_like = re.fullmatch(
        r"(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._~+%/-]+?)(?:\.git)?/?",
        normalized,
        flags=re.IGNORECASE,
    )
    if scp_like:
        host = (scp_like.group("host") or "").lower().strip(".")
        path = (scp_like.group("path") or "").strip("/")
        if not host or not _GIT_HOST_RE.fullmatch(host):
            raise ValueError("Git host is missing or invalid.")
        _validate_git_repo_path(path)
        return

    # SSH URL: ssh://user@host[:port]/owner/repo(.git)
    ssh_url = re.fullmatch(
        r"ssh://(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+)(?::\d{1,5})?/(?P<path>[A-Za-z0-9._~+%/-]+?)(?:\.git)?/?",
        normalized,
        flags=re.IGNORECASE,
    )
    if ssh_url:
        host = (ssh_url.group("host") or "").lower().strip(".")
        path = (ssh_url.group("path") or "").strip("/")
        if not host or not _GIT_HOST_RE.fullmatch(host):
            raise ValueError("Git host is missing or invalid.")
        _validate_git_repo_path(path)
        return

    raise ValueError(
        "Unsupported Git source format.\n"
        "Use one of:\n"
        "  - https://<host>/<owner>/<repo>(.git)\n"
        "  - <user>@<host>:<owner>/<repo>(.git)\n"
        "  - ssh://<user>@<host>/<owner>/<repo>(.git)\n"
        "  - owner/repo\n"
        "  - gh repo clone owner/repo"
    )


def normalize_project_git_source(value: str) -> str:
    """Normalize user-entered Git source values to clone-ready form.

    Supported shorthand inputs:
    - owner/repo                        -> https://github.com/owner/repo
    - github.com/owner/repo             -> https://github.com/owner/repo
    - gh repo clone owner/repo [target] -> https://github.com/owner/repo
    """
    s = value.strip()
    if not s:
        return s

    if s.lower().startswith("gh "):
        try:
            parts = shlex.split(s)
        except ValueError as exc:
            raise ValueError(f"Invalid GitHub CLI input: {exc}") from exc
        if len(parts) >= 4 and parts[0].lower() == "gh" and parts[1].lower() == "repo" and parts[2].lower() == "clone":
            s = parts[3].strip()
        else:
            raise ValueError(
                "Unsupported GitHub CLI format. Use: gh repo clone owner/repo"
            )

    if s.lower().startswith("github.com/"):
        return f"https://{s}"

    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s):
        return f"https://github.com/{s}"

    return s


# ── Input Sanitization ───────────────────────────────────────────────────

_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def sanitize_env_name(name: str) -> str:
    """Validate and return a safe environment name.

    Rules:
    - 1-128 characters
    - Starts with alphanumeric
    - Contains only A-Z, a-z, 0-9, '.', '-', '_'
    - No path traversal patterns
    """
    name = name.strip()
    if not name:
        raise ValueError("Environment name cannot be empty.")
    if ".." in name:
        raise ValueError("Environment name must not contain '..'.")
    if not _SAFE_ENV_NAME_RE.match(name):
        raise ValueError(
            "Environment name must be 1-128 characters, start with a letter "
            "or digit, and contain only letters, digits, '.', '-', or '_'."
        )
    return name


def validate_target_path(target: Path) -> Path:
    """Validate that *target* is safe to write into.

    Returns the resolved path. Raises ValueError for system-critical locations.
    """
    resolved = target.resolve()

    # Reject system-critical directories
    for env_key in ("SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        val = os.environ.get(env_key)
        if not val:
            continue
        critical = Path(val).resolve()
        try:
            resolved.relative_to(critical)
        except ValueError:
            continue  # not under this root → safe
        raise ValueError(
            f"Cannot create environments inside system directory: {critical}"
        )

    return resolved


def validate_zip_entry(member_name: str, dest_dir: Path) -> Path:
    """Validate a zip member name is safe (no path traversal / zip-slip).

    Returns the resolved target path. Raises ValueError on unsafe entries.
    """
    # Reject absolute paths
    if member_name.startswith(("/", "\\")):
        raise ValueError(f"Zip member has absolute path: {member_name!r}")

    # Reject path traversal
    parts_fwd = member_name.replace("\\", "/").split("/")
    if ".." in parts_fwd:
        raise ValueError(f"Zip member contains path traversal: {member_name!r}")

    target = (dest_dir / member_name).resolve()
    base = dest_dir.resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(
            f"Zip member would escape extraction directory: {member_name!r}"
        ) from None

    return target


# ── TLS Context ───────────────────────────────────────────────────────────

_tls_context: ssl.SSLContext | None = None


def get_tls_context() -> ssl.SSLContext:
    """Return a strict TLS context (TLS 1.2+, hostname verification, cert required)."""
    global _tls_context
    if _tls_context is None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        _tls_context = ctx
    return _tls_context


# ── Audit Logging ─────────────────────────────────────────────────────────

_audit_logger: logging.Logger | None = None
_audit_env_root: Path | None = None


_ABS_PATH_RE = re.compile(r"^[A-Za-z]:\\|^\\\\|^/")


def _relativize_path(value: str) -> str:
    """Make absolute paths relative to the project root (when possible)."""
    value = redact_url_secrets(value)
    if "://" in value:
        return value
    if not _ABS_PATH_RE.match(value):
        return value
    try:
        p = Path(value).resolve()
    except Exception:
        return value

    # Prefer environment root when set (built project portability)
    if _audit_env_root:
        try:
            rel = p.relative_to(_audit_env_root.resolve())
            return f".\\{rel}" if str(rel) else "."
        except Exception:
            pass

    try:
        rel = p.relative_to(project_root().resolve())
        return f".\\{rel}" if str(rel) else "."
    except Exception:
        return value


def sanitize_source_path_for_manifest(path: Path | None) -> str:
    """Return a privacy-preserving source path for build manifests."""
    if path is None:
        return ""
    try:
        p = path.resolve()
    except Exception:
        try:
            return Path(path).name
        except Exception:
            return ""
    try:
        rel = p.relative_to(project_root().resolve())
        return str(rel) if str(rel) else "."
    except Exception:
        # Keep useful context without leaking absolute machine paths.
        return p.name


def set_audit_env_root(path: Path | None) -> None:
    """Set (or clear) the environment root used for audit path relativization."""
    global _audit_env_root
    _audit_env_root = path.resolve() if path else None


def get_audit_logger() -> logging.Logger:
    """Get or create the security audit file logger."""
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    logger = logging.getLogger("pyembed_builder.security")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = logs_dir() / "security_audit.log"
    handler = logging.FileHandler(str(log_path), encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    _audit_logger = logger
    return logger


def audit(event: str, **kw: str) -> None:
    """Write a security audit log entry."""
    safe_kw = {k: _relativize_path(str(v)) for k, v in kw.items()}
    detail = " | ".join(f"{k}={v}" for k, v in safe_kw.items())
    msg = f"{event} | {detail}" if detail else event
    get_audit_logger().info(msg)
