import json

import pytest

from heatcheck_infra.settings import Settings


def test_defaults_are_valid():
    settings = Settings()
    assert settings.function_name == "heatcheck-nightly"
    assert settings.apinba_key_parameter == "/heatcheck/apinba-api-key"


def test_load_reads_the_file_skips_comments_and_applies_context(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"_help": "ignored", "alert_email": "me@example.com", "lookback_days": 3}))
    context = {"lookback_days": "5", "create_budget": "false"}.get
    settings = Settings.load(path, context)
    assert settings.alert_email == "me@example.com"
    assert settings.lookback_days == 5  # context wins, and strings are converted
    assert settings.create_budget is False


def test_missing_file_means_defaults(tmp_path):
    assert Settings.load(tmp_path / "nope.json") == Settings()


def test_the_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.json"
    settings = Settings.load(example)
    assert settings.github_repo == "your-github-username/heatcheck"


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"alert_emial": "typo@example.com"}))
    with pytest.raises(ValueError, match="alert_emial"):
        Settings.load(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project": "Heat Check"}, "project"),
        ({"project": "2heat"}, "starting with a letter"),
        ({"data_sources": "espn,nba"}, "data_sources"),
        ({"lookback_days": 30}, "lookback_days"),
        ({"alert_email": "not-an-email"}, "alert_email"),
        ({"github_repo": "just-a-name"}, "github_repo"),
        ({"lambda_timeout_seconds": 5}, "lambda_timeout_seconds"),
        ({"domain_name": "hc.example.com"}, "hosted_zone_id"),
        (
            {
                "domain_name": "hc.example.com",
                "hosted_zone_id": "Z1",
                "hosted_zone_name": "example.com",
                "aws_region": "eu-west-1",
            },
            "us-east-1",
        ),
    ],
)
def test_invalid_settings_fail_early(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(**overrides)


def test_bad_boolean_context_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="true or false"):
        Settings.load(tmp_path / "none.json", {"create_budget": "maybe"}.get)
