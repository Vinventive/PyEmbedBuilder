"""
Hardened HTTP utilities.

All outbound requests enforce HTTPS, domain allowlisting, and TLS 1.2+.
"""
from __future__ import annotations

import urllib.request

from ..security import (
    audit,
    get_tls_context,
    validate_download_url,
    validate_trusted_source,
)


DEFAULT_UA = "PyEmbedBuilder/0.2 (secure-embedded-python-builder)"

# Maximum response body for non-streaming GET (50 MB)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


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


def http_get(
    url: str,
    *,
    timeout_s: float = 30.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    source_policy: str | None = None,
) -> bytes:
    """Perform an HTTPS GET with URL validation, TLS enforcement, and size limit."""
    req = _build_request(url, source_policy=source_policy)
    audit("http_get", url=url)

    with _open_url(req, timeout_s=timeout_s, source_policy=source_policy) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(
                f"Response from {url} exceeds maximum allowed size "
                f"({max_bytes:,} bytes)."
            )
        return data


def http_open_stream(
    url: str,
    *,
    timeout_s: float = 120.0,
    source_policy: str | None = None,
):
    """Open a streaming HTTPS GET connection (caller manages the context).

    Returns the response object (use as context manager).
    """
    req = _build_request(url, source_policy=source_policy)
    audit("http_stream_open", url=url)
    return _open_url(req, timeout_s=timeout_s, source_policy=source_policy)
