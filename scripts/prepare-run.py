#!/usr/bin/env python3
"""Generate a whole user run directory in one command.

Takes a target repository and a run directory and produces the layout the
user guide describes: bootstrapped bob/charlie clones, a state directory with
a reused bearer token, rendered MCP configs from the ``runtimes/`` templates,
a run-local Codex home with its auth link, rendered worker prompts, cheap
prerequisite checks, and paste-ready launch commands plus the Alice kickoff
prompt.

Example:
    uv run --locked python scripts/prepare-run.py \\
        --repository git@github.com:your-org/your-repo.git \\
        --run-dir /absolute/path/to/my-run \\
        --issue 42 --account your-github-username

Supported worker harnesses are ``claude-code`` and ``codex`` (the paste-ready
pair); anything else fails up front with an actionable message.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_common import (  # noqa: E402
    PROVIDERS,
    ROOT,
    VERSION_COMMANDS,
    bootstrap_clone,
    clone_source,
    codex_home,
    ensure_token,
    link_or_copy,
    parse_github_slug,
    parse_harness_version,
    render_claude_mcp,
    render_codex_config,
    render_worker_prompt,
    run,
    run_output,
    save,
    write_private_text,
)

DEFAULT_BOB_HARNESS = "claude-code"
DEFAULT_CHARLIE_HARNESS = "codex"
#: Worker harnesses prepare-run can render configs *and* print verified
#: paste-ready launch lines for. opencode needs its serve/attach supervisor
#: loop and gemini CLI flags are unverified, so those topologies stay on the
#: manual walkthrough in docs/user-guide.md.
SUPPORTED_HARNESSES = ("claude-code", "codex")


def worker_env(
    name: str,
    harness: str,
    version: str,
    provider: str,
    model: str,
    capabilities: str,
    workspace: str,
    token: str,
    telemetry: str,
) -> dict[str, str]:
    return {
        "HUB_URL": "http://127.0.0.1:8420",
        "HUB_TOKEN": token,
        "AGENT_NAME": name,
        "HUB_WORKSPACE": workspace,
        "HUB_HARNESS": harness,
        "HUB_HARNESS_VERSION": version,
        "HUB_PROVIDER": provider,
        "HUB_MODEL": model,
        "HUB_CAPABILITIES": capabilities,
        "HUB_TELEMETRY_LOG": telemetry,
        "PYTHONUTF8": "1",
    }


def check_gh_auth() -> None:
    try:
        run("gh", "auth", "status")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "gh is not authenticated (gh auth status failed); run `gh auth login` and retry"
        ) from exc


def repo_settings(slug: str) -> dict[str, Any]:
    try:
        return json.loads(
            run(
                "gh",
                "repo",
                "view",
                slug,
                "--json",
                "viewerPermission,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed",
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"cannot read repository settings for {slug}; "
            "check `gh auth status` and the repository name"
        ) from exc


def repo_has_workflows(slug: str) -> bool:
    try:
        payload = json.loads(run("gh", "api", f"repos/{slug}/actions/workflows"))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"cannot list workflows for {slug}; check `gh auth status` and repo access"
        ) from exc
    workflows = payload.get("workflows", []) if isinstance(payload, dict) else []
    return bool(workflows)


def codex_login_status(home: Path) -> str:
    try:
        stdout, stderr = run_output(
            "codex", "login", "status", env={"CODEX_HOME": str(home)}
        )
    except (OSError, subprocess.CalledProcessError):
        return "not logged in (codex login status failed)"
    # Codex CLI prints its status line to stderr on some platforms (exit 0),
    # possibly after WARNING preamble lines; prefer the first non-warning line.
    output = stdout or stderr
    candidates = [line for line in output.splitlines() if line.strip()]
    preferred = [line for line in candidates if not line.lstrip().startswith("WARNING:")]
    line = (preferred or candidates or [""])[0]
    return line if line else "logged in (empty status)"


def render_alice_prompt(
    slug: str | None,
    repository: str,
    issue: int | None,
    account: str | None,
    policy: dict[str, Any],
) -> str:
    prompt = (ROOT / "prompts/alice.md").read_text(encoding="utf-8")
    begin = prompt.index("Goal:")
    end = prompt.index("GitHub comment identity account:")
    if slug is not None and issue is not None:
        goal = (
            f"Address issue `{slug}#{issue}`, merge its pull request, "
            "and close out with no roadmap edit; record the merge only in the workflow summary"
        )
    elif issue is not None:
        goal = (
            f"Address issue `{repository}#{issue}`, merge its pull request, "
            "and close out with no roadmap edit; record the merge only in the workflow summary"
        )
    else:
        goal = (
            "Address issue `<issue-owner>/<issue-repository>#<issue>`, merge its pull "
            "request, and close out with no roadmap edit; "
            "record the merge only in the workflow summary"
        )
    prompt = prompt[:begin] + "Goal: " + goal + ".\n\n" + prompt[end:]
    prompt = prompt.replace("<account>", account if account else "<account>")
    begin = prompt.index("```json") + len("```json")
    end = prompt.index("```", begin)
    prompt = prompt[:begin] + "\n" + json.dumps(policy, indent=2) + "\n" + prompt[end:]
    return prompt


def codex_launch(home: Path, git_dir: Path, prompt: Path) -> list[str]:
    """Paste-ready ``codex exec`` lines for the current platform.

    The ``VAR=value cmd ... < file`` prefix and ``<`` redirection are POSIX
    shell syntax; PowerShell needs ``$env:`` assignments and pipes the prompt
    through ``Get-Content`` instead.
    """
    if os.name == "nt":
        return [
            f'$env:CODEX_HOME = "{home}"',
            f'Get-Content -Raw "{prompt}" | codex exec --ephemeral -C . '
            f'--add-dir "{git_dir}" --approve-for-me -',
        ]
    return [
        f'CODEX_HOME="{home}" codex exec --ephemeral -C . '
        f'--add-dir "{git_dir}" --approve-for-me - < "{prompt}"',
    ]


def launch_lines(
    run_dir: Path,
    configs: Path,
    alice_runtime: Path,
    bob_dir: Path,
    charlie_dir: Path,
    bob_harness: str,
    charlie_harness: str,
) -> list[str]:
    """Paste-ready launch commands, one block per agent; paths are quoted."""
    lines = [
        f'cd "{alice_runtime}"',
        f'claude --strict-mcp-config --mcp-config "{configs / "alice.mcp.json"}"',
        "",
        f'cd "{bob_dir}"',
    ]
    if bob_harness == "codex":
        lines += codex_launch(
            configs / "bob-codex", bob_dir / ".git", run_dir / "bob.prompt.md"
        )
    else:
        lines.append(
            f'claude --strict-mcp-config --mcp-config "{configs / "bob.mcp.json"}"'
        )
    lines += ["", f'cd "{charlie_dir}"']
    if charlie_harness == "codex":
        lines += codex_launch(
            configs / "codex", charlie_dir / ".git", run_dir / "charlie.prompt.md"
        )
    else:
        lines.append(
            f'claude --strict-mcp-config --mcp-config "{configs / "charlie.mcp.json"}"'
        )
    return lines


def prepare(
    repository: str,
    run_dir: Path,
    issue: int | None = None,
    account: str | None = None,
    bob_harness: str = DEFAULT_BOB_HARNESS,
    charlie_harness: str = DEFAULT_CHARLIE_HARNESS,
    bob_model: str = "",
    charlie_model: str = "",
    bob_provider: str | None = None,
    charlie_provider: str | None = None,
    bob_capabilities: str = "",
    charlie_capabilities: str = "",
    bob_dir: Path | None = None,
    charlie_dir: Path | None = None,
    state_dir: Path | None = None,
    merge_method: str = "squash",
    allow_no_ci: str = "auto",
    skip_github_checks: bool = False,
    public_url: str = "http://127.0.0.1:8420",
) -> dict[str, Any]:
    if not run_dir.is_absolute() or run_dir != run_dir.resolve():
        raise ValueError("RUN_DIR must be absolute and canonical")
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("RUN_DIR must be outside the coordination checkout")
    if not repository:
        raise ValueError("--repository must not be empty")
    for label, harness in (("bob", bob_harness), ("charlie", charlie_harness)):
        if harness not in SUPPORTED_HARNESSES:
            raise ValueError(
                f"--{label} harness {harness!r} is not yet supported by prepare-run; "
                f"expected one of {list(SUPPORTED_HARNESSES)} "
                "(assemble other topologies via the manual walkthrough in "
                "docs/user-guide.md)"
            )
    if merge_method not in ("squash", "merge", "rebase"):
        raise ValueError("--merge-method must be one of squash, merge, rebase")
    if allow_no_ci not in ("auto", "true", "false"):
        raise ValueError("--allow-no-ci must be one of auto, true, false")

    providers = {
        "bob": bob_provider or PROVIDERS[bob_harness],
        "charlie": charlie_provider or PROVIDERS[charlie_harness],
    }

    bob_path = bob_dir or (run_dir / "bob")
    charlie_path = charlie_dir or (run_dir / "charlie")
    for path, label in ((bob_path, "--bob-dir"), ((charlie_path), "--charlie-dir")):
        if not path.is_absolute() or path != path.resolve():
            raise ValueError(f"{label} must be absolute and canonical")
    if bob_path == charlie_path:
        raise ValueError("--bob-dir and --charlie-dir must differ")
    resolved_state = state_dir or (run_dir / "hub-state")
    if not resolved_state.is_absolute() or resolved_state != resolved_state.resolve():
        raise ValueError("--state-dir must be absolute and canonical")

    # Harness versions come from the CLIs themselves, never placeholders.
    versions: dict[str, str] = {}
    parsed_versions: dict[str, str] = {}
    for harness in sorted({bob_harness, charlie_harness}):
        command = VERSION_COMMANDS[harness]
        try:
            output = run(command, "--version")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                f"harness {harness!r} requires the {command!r} CLI on PATH "
                f"({command} --version failed); install and authenticate it first"
            ) from exc
        versions[command] = output
        parsed_versions[harness] = parse_harness_version(harness, output)

    slug = parse_github_slug(repository)
    # A bare owner/repo slug passes the gh checks below but is not a valid
    # `git clone` argument; expand it to its https URL for bootstrapping.
    clone_from = clone_source(repository, slug)
    if slug is None and not skip_github_checks:
        # Local paths (e.g. disposable test origins) have no GitHub API surface.
        skip_github_checks = True
    if not skip_github_checks:
        check_gh_auth()
    policy_allow_no_ci = False
    merge_note = "skipped (local repository or --skip-github-checks)"
    ci_note = "skipped (local repository or --skip-github-checks)"
    if slug is not None and not skip_github_checks:
        settings = repo_settings(slug)
        if settings.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
            raise ValueError(
                f"repository {slug} requires push access "
                f"(viewerPermission {settings.get('viewerPermission')!r}); "
                "check `gh auth status` and repo permissions"
            )
        allowed = {
            "squash": settings.get("squashMergeAllowed"),
            "merge": settings.get("mergeCommitAllowed"),
            "rebase": settings.get("rebaseMergeAllowed"),
        }
        if not allowed[merge_method]:
            raise ValueError(
                f"repository {slug} does not allow the {merge_method!r} merge method; "
                "enable it in repository settings or pass another --merge-method"
            )
        merge_note = f"{slug} allows {merge_method}"
        has_workflows = repo_has_workflows(slug)
        ci_note = (
            f"{slug} has workflows (allow_no_ci=false)"
            if has_workflows
            else f"{slug} has no workflows (allow_no_ci=true)"
        )
        detected = not has_workflows
        policy_allow_no_ci = detected if allow_no_ci == "auto" else allow_no_ci == "true"
    else:
        if allow_no_ci == "auto":
            policy_allow_no_ci = slug is None
            if slug is None:
                ci_note = "local repository has no GitHub workflows (allow_no_ci=true)"
        else:
            policy_allow_no_ci = allow_no_ci == "true"

    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = run_dir / "run.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("run_dir") != str(run_dir):
            raise ValueError("manifest belongs to a different run directory")
        if previous.get("repository") != repository:
            raise ValueError(
                "run directory already prepared for another repository; use a fresh one"
            )

    # Bootstrap before minting the token so a failed clone leaves no state behind.
    workspaces = {
        "bob": bootstrap_clone("bob", bob_path, clone_from),
        "charlie": bootstrap_clone("charlie", charlie_path, clone_from),
    }
    token = ensure_token(resolved_state / "token")
    if workspaces["bob"]["workspace_id"] == workspaces["charlie"]["workspace_id"]:
        raise ValueError("bob and charlie must have distinct workspace IDs")
    # Keep the drive-letter case bootstrap printed; read_identity compares the string.
    bob_workspace = workspaces["bob"]["path"]
    charlie_workspace = workspaces["charlie"]["path"]

    configs = run_dir / "configs"
    configs.mkdir(mode=0o700, exist_ok=True)

    harnesses = {"bob": bob_harness, "charlie": charlie_harness}
    models = {"bob": bob_model, "charlie": charlie_model}
    capabilities = {"bob": bob_capabilities, "charlie": charlie_capabilities}
    rendered_configs: dict[str, str] = {}

    for name in ("bob", "charlie"):
        harness = harnesses[name]
        workspace = bob_workspace if name == "bob" else charlie_workspace
        telemetry = str(run_dir / f"{name}-telemetry.jsonl")
        env = worker_env(
            name,
            harness,
            parsed_versions[harness],
            providers[name],
            models[name],
            capabilities[name],
            workspace,
            token,
            telemetry,
        )
        if harness == "codex":
            home_name = "codex" if name == "charlie" else "bob-codex"
            home = codex_home(configs, home_name)
            worker_args = ["run", "--locked", "--directory", str(ROOT), "worker-mcp"]
            write_private_text(home / "config.toml", render_codex_config(env, worker_args))
            rendered_configs[name] = str(home / "config.toml")
        else:
            save(configs / f"{name}.mcp.json", render_claude_mcp(env))
            rendered_configs[name] = str(configs / f"{name}.mcp.json")

    hub_env = {
        "HUB_STATE_DIR": str(resolved_state),
        "HUB_TOKEN": token,
        "HUB_PUBLIC_URL": public_url,
        "HUB_GUIDES_DIR": str(ROOT / "guides"),
        "PYTHONUTF8": "1",
    }
    hub_args = ["run", "--locked", "--directory", str(ROOT), "hub"]
    save(
        configs / "alice.mcp.json",
        {"mcpServers": {"hub": {"command": "uv", "args": hub_args, "env": hub_env}}},
    )

    # Alice's working directory, kept apart from the worker clones and the
    # token: the orchestrator skill is linked in run-locally (as in step6), so
    # the quickstart needs no user-wide skill installation.
    alice_runtime = run_dir / "alice-runtime"
    (alice_runtime / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    link_or_copy(
        ROOT / "skills/alice-orchestrator",
        alice_runtime / ".claude" / "skills" / "alice-orchestrator",
    )

    for name in ("bob", "charlie"):
        (run_dir / f"{name}.prompt.md").write_text(
            render_worker_prompt(name), encoding="utf-8"
        )

    policy = {
        "max_review_rounds": 3,
        "merge_method": merge_method,
        "allow_no_ci": policy_allow_no_ci,
        "role_policy": {
            "reviewer_harness_differs": bob_harness != charlie_harness,
            # Baseline from prompts/alice.md; Alice still observes any real
            # provider difference at pairing time via check-in profiles.
            "reviewer_provider_differs": False,
            "implementer_capabilities": capabilities["bob"].split(",")
            if capabilities["bob"]
            else [],
            "reviewer_capabilities": capabilities["charlie"].split(",")
            if capabilities["charlie"]
            else [],
        },
        "pairing_wait_s": 120,
        "max_wall_minutes": 180,
        "max_task_lease_min": 120,
    }
    (run_dir / "alice.prompt.md").write_text(
        render_alice_prompt(slug, repository, issue, account, policy), encoding="utf-8"
    )

    manifest = {
        "schema_version": 1,
        "repository": repository,
        "clone_repository": clone_from,
        "slug": slug,
        "run_dir": str(run_dir),
        "state_dir": str(resolved_state),
        "workspaces": workspaces,
        "harnesses": harnesses,
        "providers": providers,
        "models": models,
        "versions": versions,
        "policy": policy,
        "issue": issue,
        "account": account,
        "merge_method": merge_method,
        "configs": rendered_configs,
    }
    save(manifest_path, manifest)

    codex_auth: dict[str, str] = {}
    for name in ("bob", "charlie"):
        if harnesses[name] == "codex":
            home_name = "codex" if name == "charlie" else "bob-codex"
            codex_auth[name] = f"{home_name}: {codex_login_status(configs / home_name)}"
    if not codex_auth:
        codex_auth = {"codex": "no codex worker in this topology"}

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "repository": repository,
                "slug": slug,
                "issue": issue,
                "workspaces": {
                    name: workspaces[name]["path"] for name in ("bob", "charlie")
                },
                "configs": {"alice": str(configs / "alice.mcp.json"), **rendered_configs},
                "prompts": {
                    name: str(run_dir / f"{name}.prompt.md") for name in ("alice", "bob", "charlie")
                },
                "checks": {
                    "gh_auth": "ok" if not skip_github_checks else "skipped",
                    "versions": versions,
                    "merge": merge_note,
                    "ci": ci_note,
                    "codex_auth": codex_auth,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    print("\nLaunch commands (paste in order):")
    for line in launch_lines(
        run_dir,
        configs,
        alice_runtime,
        Path(bob_workspace),
        Path(charlie_workspace),
        bob_harness,
        charlie_harness,
    ):
        print(line if line else "")
    print(f"\nAlice kickoff prompt: {run_dir / 'alice.prompt.md'}")
    if issue is None or account is None:
        print("Fill any remaining <issue>/<account> placeholders before launching Alice.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--bob", default=DEFAULT_BOB_HARNESS)
    parser.add_argument("--charlie", default=DEFAULT_CHARLIE_HARNESS)
    parser.add_argument("--bob-model", default="")
    parser.add_argument("--charlie-model", default="")
    parser.add_argument("--bob-provider", default=None)
    parser.add_argument("--charlie-provider", default=None)
    parser.add_argument("--bob-capabilities", default="")
    parser.add_argument("--charlie-capabilities", default="")
    parser.add_argument("--bob-dir", type=Path, default=None)
    parser.add_argument("--charlie-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--merge-method", default="squash")
    parser.add_argument("--allow-no-ci", default="auto")
    parser.add_argument("--skip-github-checks", action="store_true")
    parser.add_argument("--public-url", default="http://127.0.0.1:8420")
    args = parser.parse_args()
    try:
        prepare(
            args.repository,
            args.run_dir,
            args.issue,
            args.account,
            args.bob,
            args.charlie,
            args.bob_model,
            args.charlie_model,
            args.bob_provider,
            args.charlie_provider,
            args.bob_capabilities,
            args.charlie_capabilities,
            args.bob_dir,
            args.charlie_dir,
            args.state_dir,
            args.merge_method,
            args.allow_no_ci,
            args.skip_github_checks,
            args.public_url,
        )
    except ValueError as exc:
        sys.exit(f"prepare-run: error: {exc}")


if __name__ == "__main__":
    main()
