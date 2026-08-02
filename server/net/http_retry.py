"""The one HTTP retry implementation (R1/R5).

Pure transport mechanics -- retry-with-backoff on 429/5xx, honoring
`Retry-After` when a server sends one. Deliberately carries no axis-specific
status interpretation: e-signature's 409-means-not-ready-yet and the LLM
router's 401/403/408/429/5xx-means-provider-unavailable are different
meanings for different axes, decided by each dispatcher on top of this, not
baked in here. This module only ever answers "was this worth retrying, and
did it eventually succeed" -- never "what does this status mean for my
domain."

Originally lived inside `server/providers/esign.py` as a private helper;
extracted here once the LLM router (M3) needed the identical mechanics, per
that module's own docstring: "one copy here is exactly one copy, not zero
and not two."
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

_RETRYABLE = (429, 500, 502, 503, 504)


class HTTPRetryError(RuntimeError):
    """A request failed and was not (or could not be) retried into success.

    `status_code` is None for a connection-level failure (DNS, refused,
    timeout -- no HTTP response at all was ever received). Callers inspect
    `status_code` to decide what it means for their own axis; this class
    itself makes no claim beyond "the request did not succeed."
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None, body: bytes = b""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: Optional[bytes] = None,
    timeout: int = 30,
    max_retries: int = 5,
    raw: bool = False,
) -> Any:
    """One HTTP call with retry on 429/5xx, honoring Retry-After when a
    server sends one and falling back to exponential backoff otherwise.
    Returns parsed JSON unless `raw=True`, in which case it returns the
    response bytes (e.g. a downloaded PDF, or a provider's raw response for
    an axis that wants to parse it itself).

    A non-retryable status (anything not in 429/500/502/503/504, including
    409 -- "not ready yet" is a real answer, not a transient failure) raises
    immediately, no retry attempted. A retryable status or connection error
    on the last attempt also raises. Every failure raises `HTTPRetryError`;
    callers translate `status_code` into whatever their axis's own
    exception hierarchy means by it.
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return body if raw else json.loads(body.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code not in _RETRYABLE or attempt == max_retries - 1:
                detail = body.decode("utf-8", "replace")[:500]
                raise HTTPRetryError(
                    f"{method} {url} failed: HTTP {exc.code}: {detail}",
                    status_code=exc.code, body=body,
                ) from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            last_error = exc
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt == max_retries - 1:
                raise HTTPRetryError(f"{method} {url} failed: {exc}") from exc
            last_error = exc
            time.sleep(min(2 ** attempt, 30))
    raise HTTPRetryError(f"{method} {url} failed after {max_retries} attempts") from last_error
