import gzip
import json
import urllib.error

import pytest

from heatcheck.http import HttpClient, HttpError, _Response


class ScriptedOpener:
    """Returns the scripted responses (or raises the scripted errors) in order."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def ok(payload, headers=None, *, compress=False):
    body = json.dumps(payload).encode()
    headers = dict(headers or {})
    if compress:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return _Response(200, headers, body)


def make_client(opener, **kwargs):
    sleeps = []
    client = HttpClient(opener=opener, sleep=sleeps.append, clock=lambda: 0.0, **kwargs)
    return client, sleeps


def test_get_json_returns_payload_and_lowercase_headers():
    opener = ScriptedOpener(ok({"a": 1}, {"X-RateLimit-Requests-Remaining": "42"}))
    client, _ = make_client(opener)
    data, headers = client.get_json("https://example.test/api", params={"date": "2026-11-05"})
    assert data == {"a": 1}
    assert headers["x-ratelimit-requests-remaining"] == "42"
    assert opener.requests[0].full_url == "https://example.test/api?date=2026-11-05"
    assert opener.requests[0].get_header("User-agent").startswith("Mozilla/5.0")


def test_gzip_bodies_are_decoded():
    client, _ = make_client(ScriptedOpener(ok({"zipped": True}, compress=True)))
    assert client.get_json("https://example.test/x")[0] == {"zipped": True}


def test_retries_transient_errors_with_backoff():
    opener = ScriptedOpener(_Response(503, {}, b""), urllib.error.URLError("reset"), ok({"ok": True}))
    client, sleeps = make_client(opener, retries=3, backoff=1.0)
    assert client.get_json("https://example.test/x")[0] == {"ok": True}
    assert sleeps == [1.0, 2.0]
    assert client.request_count == 3


def test_honors_retry_after_on_429():
    opener = ScriptedOpener(_Response(429, {"Retry-After": "7"}, b""), ok({}))
    client, sleeps = make_client(opener)
    client.get_json("https://example.test/x")
    assert sleeps == [7.0]


def test_non_retryable_status_fails_fast():
    opener = ScriptedOpener(_Response(404, {}, b"not found"))
    client, sleeps = make_client(opener)
    with pytest.raises(HttpError) as excinfo:
        client.get_json("https://example.test/x")
    assert excinfo.value.status == 404
    assert sleeps == []


def test_gives_up_after_all_retries():
    opener = ScriptedOpener(*[_Response(500, {}, b"")] * 3)
    client, _ = make_client(opener, retries=2)
    with pytest.raises(HttpError, match="after 3 attempts"):
        client.get_json("https://example.test/x")


def test_invalid_json_is_an_http_error():
    client, _ = make_client(ScriptedOpener(_Response(200, {}, b"<html>")))
    with pytest.raises(HttpError, match="invalid JSON"):
        client.get_json("https://example.test/x")


def test_throttles_requests_per_host():
    times = iter([0.0, 1.0, 1.0, 1.0])  # clock reads: a, b (check), b (stamp), c
    sleeps = []
    client = HttpClient(
        opener=ScriptedOpener(ok({}), ok({}), ok({})),
        sleep=sleeps.append,
        clock=lambda: next(times),
        min_interval={"slow.test": 6.5},
    )
    client.get_json("https://slow.test/a")
    client.get_json("https://slow.test/b")  # 1s after the first -> wait 5.5s
    client.get_json("https://fast.test/c")  # different host, no wait
    assert sleeps == [5.5]
