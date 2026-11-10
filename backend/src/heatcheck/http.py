"""Stdlib JSON client with retries, Retry-After, gzip and per-host throttling.

HTTPS only. Redirects are only followed on the same host since urllib would
send the API key header along to a new one. Response size is capped.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; heatcheck/1.0)"
# ESPN game summaries with play-by-play run a few MB; anything near this is wrong.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class HttpError(Exception):
    """A request failed after all retries, or failed in a way retrying can't fix."""

    def __init__(self, message: str, *, url: str, status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


class _Response:
    """The parts of a urllib response the client uses (lets tests fake it)."""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body


class SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if it stays on the same host over HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(absolute)
        if new.scheme != "https" or new.netloc != old.netloc:
            raise HttpError(
                f"refused redirect from {old.netloc} to {new.scheme}://{new.netloc}",
                url=req.full_url,
                status=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()  # verifies certificates and hostnames
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


_OPENER = urllib.request.build_opener(
    SameHostRedirectHandler,
    urllib.request.HTTPSHandler(context=tls_context()),
)


def _urllib_opener(request: urllib.request.Request, timeout: float) -> _Response:
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return _Response(resp.status, dict(resp.headers.items()), resp.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as err:
        body = err.read(MAX_RESPONSE_BYTES + 1) if err.fp is not None else b""
        return _Response(err.code, dict(err.headers.items()) if err.headers else {}, body)


Opener = Callable[[urllib.request.Request, float], _Response]


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.5,
        min_interval: Mapping[str, float] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = MAX_RESPONSE_BYTES,
        opener: Opener = _urllib_opener,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.min_interval = dict(min_interval or {})
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self._open = opener
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self.request_count = 0

    def _throttle(self, host: str) -> None:
        interval = self.min_interval.get(host, 0.0)
        last = self._last_request.get(host)
        if interval > 0 and last is not None:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """GET ``url`` and return ``(parsed_json, lowercase_response_headers)``.

        Never log request headers, they can contain the API key.
        """
        if urllib.parse.urlparse(url).scheme != "https":
            raise HttpError("only https:// URLs are allowed", url=url)
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        host = urllib.parse.urlparse(full_url).netloc
        req_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        last_error = "no attempts made"
        for attempt in range(self.retries + 1):
            self._throttle(host)
            self.request_count += 1
            request = urllib.request.Request(full_url, headers=req_headers, method="GET")  # noqa: S310 - https only
            try:
                resp = self._open(request, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                last_error = f"network error: {getattr(err, 'reason', err)}"
                retry_after = None
            else:
                if 200 <= resp.status < 300:
                    return self._decode(resp, full_url), resp.headers
                last_error = f"HTTP {resp.status}"
                if resp.status not in RETRYABLE_STATUS:
                    raise HttpError(f"GET {full_url} failed: {last_error}", url=full_url, status=resp.status)
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            if attempt < self.retries:
                delay = retry_after if retry_after is not None else self.backoff * (2**attempt)
                log.warning("GET %s failed (%s); retrying in %.1fs", full_url, last_error, delay)
                self._sleep(delay)
        raise HttpError(f"GET {full_url} failed after {self.retries + 1} attempts: {last_error}", url=full_url)

    def _decode(self, resp: _Response, url: str) -> Any:
        body = resp.body
        if len(body) > self.max_bytes:
            raise HttpError(f"GET {url} returned more than {self.max_bytes} bytes", url=url, status=resp.status)
        if resp.headers.get("content-encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = _gunzip(body, self.max_bytes, url)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise HttpError(f"GET {url} returned invalid JSON: {err}", url=url, status=resp.status) from err


def _gunzip(data: bytes, limit: int, url: str) -> bytes:
    """Decompress gzip without letting a tiny "zip bomb" expand past ``limit``."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, limit + 1)
    except zlib.error as err:
        raise HttpError(f"GET {url} returned a corrupt gzip body: {err}", url=url) from err
    if len(out) > limit or decompressor.unconsumed_tail:
        raise HttpError(f"GET {url} decompressed to more than {limit} bytes", url=url)
    return out


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), 60.0)
    except ValueError:
        return None
