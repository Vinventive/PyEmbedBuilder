"""
Hardened HTTP utilities.

All outbound requests enforce HTTPS, domain allowlisting, and TLS 1.2+.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

from ..security import (
    audit,
    get_tls_context,
    validate_download_url,
    validate_trusted_source,
)


DEFAULT_UA = "PyEmbedBuilder/1.0 (secure-embedded-python-builder)"

# Maximum response body for non-streaming GET (50 MB)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024

DEFAULT_RETRIES = 3
RETRY_BACKOFF_BASE_S = 1.5
_TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against URL/source policies."""

    def __init__(self, source_policy: str | None) -> None:
        super().__init__()
        self._source_policy = source_policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._source_policy:
            validate_trusted_source(newurl, self._source_policy)
        else:
            validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_request(
    url: str,
    method: str = "GET",
    *,
    source_policy: str | None = None,
) -> urllib.request.Request:
    """Build a urllib Request after validating URL/domain/source policy."""
    if source_policy:
        validate_trusted_source(url, source_policy)
    else:
        validate_download_url(url)
    return urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_UA, "Accept": "*/*"},
        method=method,
    )


def _open_url(
    req: urllib.request.Request,
    *,
    timeout_s: float,
    source_policy: str | None,
):
    ctx = get_tls_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _PolicyRedirectHandler(source_policy),
    )
    return opener.open(req, timeout=timeout_s)


def _is_transient(exc: Exception) -> bool:
    """Return True if the exception looks like a transient network error."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in _TRANSIENT_HTTP_CODES:
        return True
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError, ConnectionError)):
        return True
    return False


def http_get(
    url: str,
    *,
    timeout_s: float = 30.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    source_policy: str | None = None,
    retries: int = DEFAULT_RETRIES,
    audit_level: str = "INFO",
) -> bytes:
    """Perform an HTTPS GET with URL validation, TLS enforcement, size limit, and retry."""
    req = _build_request(url, source_policy=source_policy)
    audit("http_get", level=audit_level, url=url)

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with _open_url(req, timeout_s=timeout_s, source_policy=source_policy) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise RuntimeError(
                        f"Response from {url} exceeds maximum allowed size "
                        f"({max_bytes:,} bytes)."
                    )
                return data
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_transient(exc):
                delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                audit("http_retry", url=url, attempt=str(attempt), delay_s=f"{delay:.1f}")
                time.sleep(delay)
                req = _build_request(url, source_policy=source_policy)
                continue
            raise
    raise last_exc  # unreachable but satisfies type checker


def http_open_stream(
    url: str,
    *,
    timeout_s: float = 120.0,
    source_policy: str | None = None,
    retries: int = DEFAULT_RETRIES,
):
    """Open a streaming HTTPS GET connection with retry for transient errors."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = _build_request(url, source_policy=source_policy)
            if attempt == 1:
                audit("http_stream_open", url=url)
            return _open_url(req, timeout_s=timeout_s, source_policy=source_policy)
        except Exception as exc:
            last_exc = exc
            if attempt < retries and _is_transient(exc):
                delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                audit("http_retry", url=url, attempt=str(attempt), delay_s=f"{delay:.1f}")
                time.sleep(delay)
                continue
            raise
    raise last_exc
