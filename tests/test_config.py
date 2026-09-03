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
