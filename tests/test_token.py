import os
from pathlib import Path

from agent_hub_common import load_or_create_token, token_matches


def test_token_file_is_created_once_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "token"

    first = load_or_create_token(None, path)
    second = load_or_create_token(None, path)

    assert first == second
    assert len(first) >= 32
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert token_matches(first, second)
    assert not token_matches(first, "wrong")


def test_explicit_token_does_not_touch_disk(tmp_path: Path) -> None:
    path = tmp_path / "token"

    assert load_or_create_token("injected-secret", path) == "injected-secret"
    assert not path.exists()
