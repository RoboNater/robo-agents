from pathlib import Path

import pytest
from agent_hub_common import ConfigurationError, HubSettings


def test_settings_have_local_defaults(tmp_path: Path) -> None:
    settings = HubSettings.from_env({"XDG_STATE_HOME": str(tmp_path)})

    assert settings.host == "127.0.0.1"
    assert settings.port == 8420
    assert settings.public_url == "http://127.0.0.1:8420"
    assert settings.state_dir == tmp_path / "agent-hub"
    assert settings.database_path == tmp_path / "agent-hub/hub.db"
    assert settings.token_file == tmp_path / "agent-hub/token"


def test_state_paths_ignore_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"XDG_STATE_HOME": "relative-state"}
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    monkeypatch.chdir(tmp_path / "one")
    first = HubSettings.from_env(env)
    monkeypatch.chdir(tmp_path / "two")
    second = HubSettings.from_env(env)

    assert first.state_dir == second.state_dir
    assert first.database_path == second.database_path
    assert first.token_file == second.token_file


def test_relative_xdg_state_home_falls_back_to_the_default() -> None:
    # The XDG base-directory specification declares a relative value invalid.
    settings = HubSettings.from_env({"XDG_STATE_HOME": "relative-state"})

    assert settings.state_dir == Path.home() / ".local/state/agent-hub"


def test_relative_state_dir_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        HubSettings.from_env({"HUB_STATE_DIR": "state"})


def test_relative_state_paths_resolve_against_the_state_directory(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {
            "HUB_STATE_DIR": str(tmp_path / "state"),
            "HUB_DB_PATH": "state.sqlite",
            "HUB_TOKEN_FILE": "secrets/token",
        }
    )

    assert settings.database_path == tmp_path / "state/state.sqlite"
    assert settings.token_file == tmp_path / "state/secrets/token"


def test_default_state_dir_falls_back_to_the_home_directory() -> None:
    settings = HubSettings.from_env({})

    assert settings.state_dir == Path.home() / ".local/state/agent-hub"


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_settings_reject_invalid_ports(port: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({"HUB_PORT": port})


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
        }
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.public_url == "https://hub.example"
    assert settings.database_path == tmp_path / "state.sqlite"
    assert settings.token == "secret"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "[0000:0000:0000:0000:0000:0000:0000:0000]", "*"],
)
def test_wildcard_bind_requires_an_explicit_public_url(host: str) -> None:
    with pytest.raises(ConfigurationError, match="HUB_PUBLIC_URL"):
        HubSettings.from_env({"HUB_HOST": host})


def test_wildcard_bind_accepts_an_explicit_public_url() -> None:
    settings = HubSettings.from_env(
        {"HUB_HOST": "0.0.0.0", "HUB_PUBLIC_URL": "http://alice-host:8420"}
    )

    assert settings.public_url == "http://alice-host:8420"


@pytest.mark.parametrize("host", ["::1", "[::1]", "0:0:0:0:0:0:0:1"])
def test_ipv6_bind_is_advertised_in_bracketed_compressed_form(host: str) -> None:
    settings = HubSettings.from_env({"HUB_HOST": host})

    assert settings.public_url == "http://[::1]:8420"


@pytest.mark.parametrize(
    ("name", "value"),
    [("HUB_PUBLIC_URL", "ftp://hub.example"), ("HUB_TOKEN", "   ")],
)
def test_settings_reject_invalid_overrides(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({name: value})
