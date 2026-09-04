import socket
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
def test_ipv6_bind_host_is_normalized_for_the_socket_and_the_url(host: str) -> None:
    settings = HubSettings.from_env({"HUB_HOST": host})

    # The resolver rejects the bracketed form, so brackets belong only in the URL.
    assert settings.host == "::1"
    assert settings.public_url == "http://[::1]:8420"
    assert socket.getaddrinfo(settings.host, settings.port, type=socket.SOCK_STREAM)


def test_hostnames_are_left_alone() -> None:
    settings = HubSettings.from_env({"HUB_HOST": "alice-host"})

    assert settings.host == "alice-host"
    assert settings.public_url == "http://alice-host:8420"


@pytest.mark.parametrize(
    ("name", "value"),
    [("HUB_PUBLIC_URL", "ftp://hub.example"), ("HUB_TOKEN", "   ")],
)
def test_settings_reject_invalid_overrides(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({name: value})


def test_wait_bounds_and_liveness_scale_with_the_default_hold() -> None:
    settings = HubSettings.from_env({})

    assert settings.default_wait_s == 120
    assert settings.max_wait_s == 300
    # Spec §4.3: an agent is lost after three times the timeout with no contact,
    # which has to outlast the longest hold the hub will grant.
    assert settings.heartbeat_timeout_s == 360
    assert settings.heartbeat_timeout_s > settings.max_wait_s
    assert settings.sweep_interval_s == 10


def test_lowering_the_default_hold_keeps_the_bounds_consistent() -> None:
    settings = HubSettings.from_env({"HUB_DEFAULT_WAIT_S": "20"})

    assert settings.max_wait_s == 50
    assert settings.heartbeat_timeout_s == 60


def test_a_requested_wait_is_clamped_to_the_ceiling() -> None:
    settings = HubSettings.from_env({"HUB_DEFAULT_WAIT_S": "30", "HUB_MAX_WAIT_S": "45"})

    assert settings.bounded_wait(None) == 30
    assert settings.bounded_wait(10) == 10
    assert settings.bounded_wait(9000) == 45
    assert settings.bounded_wait(-5) == 0


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"HUB_DEFAULT_WAIT_S": "0"}, id="zero-wait"),
        pytest.param({"HUB_SWEEP_INTERVAL_S": "-1"}, id="negative-interval"),
        pytest.param({"HUB_MAX_WAIT_S": "soon"}, id="not-a-number"),
        pytest.param(
            {"HUB_DEFAULT_WAIT_S": "120", "HUB_MAX_WAIT_S": "60"}, id="ceiling-below-default"
        ),
        pytest.param(
            {"HUB_MAX_WAIT_S": "300", "HUB_HEARTBEAT_TIMEOUT_S": "120"}, id="lost-mid-hold"
        ),
    ],
)
def test_settings_reject_inconsistent_timings(env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env(env)
