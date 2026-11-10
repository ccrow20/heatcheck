"""Tests for the security properties: secrets stay hidden, the HTTP client stays strict."""

import gzip
import json
import logging
import ssl
import urllib.request

import pytest

from heatcheck import cli
from heatcheck import handler as handler_module
from heatcheck.config import Config, read_secret_parameter
from heatcheck.http import HttpClient, HttpError, SameHostRedirectHandler, _Response, tls_context
from heatcheck.security import REDACTED, RedactingFilter, install_redaction

SECRET = "sk-live-0123456789abcdef"  # gitleaks:allow (fake value used by the tests)


def client_returning(body: bytes, headers=None, **kwargs):
    return HttpClient(opener=lambda req, timeout: _Response(200, headers or {}, body), sleep=lambda s: None, **kwargs)


# HTTP client
def test_only_https_urls_are_allowed():
    client = client_returning(b"{}")
    for url in ("http://example.test/x", "file:///etc/passwd", "ftp://example.test/x"):
        with pytest.raises(HttpError, match="only https"):
            client.get_json(url)


def test_oversized_responses_are_rejected():
    client = client_returning(b'{"a": "' + b"x" * 2000 + b'"}', max_bytes=1000)
    with pytest.raises(HttpError, match="more than 1000 bytes"):
        client.get_json("https://example.test/x")


def test_gzip_bombs_are_rejected():
    bomb = gzip.compress(b'{"pad": "' + b"0" * 5_000_000 + b'"}')
    assert len(bomb) < 20_000  # tiny on the wire...
    client = client_returning(bomb, {"Content-Encoding": "gzip"}, max_bytes=1_000_000)
    with pytest.raises(HttpError, match="decompressed to more than"):  # ...but not allowed to expand
        client.get_json("https://example.test/x")


def test_normal_gzip_still_works():
    client = client_returning(gzip.compress(json.dumps({"ok": True}).encode()), {"Content-Encoding": "gzip"})
    assert client.get_json("https://example.test/x")[0] == {"ok": True}


def test_corrupt_gzip_is_an_error():
    client = client_returning(b"\x1f\x8b" + b"not really gzip", {"Content-Encoding": "gzip"})
    with pytest.raises(HttpError, match="corrupt gzip"):
        client.get_json("https://example.test/x")


def make_request(url="https://v2.nba.api-sports.io/games"):
    return urllib.request.Request(url, headers={"x-apisports-key": SECRET})


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/steal",
        "http://v2.nba.api-sports.io/games",
        "//evil.test/x",
        "https://v2.nba.api-sports.io.evil.test/",
    ],
)
def test_redirects_off_the_host_are_refused(target):
    """urllib forwards request headers on redirect, so leaving the host would leak the key."""
    with pytest.raises(HttpError, match="refused redirect"):
        SameHostRedirectHandler().redirect_request(make_request(), None, 302, "Found", {}, target)


def test_same_host_redirects_are_followed():
    new = SameHostRedirectHandler().redirect_request(make_request(), None, 301, "Moved", {}, "/games?page=2")
    assert new.full_url == "https://v2.nba.api-sports.io/games?page=2"


def test_tls_context_verifies_and_requires_tls12():
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_request_headers_never_appear_in_errors():
    client = HttpClient(opener=lambda req, timeout: _Response(401, {}, b"denied"), sleep=lambda s: None)
    with pytest.raises(HttpError) as excinfo:
        client.get_json("https://example.test/x", headers={"x-apisports-key": SECRET})
    assert SECRET not in str(excinfo.value)


# redaction
def make_logger(name):
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    logger.addHandler(Collect())
    logger.setLevel(logging.DEBUG)
    return logger, records


def test_redaction_scrubs_messages_args_and_tracebacks():
    logger, records = make_logger("redaction-test")
    install_redaction(SECRET, logger=logger)

    logger.warning("using key %s for %s", SECRET, "games")
    logger.info(f"inline {SECRET}")
    try:
        raise RuntimeError(f"upstream echoed {SECRET}")
    except RuntimeError:
        logger.exception("request failed")

    joined = "\n".join(records)
    assert SECRET not in joined
    assert joined.count(REDACTED) == 3
    assert "using key [REDACTED] for games" in joined


def test_redaction_ignores_short_or_missing_secrets_and_does_not_stack():
    logger, records = make_logger("redaction-test-2")
    assert install_redaction(None, "abc", logger=logger).secrets == ()
    install_redaction(SECRET, logger=logger)
    install_redaction(SECRET, logger=logger)  # warm Lambda invocations call this again
    assert sum(isinstance(f, RedactingFilter) for f in logger.handlers[0].filters) == 1
    logger.warning("abc is fine")
    assert records == ["abc is fine"]


def test_config_repr_hides_the_key():
    config = Config(data_bucket="d", site_bucket="s", apinba_key=SECRET)
    assert SECRET not in repr(config)
    assert SECRET not in str(config)


def test_reading_a_parameter_never_logs_its_value(caplog):
    class Ok:
        def get_parameter(self, Name, WithDecryption):
            assert WithDecryption is True
            return {"Parameter": {"Value": SECRET}}

    class Denied:
        def get_parameter(self, **kwargs):
            err = Exception("denied")
            err.response = {"Error": {"Code": "AccessDeniedException"}}
            raise err

    assert read_secret_parameter("/heatcheck/apinba-api-key", Ok()) == SECRET
    with caplog.at_level(logging.WARNING):
        assert read_secret_parameter("/heatcheck/apinba-api-key", Denied()) is None
    assert "AccessDeniedException" in caplog.text and SECRET not in caplog.text


# Lambda input limits + redaction
@pytest.fixture
def lambda_env(tmp_path, monkeypatch):
    from heatcheck.storage import LocalStorage

    monkeypatch.setenv("DATA_BUCKET", "data-bucket")
    monkeypatch.setenv("SITE_BUCKET", "site-bucket")
    monkeypatch.setenv("DATA_SOURCES", "espn")
    monkeypatch.delenv("APINBA_KEY_PARAM", raising=False)
    monkeypatch.setattr(handler_module, "S3Storage", lambda bucket: LocalStorage(tmp_path / bucket))
    return tmp_path


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"dates": [f"2026-10-{d:02d}" for d in range(1, 16)]}, "at most 14 dates"),
        ({"dates": "2026-11-05"}, "must be a list"),
        ({"dates": [20261105]}, "must be a list"),
        ({"lookback_days": 0}, "between 1 and 14"),
        ({"lookback_days": 400}, "between 1 and 14"),
    ],
)
def test_manual_invocations_are_bounded(lambda_env, event, message):
    with pytest.raises(ValueError, match=message):
        handler_module.lambda_handler(event, None)


def test_handler_redacts_the_key_from_logs(lambda_env, monkeypatch, caplog):
    monkeypatch.setenv("APINBA_KEY", SECRET)

    def leaky_build_sources(names, http, apinba_key=None):
        logging.getLogger("heatcheck.leaky").warning("debug: key=%s", apinba_key)
        raise RuntimeError("stop here")

    monkeypatch.setattr(handler_module, "build_sources", leaky_build_sources)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        handler_module.lambda_handler({"dates": ["2026-11-05"]}, None)
    assert "key=[REDACTED]" in caplog.text
    assert SECRET not in caplog.text


# CLI key handling
class FakeSsm:
    def __init__(self):
        self.calls = []

    def put_parameter(self, **kwargs):
        self.calls.append(kwargs)


def test_set_api_key_prompts_hidden_and_stores_securestring(monkeypatch, capsys):
    ssm = FakeSsm()
    monkeypatch.setattr(cli, "_ssm_client", lambda region: ssm)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: f"  {SECRET}  ")
    assert cli.main(["set-api-key", "--name", "/test/key"]) == 0
    assert ssm.calls == [
        {
            "Name": "/test/key",
            "Value": SECRET,
            "Type": "SecureString",
            "Overwrite": True,
            "Description": "API-NBA key for Heat Check (read by the nightly Lambda)",
        }
    ]
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_set_api_key_refuses_empty_input(monkeypatch):
    ssm = FakeSsm()
    monkeypatch.setattr(cli, "_ssm_client", lambda region: ssm)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "   ")
    assert cli.main(["set-api-key"]) == 1
    assert ssm.calls == []


def test_set_api_key_from_env(monkeypatch):
    ssm = FakeSsm()
    monkeypatch.setattr(cli, "_ssm_client", lambda region: ssm)
    monkeypatch.setenv("APINBA_KEY", SECRET)
    assert cli.main(["set-api-key", "--from-env"]) == 0
    assert ssm.calls[0]["Value"] == SECRET and ssm.calls[0]["Name"] == cli.DEFAULT_KEY_PARAM


def test_cli_reads_the_key_from_ssm_when_configured(monkeypatch):
    monkeypatch.delenv("APINBA_KEY", raising=False)
    monkeypatch.setenv("APINBA_KEY_PARAM", "/heatcheck/apinba-api-key")
    monkeypatch.setattr(
        cli, "read_secret_parameter", lambda name: SECRET if name == "/heatcheck/apinba-api-key" else None
    )
    assert cli._apinba_key() == SECRET


def test_cli_has_no_key_argument():
    """Keys passed as arguments end up in shell history and `ps` output."""
    with pytest.raises(SystemExit):
        cli.main(["ingest", "--start", "2026-11-01", "--end", "2026-11-01", "--apinba-key", SECRET])


def test_the_real_opener_uses_the_strict_handlers():
    from heatcheck import http

    handlers = http._OPENER.handlers
    assert any(type(h) is SameHostRedirectHandler for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)
    https = next(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
    assert https._context.verify_mode == ssl.CERT_REQUIRED
    assert https._context.minimum_version >= ssl.TLSVersion.TLSv1_2
