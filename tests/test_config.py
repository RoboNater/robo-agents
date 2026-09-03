from pathlib import Path

import pytest
from agent_hub_common import ConfigurationError, HubSettings


def test_settings_have_local_defaults(tmp_path: Path) -> None:
    settings = HubSettings.from_env({"XDG_STATE_HOME": str(tmp_path)}, cwd=tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8420
    assert settings.public_url == "http://127.0.0.1:8420"
    assert settings.state_dir == tmp_path / "agent-hub"
    assert settings.database_path == tmp_path / "agent-hub/hub.db"
    assert settings.token_file == tmp_path / "agent-hub/token"


def test_state_paths_ignore_the_working_directory(tmp_path: Path) -> None:
    env = {"HUB_STATE_DIR": str(tmp_path / "state")}

    first = HubSettings.from_env(env, cwd=tmp_path / "one")
    second = HubSettings.from_env(env, cwd=tmp_path / "two")

    assert first.database_path == second.database_path == tmp_path / "state/hub.db"
    assert first.token_file == second.token_file == tmp_path / "state/token"


def test_relative_state_paths_resolve_against_the_state_directory(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {
            "HUB_STATE_DIR": str(tmp_path / "state"),
            "HUB_DB_PATH": "state.sqlite",
            "HUB_TOKEN_FILE": "secrets/token",
        },
        cwd=tmp_path,
    )

    assert settings.database_path == tmp_path / "state/state.sqlite"
    assert settings.token_file == tmp_path / "state/secrets/token"


def test_default_state_dir_falls_back_to_the_home_directory(tmp_path: Path) -> None:
    settings = HubSettings.from_env({}, cwd=tmp_path)

    assert settings.state_dir == Path.home() / ".local/state/agent-hub"


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_settings_reject_invalid_ports(tmp_path: Path, port: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({"HUB_PORT": port}, cwd=tmp_path)


def test_settings_normalize_overrides(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {
            "HUB_HOST": "0.0.0.0",
            "HUB_PORT": "9000",
            "HUB_PUBLIC_URL": "https://hub.example/",
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": "state.sqlite",
            "HUB_TOKEN": " secret ",
            "HUB_TOKEN_FILE": "unused-token",
        },
        cwd=tmp_path,
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.public_url == "https://hub.example"
    assert settings.database_path == tmp_path / "state.sqlite"
    assert settings.token == "secret"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "*"])
def test_wildcard_bind_requires_an_explicit_public_url(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigurationError, match="HUB_PUBLIC_URL"):
        HubSettings.from_env({"HUB_HOST": host}, cwd=tmp_path)


def test_wildcard_bind_accepts_an_explicit_public_url(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {"HUB_HOST": "0.0.0.0", "HUB_PUBLIC_URL": "http://alice-host:8420"},
        cwd=tmp_path,
    )

    assert settings.public_url == "http://alice-host:8420"


def test_ipv6_loopback_bind_is_advertised_with_brackets(tmp_path: Path) -> None:
    settings = HubSettings.from_env({"HUB_HOST": "::1"}, cwd=tmp_path)

    assert settings.public_url == "http://[::1]:8420"


@pytest.mark.parametrize(
    ("name", "value"),
    [("HUB_PUBLIC_URL", "ftp://hub.example"), ("HUB_TOKEN", "   ")],
)
def test_settings_reject_invalid_overrides(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({name: value}, cwd=tmp_path)
