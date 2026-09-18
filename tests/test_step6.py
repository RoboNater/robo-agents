"""Local script execution, deterministic driver milestones, and fail-closed evidence."""

import copy
import importlib.util
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("step6", ROOT / "scripts/step6.py")
assert SPEC and SPEC.loader
STEP6 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STEP6)


def stamp(second: int) -> str:
    return f"2026-09-18T12:00:{second:02d}.000Z"


@pytest.fixture
def proof() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    a, b, c, d, e, f, g = (letter * 40 for letter in "abcdef1")
    url = "https://github.com/RoboNater/robo-agents-sandbox/pull/10"
    policy = json.loads((ROOT / "scenarios/step6-localhost-untrusted.json").read_text())["policy"]
    manifest: dict[str, Any] = {
        "run_id": "test",
        "issue": {"number": 9},
        "work_pr": {"url": url, "number": 10},
        "repository": STEP6.SANDBOX,
        "models": {"bob": "claude-sonnet-5", "charlie": "gpt-5.6-sol"},
        "versions": {"claude": "2.1.276 (Claude Code)", "codex": "codex-cli 0.154.0"},
        "workspaces": {
            "bob": {"path": "/runs/bob", "workspace_id": "bob-id"},
            "charlie": {"path": "/runs/charlie", "workspace_id": "charlie-id"},
        },
        "policy": policy,
        "isolation_check": {"absent_in_charlie": True},
        "canary": "unique-canary",
        "finding_id": "r1-1",
        "finding_tag": "STEP6-NORMALIZE-001",
        "disturbances": {
            "head": {
                "old_head": b,
                "new_head": c,
                "state": "pushed",
                "at": stamp(9),
                "approval_task": "3",
            },
            "base": {
                "old_main": d,
                "new_main": e,
                "head": g,
                "state": "merged",
                "merged_at": stamp(14),
                "approval_task": "4",
            },
        },
    }
    tasks = []

    def task(
        number: int, title: str, role: str, result: dict[str, Any], head: str | None = None
    ) -> None:
        tasks.append(
            {
                "id": str(number),
                "title": title,
                "role": role,
                "assignee": "charlie" if role == "reviewer" else "bob",
                "pr_head_sha": head,
                "state": "completed",
                "created": stamp(number * 2),
                "updated": stamp(number * 2 + 1),
                "result_json": json.dumps(result),
            }
        )

    task(
        0,
        "IMPLEMENT for sandbox#9",
        "implementer",
        {"outcome": "completed", "head_sha": a, "pr_url": url},
    )
    task(
        1,
        "REVIEW for 0",
        "reviewer",
        {
            "verdict": "changes_requested",
            "reviewed_head_sha": a,
            "blocking_findings": [{"id": "r1-1", "text": "STEP6-NORMALIZE-001 needs casefold"}],
            "review_url": "review-1",
        },
        a,
    )
    task(2, "ADDRESS for 1", "implementer", {"head_sha": b, "resolved_finding_ids": ["r1-1"]})
    task(
        3,
        "REVIEW for 2",
        "reviewer",
        {"verdict": "approved", "reviewed_head_sha": b, "review_url": "review-3"},
        b,
    )
    task(
        4,
        "REVIEW for head",
        "reviewer",
        {"verdict": "approved", "reviewed_head_sha": c, "review_url": "review-4"},
        c,
    )
    tasks[4]["created"], tasks[4]["updated"] = stamp(11), stamp(12)
    task(
        5,
        "REBASE for head",
        "rebase",
        {"outcome": "completed", "head_sha": f, "conflict_files": []},
        c,
    )
    tasks[5]["created"], tasks[5]["updated"] = stamp(16), stamp(17)
    task(6, "CLOSE-OUT for head", "implementer", {"outcome": "completed", "head_sha": f})
    tasks[6]["created"], tasks[6]["updated"] = stamp(22), stamp(23)
    agents = [
        {
            "name": name,
            "harness": harness,
            "provider": provider,
            "model": manifest["models"][name],
            "model_source": "env",
            "harness_version": version,
            "worker_instance_id": name + "-instance",
            "workspace_id": name + "-id",
            "status": "released",
        }
        for name, harness, provider, version in (
            ("bob", "claude-code", "anthropic", "2.1.276"),
            ("charlie", "codex", "openai", "0.154.0"),
        )
    ]
    decisions: list[dict[str, Any]] = [
        {"key": "plan", "summary": "PLAN", "ts": stamp(0), "rationale": "acceptance"}
    ]
    traces: dict[str, Any] = {
        "alice": [],
        "bob": [{"name": "Bash", "input": {"command": "git push origin branch"}}],
        "charlie": [{"name": "Bash", "input": {"command": "git fetch origin"}}],
    }
    for label, sha, second in (("head", b, 10), ("base", c, 15), ("final", f, 19)):
        gate = {
            "expected_head_sha": sha,
            "current_head_sha": c if label == "head" else sha,
            "head_matches": label != "head",
            "base_behind_main": label == "base",
            "ci": "pass",
            "mergeable": "clean",
            "pr_state": "open",
        }
        decisions.append(
            {
                "key": "step6:gate:" + label,
                "rationale": json.dumps(gate),
                "summary": label,
                "ts": stamp(second),
            }
        )
        traces["alice"].append(
            {
                "name": "mcp__hub__check_merge_gate",
                "input": {"pr_url": url, "expected_head_sha": sha},
                "result": gate,
            }
        )
    traces["alice"].append(
        {
            "name": "Bash",
            "input": {
                "command": f"gh pr merge {url} --squash --delete-branch --match-head-commit {f}"
            },
        }
    )
    for key, second, rationale in (
        ("wrap-up", 21, "wrap"),
        ("release:bob", 24, "release"),
        ("release:charlie", 25, "release"),
        ("follow-ups", 21, '{"urls": []}'),
    ):
        decisions.append(
            {"key": "step6:" + key, "ts": stamp(second), "summary": key, "rationale": rationale}
        )
    for name in ("bob", "charlie"):
        traces["alice"].append(
            {"name": "mcp__hub__release_agent", "input": {"agent": name}, "timestamp": stamp(24)}
        )
    snapshot = {
        "task": tasks,
        "agent": agents,
        "decision": decisions,
        "workflow": [
            {
                "status": "done",
                "policy_json": json.dumps(policy),
                "goal": "RoboNater/robo-agents-sandbox#9 and close out with no roadmap edit",
            }
        ],
    }
    facts = {
        "work_pr": {
            "number": 10,
            "state": "MERGED",
            "headRefOid": f,
            "mergedAt": stamp(20),
            "mergedBy": {"login": "RoboNater"},
            "body": "Closes #9 unique-canary",
        },
        "base_pr": {"state": "MERGED", "headRefOid": g, "mergeCommit": {"oid": e}},
        "head_commit": {"parents": [{"sha": b}]},
        "merge_commit": {"parents": [{"sha": e}]},
        "base_ancestor_of_final": True,
        "merge_tree_matches_final": True,
        "issue": {"state": "CLOSED", "body": "unique-canary"},
        "follow_ups": {},
        "telemetry": {
            name: [{"outcome": "release", "worker_instance_id": name + "-instance"}]
            for name in ("bob", "charlie")
        },
        "checks": {
            sha: [
                {
                    "name": "test",
                    "head_sha": sha,
                    "conclusion": "success",
                    "status": "completed",
                    "completed_at": stamp(second),
                }
            ]
            for sha, second in ((b, 6), (c, 11), (f, 18), (g, 13))
        },
        "comments": [
            {
                "html_url": result["review_url"],
                "created_at": task["updated"],
                "body": "Reviewer agent Charlie on behalf of RoboNater "
                + result["reviewed_head_sha"],
            }
            for task, result in STEP6.results(snapshot)
            if task["role"] == "reviewer"
        ],
    }
    return manifest, snapshot, facts, traces


def test_complete_correlated_proof(proof: tuple[Any, ...]) -> None:
    evidence = STEP6.evaluate(*proof)
    assert evidence["passed"], evidence["failed_checks"]


@pytest.mark.parametrize(
    "defect",
    [
        "plan",
        "policy",
        "role",
        "review",
        "address",
        "moved_review",
        "rebase",
        "ci",
        "stale_ci",
        "head",
        "merger",
        "merge_method",
        "release",
        "canary",
        "workspace_access",
        "follow_ups",
    ],
)
def test_verifier_rejects_missing_or_wrong_evidence(proof: tuple[Any, ...], defect: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    if defect == "plan":
        snapshot["decision"] = [row for row in snapshot["decision"] if row["key"] != "plan"]
    elif defect == "policy":
        snapshot["workflow"][0]["policy_json"] = "{}"
    elif defect == "role":
        snapshot["task"][1]["assignee"] = "bob"
    elif defect in ("review", "address", "moved_review", "rebase"):
        index = {"review": 1, "address": 2, "moved_review": 4, "rebase": 5}[defect]
        del snapshot["task"][index]
    elif defect == "ci":
        facts["checks"] = {}
    elif defect == "stale_ci":
        for rows in facts["checks"].values():
            rows[0]["head_sha"] = "0" * 40
    elif defect == "head":
        facts["work_pr"]["headRefOid"] = "0" * 40
    elif defect == "merger":
        traces["alice"] = []
    elif defect == "merge_method":
        facts["merge_commit"]["parents"].append({"sha": "0" * 40})
    elif defect == "release":
        next(row for row in snapshot["decision"] if row["key"] == "step6:release:charlie")["ts"] = (
            stamp(1)
        )
    elif defect == "canary":
        facts["issue"]["body"] = ""
    elif defect == "workspace_access":
        traces["charlie"].append({"name": "Bash", "input": {"command": "cat ../bob/file.py"}})
    elif defect == "follow_ups":
        facts["follow_ups"] = {"unrecorded": {}}
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert not evidence["passed"]


def test_local_prepare_and_all_launchers(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
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
    directory = tmp_path / "persistent run"
    subprocess.run(
        [
            str(ROOT / "scripts/prepare-step6-demo.sh"),
            str(directory),
            "--local-repository",
            str(origin),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = STEP6.load_manifest(directory)
    token = (directory / "token").read_text().strip()
    assert token not in (directory / "run.json").read_text()
    assert not manifest.get("issue")
    assert (
        manifest["workspaces"]["bob"]["workspace_id"]
        != manifest["workspaces"]["charlie"]["workspace_id"]
    )
    for name in ("bob", "charlie"):
        configuration = json.loads((directory / f"{name}.mcp.json").read_text())
        assert configuration["mcpServers"]["hub"]["env"]["HUB_WORKSPACE"] == str(directory / name)
        assert "$AGENT_NAME" not in (directory / f"{name}.prompt.md").read_text()
    codex = tomllib.loads((directory / "codex-home/config.toml").read_text())
    assert set(codex["mcp_servers"]) == {"hub"}
    assert codex["sandbox_mode"] == "workspace-write"
    assert set(codex["mcp_servers"]["hub"]["enabled_tools"]) == set(STEP6.TOOLS)
    assert "<account>" not in (directory / "alice.prompt.md").read_text()
    manifest["issue"] = {"number": 9, "url": "https://github.com/" + STEP6.SANDBOX + "/issues/9"}
    STEP6.save(directory / "run.json", manifest)
    fake = tmp_path / "bin"
    fake.mkdir()
    # These commands execute the real launcher paths without starting paid runtimes.
    for name in ("claude", "codex"):
        script = fake / name
        script.write_text(
            "#!/usr/bin/env python3\nimport os, sys, json\n"
            'print(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}))\n'
        )
        script.chmod(0o755)
    curl = fake / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl.chmod(0o755)
    env = os.environ | {"PATH": str(fake) + os.pathsep + os.environ["PATH"]}
    for name in ("alice", "bob", "charlie"):
        output = subprocess.run(
            [str(ROOT / f"scripts/launch-step6-{name}.sh"), str(directory)],
            env=env,
            capture_output=True,
            text=True,
        )
        if name == "bob":
            assert output.returncode == 1 and "before" in output.stderr
        else:
            assert output.returncode == 0, output.stderr
        record = json.loads(output.stdout)
        expected = "alice-runtime" if name == "alice" else "bob" if name == "bob" else None
        if expected:
            assert record["cwd"] == str(directory / expected)
        if name == "charlie":
            assert record["args"][record["args"].index("-C") + 1] == str(directory / "charlie")
            assert "--ephemeral" in record["args"] and "--approve-for-me" in record["args"]
    # Execute both remaining script entry points against an incomplete fixture: fail closed.
    for filename in ("run-step6-disturbances.py", "verify-step6-demo.sh"):
        output = subprocess.run(
            [str(ROOT / "scripts" / filename), str(directory)], capture_output=True, text=True
        )
        assert output.returncode != 0
    with pytest.raises(ValueError, match="reuse"):
        STEP6.prepare(directory, str(origin), False)
    manifest["repository"] = "another/repository"
    STEP6.save(directory / "run.json", manifest)
    with pytest.raises(ValueError, match="only"):
        STEP6.load_manifest(directory)


def test_shell_scripts_parse_and_are_executable() -> None:
    for script in ROOT.glob("scripts/*step6*.sh"):
        assert os.access(script, os.X_OK)
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_driver_waits_for_exact_approvals_and_recovers_merged_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    manifest, snapshot, facts, _ = copy.deepcopy(proof)
    directory = tmp_path / "run"
    directory.mkdir()
    manifest.update(
        {
            "run_dir": str(directory),
            "implementation_branch": "run/implement",
            "base_branch": "run/base",
        }
    )
    manifest["workspaces"]["driver"] = {"path": str(directory / "driver")}
    view = facts["work_pr"] | {
        "url": manifest["work_pr"]["url"],
        "headRefName": "run/implement",
        "baseRefName": "main",
        "state": "OPEN",
    }
    view["headRefOid"] = manifest["disturbances"]["head"]["new_head"]
    STEP6.save(directory / "run.json", manifest)
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)
    monkeypatch.setattr(STEP6, "pr_view", lambda _: view)
    commands: list[tuple[Any, ...]] = []

    def runner(*args: Any, **kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:" + str(STEP6.SANDBOX) + ".git"
        return ""

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    snapshot["task"][4]["result_json"] = json.dumps(
        {"verdict": "approved", "reviewed_head_sha": "0" * 40}
    )
    assert not STEP6.driver_once(directory)
    assert not any("push" in args or "merge" in args for args in commands)
    # Before exact initial approval, even a completed ADDRESS cannot trigger a push.
    manifest["disturbances"] = {}
    view["headRefOid"] = "0" * 40
    STEP6.save(directory / "run.json", manifest)
    assert not STEP6.driver_once(directory)
    assert not any("push" in args or "merge" in args for args in commands)


def test_driver_executes_only_named_disturbances_and_recovers_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    manifest, snapshot, facts, _ = copy.deepcopy(proof)
    directory = tmp_path / "run"
    directory.mkdir()
    driver = directory / "driver"
    driver.mkdir()
    head = copy.deepcopy(manifest["disturbances"]["head"])
    base = copy.deepcopy(manifest["disturbances"]["base"])
    manifest.update(
        {
            "run_dir": str(directory),
            "implementation_branch": "run/implement",
            "base_branch": "run/base",
            "disturbances": {},
        }
    )
    manifest["workspaces"]["driver"] = {"path": str(driver)}
    STEP6.save(directory / "run.json", manifest)
    work = facts["work_pr"] | {
        "url": manifest["work_pr"]["url"],
        "headRefName": "run/implement",
        "baseRefName": "main",
        "state": "OPEN",
        "headRefOid": head["old_head"],
    }
    base_pr: dict[str, Any] = {
        "state": "OPEN",
        "headRefOid": base["head"],
        "mergeCommit": {"oid": base["new_main"]},
        "mergedAt": stamp(14),
    }
    commands: list[tuple[Any, ...]] = []
    remote: dict[str, str] = {}
    current = {"fetch": head["old_head"], "head": head["old_head"]}

    def runner(*args: Any, **kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:" + str(STEP6.SANDBOX) + ".git"
        if args[:3] == ("git", "fetch", "origin"):
            current["fetch"] = (
                base["old_main"]
                if args[-1] == "main"
                else remote.get("implement", head["old_head"])
            )
        if args[:3] == ("git", "rev-parse", "FETCH_HEAD"):
            return str(current["fetch"])
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return str(current["head"])
        if args[:3] == ("git", "rev-parse", head["new_head"] + "^"):
            return str(head["old_head"])
        if args[:2] == ("git", "show"):
            return str(manifest["canary"])
        if args[:2] == ("git", "commit"):
            current["head"] = head["new_head"] if "head disturbance" in args[-1] else base["head"]
        if args[:2] == ("git", "ls-remote"):
            return str(remote.get("base", ""))
        if args[:2] == ("git", "push"):
            remote["implement" if args[-1] == "HEAD:run/implement" else "base"] = current["head"]
            if "implement" in remote:
                work["headRefOid"] = remote["implement"]
        if args[:3] == ("gh", "pr", "create"):
            return "https://github.com/" + str(STEP6.SANDBOX) + "/pull/11"
        if args[:3] == ("gh", "pr", "merge"):
            base_pr["state"] = "MERGED"
        return ""

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)
    monkeypatch.setattr(STEP6, "pr_view", lambda number: base_pr if str(number) == "11" else work)
    monkeypatch.setattr(STEP6, "gh", lambda *args: [])
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    monkeypatch.setattr(STEP6, "checks", lambda _: [{"name": "test", "conclusion": "success"}])
    assert not STEP6.driver_once(directory)
    recorded = STEP6.load_manifest(directory)
    assert recorded["disturbances"]["head"]["old_head"] == head["old_head"]
    assert recorded["disturbances"]["head"]["new_head"] == head["new_head"]
    assert sum(args[:2] == ("git", "push") for args in commands) == 1
    # Simulate a crash after the push but before its final checkpoint.
    recorded["disturbances"]["head"]["state"] = "prepared"
    STEP6.save(directory / "run.json", recorded)
    assert not STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 1
    # Only the exact moved-head approval allows the base PR and checked merge.
    assert STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 2
    assert sum(args[:3] == ("gh", "pr", "merge") for args in commands) == 1
    assert not any(
        args[:3] == ("git", "push", "origin") and args[-1].endswith(":main") for args in commands
    )
    recorded = STEP6.load_manifest(directory)
    # Recover a merge whose remote branch has already been deleted.
    recorded["disturbances"]["base"]["state"] = "prepared"
    remote.pop("base")
    STEP6.save(directory / "run.json", recorded)
    assert STEP6.driver_once(directory)
    assert STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 2
    assert sum(args[:3] == ("gh", "pr", "create") for args in commands) == 1
    assert sum(args[:3] == ("gh", "pr", "merge") for args in commands) == 1


def test_gate_response_is_correlated_by_tool_id() -> None:
    gate = {"expected_head_sha": "a" * 40, "head_matches": False}
    lines = [
        {
            "timestamp": stamp(1),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "mcp__hub__check_merge_gate",
                        "input": {"expected_head_sha": "a" * 40},
                    }
                ]
            },
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "unrelated",
                        "content": json.dumps(
                            {"expected_head_sha": "b" * 40, "head_matches": True}
                        ),
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [{"type": "text", "text": json.dumps(gate)}],
                    }
                ]
            }
        },
    ]
    calls = STEP6.tool_calls("\n".join(json.dumps(line) for line in lines))
    assert calls[0]["result"] == gate


def test_fabricated_gate_log_does_not_satisfy_proof(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    traces["alice"][0]["result"] = {"head_matches": True}
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert "head_gate_response_correlated" in evidence["failed_checks"]
