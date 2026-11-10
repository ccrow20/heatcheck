import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from heatcheck import handler as handler_module
from heatcheck.cli import main as cli_main
from heatcheck.config import Config, clean_secret, resolve_apinba_key
from heatcheck.pipeline import PipelineError, load_lines, run_pipeline
from heatcheck.sources import SourceError
from heatcheck.storage import STREAKS_KEY, LocalStorage, S3Storage, gamelog_key, open_storage
from heatcheck.telemetry import emit_metrics
from tests.conftest import STEADY_SCORES, day_result, full_game, make_line

NOW = datetime(2026, 11, 6, 10, 30, tzinfo=UTC)
NOV5 = date(2026, 11, 5)


class ScriptedSource:
    """Returns a full slate for every date unless told to fail on some."""

    def __init__(self, name="fake", fail_on=()):
        self.name = name
        self.fail_on = set(fail_on)
        self.days = []

    def fetch_day(self, day):
        self.days.append(day)
        if day in self.fail_on:
            raise SourceError(f"{self.name} is down")
        iso = day.isoformat()
        return day_result(iso, full_game(iso, f"{self.name}:{iso}"), source=self.name)


def seed_history(store, player="Seeded Star", days=20, end=date(2026, 11, 3)):
    """Store one game per night for a player (plus filler so validation passes)."""
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        lines = [*full_game(day.isoformat(), f"seed:{day}"), make_line(player, day, pts=STEADY_SCORES[i % 20])]
        store.put_json(gamelog_key(day), day_result(day.isoformat(), lines, source="seed").to_dict())


# storage
def test_local_storage_round_trip(tmp_path):
    store = LocalStorage(tmp_path)
    store.put_json("gamelogs/season=2026-27/date=2026-11-05.json", {"a": 1})
    store.put_json("gamelogs/season=2025-26/date=2026-04-01.json", {"b": 2})
    assert store.get_json("gamelogs/season=2026-27/date=2026-11-05.json") == {"a": 1}
    assert store.get_json("missing.json") is None
    assert store.list_keys("gamelogs/season=2026-27/") == ["gamelogs/season=2026-27/date=2026-11-05.json"]
    assert len(store.list_keys("gamelogs/")) == 2
    assert store.list_keys("nothing/here/") == []
    with pytest.raises(ValueError):
        store.put_json("../escape.json", {})


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType, **extra):
        self.objects[Key] = Body
        self.puts.append({"Key": Key, "ContentType": ContentType, **extra})

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + 2]  # tiny pages to exercise pagination
        more = start + 2 < len(keys)
        resp = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            resp["NextContinuationToken"] = str(start + 2)
        return resp


def test_s3_storage_with_fake_client():
    fake = FakeS3()
    store = S3Storage("bucket", client=fake)
    for i in range(5):
        store.put_json(f"p/{i}.json", {"i": i})
    store.put_json(STREAKS_KEY, {}, cache_control="public, max-age=300")
    assert store.get_json("p/3.json") == {"i": 3}
    assert store.get_json("p/missing.json") is None
    assert store.list_keys("p/") == [f"p/{i}.json" for i in range(5)]
    assert store.get_many_json(["p/0.json", "p/4.json"]) == {"p/0.json": {"i": 0}, "p/4.json": {"i": 4}}
    assert fake.puts[-1] == {
        "Key": STREAKS_KEY,
        "ContentType": "application/json",
        "CacheControl": "public, max-age=300",
    }


def test_s3_storage_reraises_other_errors():
    class Broken(FakeS3):
        def get_object(self, Bucket, Key):
            err = Exception("denied")
            err.response = {"Error": {"Code": "AccessDenied"}}
            raise err

    with pytest.raises(Exception, match="denied"):
        S3Storage("bucket", client=Broken()).get_json("x")


def test_open_storage(tmp_path):
    assert isinstance(open_storage(str(tmp_path)), LocalStorage)
    with pytest.raises(ValueError):
        open_storage("s3://bucket/with/path")


# pipeline
def test_pipeline_ingests_and_publishes(tmp_path):
    data, site = LocalStorage(tmp_path / "data"), LocalStorage(tmp_path / "site")
    seed_history(data)
    source = ScriptedSource()

    report = run_pipeline(dates=[date(2026, 11, 4), NOV5], sources=[source], data_store=data, site_store=site, now=NOW)

    assert report.ok and report.published
    assert source.days == [date(2026, 11, 4), NOV5]
    assert set(report.ingested) == {"2026-11-04", "2026-11-05"}
    assert data.get_json(gamelog_key(NOV5))["source"] == "fake"

    payload = site.get_json(STREAKS_KEY)
    assert payload["schema_version"] == 1
    assert payload["as_of"] == "2026-11-05"
    assert payload["generated_at"] == NOW.isoformat(timespec="seconds")
    assert payload["last_ingest"]["2026-11-05"]["source"] == "fake"
    assert any(p["name"] == "Seeded Star" for p in payload["players"])


def test_reingesting_a_date_overwrites_it(tmp_path):
    data, site = LocalStorage(tmp_path / "data"), LocalStorage(tmp_path / "site")
    run_pipeline(dates=[NOV5], sources=[ScriptedSource("first")], data_store=data, site_store=site, now=NOW)
    run_pipeline(dates=[NOV5], sources=[ScriptedSource("second")], data_store=data, site_store=site, now=NOW)
    assert data.get_json(gamelog_key(NOV5))["source"] == "second"
    assert len(data.list_keys("gamelogs/")) == 1


def test_fallback_days_are_counted(tmp_path):
    data, site = LocalStorage(tmp_path / "data"), LocalStorage(tmp_path / "site")
    primary = ScriptedSource("primary", fail_on={NOV5})
    backup = ScriptedSource("backup")
    report = run_pipeline(
        dates=[date(2026, 11, 4), NOV5], sources=[primary, backup], data_store=data, site_store=site, now=NOW
    )
    assert report.ok
    assert report.fallback_days == 1
    assert report.ingested["2026-11-05"]["source"] == "backup"
    assert report.ingested["2026-11-05"]["failed_sources"] == ["primary"]


def test_total_failure_still_publishes_existing_data(tmp_path):
    data, site = LocalStorage(tmp_path / "data"), LocalStorage(tmp_path / "site")
    seed_history(data)
    report = run_pipeline(
        dates=[NOV5], sources=[ScriptedSource(fail_on={NOV5})], data_store=data, site_store=site, now=NOW
    )
    assert not report.ok
    assert "2026-11-05" in report.failures
    assert report.published
    assert data.get_json(gamelog_key(NOV5)) is None
    assert any(p["name"] == "Seeded Star" for p in site.get_json(STREAKS_KEY)["players"])


def test_load_lines_reads_current_and_previous_season(tmp_path):
    data = LocalStorage(tmp_path)
    for day in ("2026-04-10", "2026-11-05", "2024-04-10"):
        d = date.fromisoformat(day)
        data.put_json(gamelog_key(d), day_result(day, [make_line("Season Hopper", day)]).to_dict())
    lines = load_lines(data, ["2025-26", "2026-27"])
    assert sorted(ln.date for ln in lines) == ["2026-04-10", "2026-11-05"]


# config, telemetry, handler, CLI
def test_config_from_env():
    cfg = Config.from_env(
        {"DATA_BUCKET": "d", "SITE_BUCKET": "s", "DATA_SOURCES": "apinba, espn", "LOOKBACK_DAYS": "3"}
    )
    assert cfg.sources == ("apinba", "espn")
    assert cfg.lookback_days == 3
    with pytest.raises(RuntimeError, match="DATA_BUCKET"):
        Config.from_env({"SITE_BUCKET": "s"})


def test_api_key_resolution():
    assert clean_secret("unset") is None
    assert clean_secret("  abc ") == "abc"
    base = {"DATA_BUCKET": "d", "SITE_BUCKET": "s"}
    assert resolve_apinba_key(Config.from_env({**base, "APINBA_KEY": "direct"})) == "direct"

    class FakeSsm:
        def __init__(self, value):
            self.value = value

        def get_parameter(self, Name, WithDecryption):
            assert WithDecryption is True
            return {"Parameter": {"Value": self.value}}

    cfg = Config.from_env({**base, "APINBA_KEY_PARAM": "/heatcheck/apinba-key"})
    assert resolve_apinba_key(cfg, FakeSsm("real-key")) == "real-key"
    assert resolve_apinba_key(cfg, FakeSsm("unset")) is None

    class BrokenSsm:
        def get_parameter(self, **kwargs):
            raise RuntimeError("AccessDenied")

    assert resolve_apinba_key(cfg, BrokenSsm()) is None


def test_emit_metrics_is_valid_emf():
    buf = io.StringIO()
    record = json.loads(emit_metrics({"PipelineSuccess": 1, "FallbackDays": 0}, project="heatcheck", stream=buf))
    assert buf.getvalue().strip() == json.dumps(record)
    directive = record["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "HeatCheck"
    assert directive["Dimensions"] == [["Project"]]
    assert [m["Name"] for m in directive["Metrics"]] == ["PipelineSuccess", "FallbackDays"]
    assert record["Project"] == "heatcheck" and record["PipelineSuccess"] == 1


@pytest.fixture
def lambda_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "data-bucket")
    monkeypatch.setenv("SITE_BUCKET", "site-bucket")
    monkeypatch.setenv("DATA_SOURCES", "espn")
    monkeypatch.delenv("APINBA_KEY", raising=False)
    monkeypatch.delenv("APINBA_KEY_PARAM", raising=False)
    monkeypatch.setattr(handler_module, "S3Storage", lambda bucket: LocalStorage(tmp_path / bucket))
    monkeypatch.setattr(handler_module, "utc_now", lambda: NOW)
    return tmp_path


def test_lambda_handler_success(lambda_env, monkeypatch, capsys):
    source = ScriptedSource("espn")
    monkeypatch.setattr(handler_module, "build_sources", lambda names, http, apinba_key=None: [source])

    result = handler_module.lambda_handler({}, None)

    assert result["ok"] is True
    assert source.days == [date(2026, 11, 4), NOV5]  # default two-night lookback
    assert (lambda_env / "site-bucket" / STREAKS_KEY).exists()
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert emf[-1]["PipelineSuccess"] == 1
    assert emf[-1]["LinesIngested"] == 20


def test_lambda_handler_explicit_dates(lambda_env, monkeypatch):
    source = ScriptedSource("espn")
    monkeypatch.setattr(handler_module, "build_sources", lambda names, http, apinba_key=None: [source])
    handler_module.lambda_handler({"dates": ["2026-10-30"]}, None)
    assert source.days == [date(2026, 10, 30)]


def test_lambda_handler_raises_after_publishing_on_failure(lambda_env, monkeypatch, capsys):
    monkeypatch.setattr(
        handler_module, "build_sources", lambda names, http, apinba_key=None: [ScriptedSource(fail_on={NOV5})]
    )
    with pytest.raises(PipelineError, match="2026-11-05"):
        handler_module.lambda_handler({}, None)
    assert (lambda_env / "site-bucket" / STREAKS_KEY).exists()
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert emf[-1]["PipelineSuccess"] == 0
    assert emf[-1]["DaysFailed"] == 1


def test_cli_publish_from_local_data(tmp_path, capsys):
    data = LocalStorage(tmp_path / "data")
    seed_history(data, end=date(2026, 11, 5))
    code = cli_main(
        ["publish", "--data", str(tmp_path / "data"), "--site", str(tmp_path / "site"), "--as-of", "2026-11-05"]
    )
    assert code == 0
    payload = LocalStorage(tmp_path / "site").get_json(STREAKS_KEY)
    assert payload["as_of"] == "2026-11-05"
    assert "Published streaks for" in capsys.readouterr().out


def test_cli_ingest_resume_and_run(tmp_path, monkeypatch, capsys):
    from heatcheck import cli

    source = ScriptedSource("espn", fail_on={date(2026, 11, 2)})
    monkeypatch.setattr(cli, "build_sources", lambda names, http, apinba_key=None: [source])
    data, site = str(tmp_path / "data"), str(tmp_path / "site")

    assert cli.main(["ingest", "--start", "2026-11-01", "--end", "2026-11-03", "--data", data]) == 1
    out = capsys.readouterr()
    assert "2 ok, 1 failed" in out.out and "FAILED" in out.err

    source.days.clear()
    assert cli.main(["ingest", "--start", "2026-11-01", "--end", "2026-11-03", "--data", data, "--skip-existing"]) == 1
    assert source.days == [date(2026, 11, 2)]  # stored dates are skipped, the failed one is retried
    assert "skipped (already stored)" in capsys.readouterr().out

    assert cli.main(["run", "--date", "2026-11-03", "--data", data, "--site", site]) == 0
    assert LocalStorage(site).get_json(STREAKS_KEY)["last_ingest"]["2026-11-03"]["source"] == "espn"
