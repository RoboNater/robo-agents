"""Prepare a whole user run directory in one command (#76)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
assert SPEC and SPEC.loader
PREPARE_RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_RUN)


def make_origin(tmp_path: Path) -> Path:
    origin = (tmp_path / "origin").resolve()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    return origin


def fake_runner(monkeypatch: pytest.MonkeyPatch, *, gh_auth_ok: bool = True) -> Any:
    original_run = PREPARE_RUN.run
    original_output = PREPARE_RUN.run_output

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[:2] == ("claude", "--version"):
            return "2.1.277 (Claude Code)"
        if args[:2] == ("codex", "--version"):
            return "codex-cli 0.154.0"
        if args[:3] == ("gh", "auth", "status"):
            if not gh_auth_ok:
                raise subprocess.CalledProcessError(1, list(args))
            return ""
        if args[:3] == ("gh", "repo", "view"):
            return json.dumps(
                {
                    "viewerPermission": "WRITE",
                    "squashMergeAllowed": True,
                    "mergeCommitAllowed": False,
                    "rebaseMergeAllowed": False,
                }
            )
        if args[:2] == ("gh", "api"):
            return json.dumps({"workflows": []})
        return str(original_run(*args, **kwargs))

    def output_runner(*args: Any, **kwargs: Any) -> tuple[str, str]:
        if args[:3] == ("codex", "login", "status"):
            # Codex CLI reports its status on stderr with exit 0 on Windows.
            return ("", "Logged in using ChatGPT")
        result = original_output(*args, **kwargs)
        return (str(result[0]), str(result[1]))

    monkeypatch.setattr(PREPARE_RUN, "run", runner)
    monkeypatch.setattr(PREPARE_RUN, "run_output", output_runner)
    return runner


def printed_report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    report = json.loads(out.split("\nLaunch commands")[0])
    assert isinstance(report, dict)
    return report


def test_fresh_run_produces_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, issue=42, account="testuser", allow_no_ci="auto"
    )
    assert (
        manifest["workspaces"]["bob"]["workspace_id"]
        != manifest["workspaces"]["charlie"]["workspace_id"]
    )
    assert manifest["clone_repository"] == str(origin)
    assert manifest["policy"]["allow_no_ci"] is True  # local repo has no workflows
    assert manifest["policy"]["merge_method"] == "squash"

    configs = run_dir / "configs"
    bob_config = json.loads((configs / "bob.mcp.json").read_text(encoding="utf-8"))
    assert bob_config["mcpServers"]["hub"]["env"]["AGENT_NAME"] == "bob"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_HARNESS"] == "claude-code"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_HARNESS_VERSION"] == "2.1.277"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_PROVIDER"] == "anthropic"
    assert (
        bob_config["mcpServers"]["hub"]["env"]["HUB_WORKSPACE"]
        == manifest["workspaces"]["bob"]["path"]
    )
    codex_config = tomllib.loads((configs / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert set(codex_config["mcp_servers"]) == {"hub"}
    assert codex_config["sandbox_mode"] == "workspace-write"
    assert codex_config["sandbox_workspace_write"]["network_access"] is True
    assert set(codex_config["mcp_servers"]["hub"]["enabled_tools"]) == {
        "check_in",
        "get_role_guide",
        "await_assignment",
        "report_progress",
        "ask_alice",
        "submit_result",
    }
    assert codex_config["mcp_servers"]["hub"]["env"]["AGENT_NAME"] == "charlie"
    assert codex_config["mcp_servers"]["hub"]["env"]["HUB_HARNESS_VERSION"] == "0.154.0"

    for name in ("bob", "charlie"):
        prompt = (run_dir / f"{name}.prompt.md").read_text(encoding="utf-8")
        assert "$AGENT_NAME" not in prompt
        assert name in prompt
    alice_prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    assert "42" in alice_prompt and "testuser" in alice_prompt
    assert "<account>" not in alice_prompt

    alice_config = json.loads((configs / "alice.mcp.json").read_text(encoding="utf-8"))
    assert alice_config["mcpServers"]["hub"]["env"]["HUB_STATE_DIR"] == str(
        run_dir / "hub-state"
    )

    # Alice's orchestrator skill is linked run-locally, next to the clones.
    skill = run_dir / "alice-runtime" / ".claude" / "skills" / "alice-orchestrator"
    assert skill.exists()

    # The stderr-form login status survives the report.
    report = printed_report(capsys)
    assert report["checks"]["codex_auth"] == {"charlie": "codex: Logged in using ChatGPT"}


def test_rerun_is_idempotent_and_preserves_clone_token_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    first = PREPARE_RUN.prepare(str(origin), run_dir)
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8")
    bob_identity = (
        run_dir / "bob" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8")
    charlie_identity = (
        run_dir / "charlie" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8")
    marker = run_dir / "bob" / "uncommitted-marker"
    marker.write_text("preserve")
    # Rerun on a dirty clone must fail without touching anything.
    with pytest.raises(ValueError, match="dirty"):
        PREPARE_RUN.prepare(str(origin), run_dir)
    assert (run_dir / "hub-state" / "token").read_text(encoding="utf-8") == token
    marker.unlink()
    second = PREPARE_RUN.prepare(str(origin), run_dir)
    assert second["workspaces"] == first["workspaces"]
    assert (run_dir / "hub-state" / "token").read_text(encoding="utf-8") == token
    assert (
        run_dir / "bob" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8") == bob_identity
    assert (
        (run_dir / "charlie" / ".git" / "robo-agents-workspace.json").read_text(
            encoding="utf-8"
        )
        == charlie_identity
    )


def test_fails_when_gh_unauthenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runner(monkeypatch, gh_auth_ok=False)
    with pytest.raises(ValueError, match="authenticated"):
        PREPARE_RUN.prepare("test-org/test-repo", (tmp_path / "run").resolve())


def test_fails_when_runtime_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runner(monkeypatch)
    original = PREPARE_RUN.run

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[:2] == ("codex", "--version"):
            raise OSError("no such file")
        return str(original(*args, **kwargs))

    monkeypatch.setattr(PREPARE_RUN, "run", runner)
    with pytest.raises(ValueError, match="requires the 'codex' CLI on PATH"):
        PREPARE_RUN.prepare(
            "test-org/test-repo",
            (tmp_path / "run").resolve(),
            skip_github_checks=True,
        )


def test_no_token_or_clone_path_inside_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir)
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8").strip()
    bob_path = manifest["workspaces"]["bob"]["path"]
    charlie_path = manifest["workspaces"]["charlie"]["path"]
    for clone, other in ((bob_path, charlie_path), (charlie_path, bob_path)):
        for path in Path(clone).rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            assert token not in text, f"token leaked into {path}"
            assert other not in text, f"other clone path leaked into {path}"
        assert not (Path(clone) / "bob.mcp.json").exists()
        assert not (Path(clone) / "charlie.prompt.md").exists()
        assert not (Path(clone) / "token").exists()


def test_same_harness_pair_renders_both_claude_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        bob_harness="claude-code",
        charlie_harness="claude-code",
        bob_model="claude-sonnet-5",
        charlie_model="claude-sonnet-5",
    )
    assert manifest["policy"]["role_policy"]["reviewer_harness_differs"] is False
    assert (run_dir / "configs" / "charlie.mcp.json").exists()
    with pytest.raises(ValueError, match="not yet supported"):
        PREPARE_RUN.prepare(
            str(origin),
            (tmp_path / "other").resolve(),
            charlie_harness="opencode",
            skip_github_checks=True,
        )


@pytest.mark.parametrize("bob_harness", ["claude-code", "codex"])
@pytest.mark.parametrize("charlie_harness", ["claude-code", "codex"])
def test_launch_lines_point_at_rendered_configs(
    tmp_path: Path, bob_harness: str, charlie_harness: str
) -> None:
    run_dir = (tmp_path / "run").resolve()
    configs = run_dir / "configs"
    lines = PREPARE_RUN.launch_lines(
        run_dir,
        configs,
        run_dir / "alice-runtime",
        run_dir / "bob",
        run_dir / "charlie",
        bob_harness,
        charlie_harness,
    )
    text = "\n".join(lines)
    assert f'cd "{run_dir / "alice-runtime"}"' in text
    if bob_harness == "codex":
        assert "bob-codex" in text
        assert "bob.mcp.json" not in text
    else:
        assert f'--mcp-config "{configs / "bob.mcp.json"}"' in text
    if charlie_harness == "codex":
        assert f'--add-dir "{run_dir / "charlie" / ".git"}"' in text
        assert "charlie.mcp.json" not in text
    else:
        assert f'--mcp-config "{configs / "charlie.mcp.json"}"' in text


def test_launch_lines_windows_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    run_dir = (tmp_path / "run").resolve()
    lines = PREPARE_RUN.launch_lines(
        run_dir,
        run_dir / "configs",
        run_dir / "alice-runtime",
        run_dir / "bob",
        run_dir / "charlie",
        "claude-code",
        "codex",
    )
    text = "\n".join(lines)
    assert "$env:CODEX_HOME" in text
    assert "Get-Content -Raw" in text
    assert "CODEX_HOME=" not in text.replace("$env:CODEX_HOME", "")


def test_launch_lines_quote_paths_with_spaces(tmp_path: Path) -> None:
    run_dir = (tmp_path / "my run").resolve()
    lines = PREPARE_RUN.launch_lines(
        run_dir,
        run_dir / "configs",
        run_dir / "alice-runtime",
        run_dir / "bob",
        run_dir / "charlie",
        "claude-code",
        "codex",
    )
    for line in lines:
        if "my run" in line:
            assert line.count('"') >= 2, line


def test_bare_slug_expands_to_https_clone_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runner(monkeypatch)
    seen: list[tuple[str, str]] = []
    ids = {"bob": "a" * 64, "charlie": "b" * 64}

    def fake_bootstrap(agent: str, destination: Path, repository: str) -> dict[str, str]:
        seen.append((agent, repository))
        return {"path": str(destination), "workspace_id": ids[agent]}

    monkeypatch.setattr(PREPARE_RUN, "bootstrap_clone", fake_bootstrap)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        "test-org/test-repo", run_dir, skip_github_checks=True
    )
    assert manifest["clone_repository"] == "https://github.com/test-org/test-repo.git"
    assert seen == [
        ("bob", "https://github.com/test-org/test-repo.git"),
        ("charlie", "https://github.com/test-org/test-repo.git"),
    ]


@pytest.mark.parametrize(
    "repository,slug,expected",
    [
        ("test-org/test-repo", "test-org/test-repo", "https://github.com/test-org/test-repo.git"),
        (
            "test-org/test-repo.git",
            "test-org/test-repo",
            "https://github.com/test-org/test-repo.git",
        ),
        (
            "git@github.com:test-org/test-repo.git",
            "test-org/test-repo",
            "git@github.com:test-org/test-repo.git",
        ),
        (
            "https://github.com/test-org/test-repo.git",
            "test-org/test-repo",
            "https://github.com/test-org/test-repo.git",
        ),
    ],
)
def test_clone_source_derivation(repository: str, slug: str, expected: str) -> None:
    assert PREPARE_RUN.clone_source(repository, slug) == expected


def test_both_codex_workers_report_each_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    PREPARE_RUN.prepare(
        str(origin), run_dir, bob_harness="codex", charlie_harness="codex"
    )
    report = printed_report(capsys)
    assert set(report["checks"]["codex_auth"]) == {"bob", "charlie"}


def test_main_reports_actionable_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            "test-org/test-repo",
            "--run-dir",
            str((tmp_path / "run").resolve()),
            "--bob",
            "nosuch",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        PREPARE_RUN.main()
    assert "prepare-run: error:" in str(exc.value.code)
    assert "not yet supported" in str(exc.value.code)
