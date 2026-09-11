import pytest
from agent_hub_common import AgentProfile, ConfigurationError, ModelSource
from worker_mcp.config import WorkerSettings


def test_worker_settings_parses_valid_env() -> None:
    env = {
        "HUB_URL": "http://127.0.0.1:8420/",
        "HUB_TOKEN": "secret-token",
        "AGENT_NAME": "bob",
    }
    settings = WorkerSettings.from_env(env)
    assert settings.hub_url == "http://127.0.0.1:8420"
    assert settings.token == "secret-token"
    assert settings.agent_name == "bob"
    # Nothing the launcher left unset is guessed, not even the harness.
    assert settings.profile == AgentProfile()
    assert settings.default_wait_s == 120.0
    assert settings.max_retries == 3
    assert settings.backoff_factor_s == 0.5


def test_worker_settings_custom_overrides() -> None:
    env = {
        "HUB_URL": "https://hub.example.com",
        "HUB_TOKEN": "token-123",
        "AGENT_NAME": "charlie",
        "HUB_HARNESS": "codex",
        "HUB_HARNESS_VERSION": "0.154.0",
        "HUB_PROVIDER": "openai",
        "HUB_MODEL": "example-codex-model",
        "HUB_CAPABILITIES": "python, gh,,python",
        "HUB_DEFAULT_WAIT_S": "45.5",
        "HUB_MAX_RETRIES": "5",
        "HUB_BACKOFF_FACTOR_S": "1.5",
    }
    settings = WorkerSettings.from_env(env)
    assert settings.hub_url == "https://hub.example.com"
    assert settings.agent_name == "charlie"
    assert settings.profile == AgentProfile(
        harness="codex",
        harness_version="0.154.0",
        provider="openai",
        model="example-codex-model",
        model_source=ModelSource.ENV,
        capabilities=("python", "gh"),
    )
    assert settings.default_wait_s == 45.5
    assert settings.max_retries == 5
    assert settings.backoff_factor_s == 1.5


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({}, "HUB_URL must be set"),
        ({"HUB_URL": ""}, "HUB_URL must be set"),
        ({"HUB_URL": "ftp://hub"}, r"HUB_URL must be an http\(s\) URL"),
        ({"HUB_URL": "http://hub"}, "HUB_TOKEN must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": ""}, "HUB_TOKEN must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": "tok"}, "AGENT_NAME must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": "tok", "AGENT_NAME": ""}, "AGENT_NAME must be set"),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_DEFAULT_WAIT_S": "-1",
            },
            "must be greater than zero",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_DEFAULT_WAIT_S": "abc",
            },
            "must be a number of seconds",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_MAX_RETRIES": "-2",
            },
            "must be zero or greater",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_MAX_RETRIES": "xyz",
            },
            "must be an integer",
        ),
    ],
)
def test_worker_settings_rejects_invalid_env(env: dict[str, str], match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        WorkerSettings.from_env(env)
