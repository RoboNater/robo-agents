from pathlib import Path

import pytest
from agent_hub_common import ConfigurationError, HubSettings


def test_settings_have_local_defaults(tmp_path: Path) -> None:
    settings = HubSettings.from_env({}, cwd=tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8420
    assert settings.public_url == "http://127.0.0.1:8420"
    assert settings.database_path == tmp_path / ".hub/hub.db"
    assert settings.token_file == tmp_path / ".hub/token"


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


@pytest.mark.parametrize(
    ("name", "value"),
    [("HUB_PUBLIC_URL", "ftp://hub.example"), ("HUB_TOKEN", "   ")],
)
def test_settings_reject_invalid_overrides(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({name: value}, cwd=tmp_path)
