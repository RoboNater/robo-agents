# Feasibility and Impact of Supporting GitLab-Centric Workflows

## 1. Executive Summary & Verdict

This document assesses the feasibility and architectural impact of expanding **robo-agents** from its current GitHub-centric proof-of-concept (PoC) into supporting **GitLab**-centric development workflows (issues, merge requests, CI pipelines, discussions, and automated merge gating).

### Verdict: **High Feasibility, Moderate Impact**
- **Feasibility is High**: The fundamental coordination model of robo-agents—the **hub-centric pull model**, A2A JSON-RPC messaging, SQLite-backed durable state, lease/heartbeat sweeper, worker role specialization, and prompt-injection defense—is **completely forge-agnostic**. Crucially, GitLab provides **100% functional parity** for every core invariant enforced by the workflow (including SHA-bound merges, stale-base detection, CI gating, and conflict detection).
- **Impact is Moderate and Well-Bounded**: The dependency on GitHub is isolated to specific peripheral layers:
  1. The pre-merge verification gate (`packages/hub/src/agent_hub/merge_gate.py`).
  2. Orchestrator and worker role prompts/guides (`skills/alice-orchestrator/SKILL.md`, `guides/*.md`).
  3. Preflight checks and workspace bootstrapping scripts (`scripts/prepare-run.py`, `scripts/run_common.py`).
  4. Repository URL parsing and issue/change naming conventions.

No core protocol revisions (A2A message format, task state machines, or SQLite transactional semantics) are required.

---

## 2. Current GitHub Coupling Footprint

In the current PoC, GitHub integration is concentrated across five functional areas:

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ Current robo-agents Architecture & Forge Touchpoints                           │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  [Alice (Orchestrator)]                                                        │
│    │ • skill: alice-orchestrator                                               │
│    │ • uses: gh issue view, gh pr merge --match-head-commit                     │
│    ▼                                                                           │
│  [Hub Service (agent_hub)]                                                     │
│    │ • MCP Tool: check_merge_gate                                              │
│    │ • packages/hub/src/agent_hub/merge_gate.py (invokes `gh` CLI subprocess)  │
│    │ • PR_URL_RE = re.compile(r".../pull/(?P<number>[1-9][0-9]*)")             │
│    ▼                                                                           │
│  [Worker MCP (worker_mcp) & Guides]                                            │
│    │ • guides/implementer.md: gh pr create, gh pr view --json headRefOid       │
│    │ • guides/reviewer.md: gh pr comment                                       │
│    │ • guides/rebase.md: gh pr view --json headRefOid                          │
│    ▼                                                                           │
│  [Data Models & Protocol (agent_hub_common)]                                   │
│    │ • models.py: pr_url, head_sha, reviewed_head_sha                          │
│    │ • constants.py: MetaKeys.PR_HEAD_SHA = "hub.pr_head_sha"                  │
│    │ • database.py: task.pr_head_sha column                                    │
│    ▼                                                                           │
│  [Run Lifecycle Scripts]                                                       │
│    │ • scripts/prepare-run.py: gh auth status, gh repo view, gh api workflows  │
│    │ • scripts/run_common.py: parse_github_slug, gh config git_protocol        │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Direct Coupling Points:
1. **`merge_gate.py`**:
   - Strictly matches GitHub pull request URLs via `PR_URL_RE` (`https://<host>/<owner>/<repo>/pull/<number>`).
   - Invokes `gh pr view`, `gh pr checks`, and `gh api repos/{owner}/{repo}/compare/...`.
   - Inspects GitHub Actions workflows via `gh api repos/{owner}/{repo}/actions/workflows`.
2. **`alice-orchestrator` Skill**:
   - Instructs Alice to read issues via `gh issue view`.
   - Enforces SHA-bound merge via `gh pr merge <pr_url> --<method> --delete-branch --match-head-commit <head_sha>`.
3. **Role Guides (`guides/implementer.md`, `guides/reviewer.md`, `guides/rebase.md`)**:
   - Instruct implementers to create PRs via `gh pr create` and verify head via `gh pr view --json headRefOid`.
   - Instruct reviewers to post review comments via `gh pr comment`.
   - Instruct rebase agents to verify the updated head via `gh pr view`.
4. **Data Models & Database Schema**:
   - Wire metadata keys: `hub.pr_head_sha`.
   - SQLite table schema: `task.pr_head_sha`.
   - Result models: `ImplementerResult.pr_url`, `ReviewerResult.pr_url`, `RebaseResult.pr_url`.
5. **Run Tooling (`scripts/prepare-run.py`, `scripts/run_common.py`)**:
   - Assumes 2-part repository slugs (`owner/repo`).
   - Executes `gh auth status` and reads GitHub repository settings (`viewerPermission`, `squashMergeAllowed`, etc.).

---

## 3. Paradigm & Feature Mapping: GitHub vs. GitLab

GitLab's workflow model maps cleanly to GitHub's, but introduces architectural differences that must be accounted for:

| Workflow Concept | GitHub Paradigm | GitLab Paradigm | robo-agents Mapping & Notes |
|---|---|---|---|
| **Namespace & Hierarchy** | 2-level: `owner/repo` | Multi-level: `group/subgroup1/.../project` or `user/project` | GitLab projects can have arbitrary nesting. Slugs cannot assume `len(parts) == 2`. |
| **Change Request** | Pull Request (PR) | Merge Request (MR) | Conceptually identical. URLs use `/-/merge_requests/<iid>` instead of `/pull/<number>`. |
| **Issue Tracking** | Issue `#123` | Issue `#123` (identified by project-scoped `iid`) | Identical semantics. Closing keywords (`Closes #N`, `Fixes #N`) supported natively by both. |
| **Review Comments** | PR Comments & Reviews (`gh pr comment`, `gh pr review`) | Notes & Discussions (`glab mr note`, `POST /notes`) | Both support markdown comments and threads. Under a shared PoC account, both use comment-based approval. |
| **Merge Head Binding** | `gh pr merge --match-head-commit <sha>` | `glab mr merge --sha <sha>` or REST API `sha` param | **Exact Parity**: GitLab API `PUT /merge` rejects merges with HTTP 409 if `sha` does not match the MR HEAD. |
| **CI / Checks** | GitHub Actions Workflows & Check Runs | GitLab CI/CD Pipelines & Jobs | GitLab pipelines report `success`, `failed`, `running`, `pending`, `canceled`, `skipped`. Maps 1:1 to `CiStatus`. |
| **Stale Base Detection** | GitHub Compare API (`behind_by > 0`) | MR `diverged_commits_count > 0` or Compare API | GitLab MR metadata directly provides `diverged_commits_count` without a separate compare call. |
| **Mergeability** | `mergeable` (`MERGEABLE` / `CONFLICTING`) | `has_conflicts` & `detailed_merge_status` | GitLab computes mergeability asynchronously (`merge_status="checking"`), mirroring GitHub's `unknown`. |
| **Discussion Gating** | Branch protection setting | Native MR status (`discussions_not_resolved`) | GitLab natively reports whether unresolved discussions block the merge. |
| **Deployment Model** | Primarily SaaS (`github.com`) | Common SaaS (`gitlab.com`) + Ubiquitous Self-Hosted (CE/EE) | GitLab support requires configurable hostnames, custom ports, and corporate TLS/CA support. |

---

## 4. Deep-Dive: Invariant Parity (§5 Rails)

Robo-agents guarantees safety through strict rails defined in `docs/poc-spec.md` §5. GitLab provides full parity for each rail:

### 4.1 SHA-Bound Merge Invariant
- **The Rule**: Merging is bound to the exact commit SHA approved by the reviewer. If a worker or external party pushes after approval, the merge must fail immediately and trigger a re-review.
- **GitHub Implementation**: `gh pr merge --match-head-commit <approved_head>`.
- **GitLab Implementation**:
  - GitLab REST API: `PUT /projects/:id/merge_requests/:mr_iid/merge` accepts a `sha` parameter:
    > *"If present, then this SHA must match the HEAD of the MR for the merge to succeed."*
    If the head moved, GitLab returns `409 Conflict` (`"SHA does not match HEAD of source branch"`).
  - GitLab CLI: `glab mr merge <id> --sha <approved_head> --squash --remove-source-branch`.
- **Parity**: **100% match**.

### 4.2 CI Gate Invariant
- **The Rule**: Merging requires CI green on the approved head (or repo without CI when `allow_no_ci: true`).
- **GitHub Implementation**: `gh pr checks` mapped to `pass`, `fail`, `pending`, `cancelled`, `no_checks`, `no_workflows`.
- **GitLab Implementation**:
  - The MR object provides `head_pipeline`. Status maps directly:
    - `success` $\rightarrow$ `CiStatus.PASS`
    - `failed` $\rightarrow$ `CiStatus.FAIL`
    - `running`, `pending`, `preparing`, `waiting_for_resource` $\rightarrow$ `CiStatus.PENDING`
    - `canceled` $\rightarrow$ `CiStatus.CANCELLED`
    - `skipped`, `manual` $\rightarrow$ `CiStatus.PASS` (or handled per policy)
  - Absence of CI: Checked via `.gitlab-ci.yml` existence in repository tree or Project CI API (`ci_config_path`).
- **Parity**: **100% match**.

### 4.3 Stale Base & Rebase Invariant
- **The Rule**: An approved PR whose base branch has advanced must not merge directly; it triggers a `rebase` task.
- **GitHub Implementation**: `gh api repos/{owner}/{repo}/compare/<base>...<head>` checking `behind_by > 0`.
- **GitLab Implementation**:
  - GitLab MR endpoint returns `diverged_commits_count` directly.
  - Alternatively, `GET /projects/:id/repository/compare?from=<target>&to=<source>` returns commits ahead/behind.
  - Furthermore, GitLab's `detailed_merge_status` explicitly reports `"need_rebase"` if target moved under fast-forward/semi-linear policy.
- **Parity**: **100% match**.

### 4.4 Mergeability & Conflict Invariant
- **The Rule**: Textual or git conflicts must trigger a `rebase` task or escalation.
- **GitHub Implementation**: `mergeable == "CONFLICTING"` or `mergeStateStatus == "DIRTY"`.
- **GitLab Implementation**:
  - GitLab MR endpoint returns `has_conflicts: true/false`.
  - `detailed_merge_status`: `"conflict"`.
  - Lazy resolution: When GitLab is computing conflicts, `merge_status` is `"checking"` or `"unchecked"`, directly corresponding to GitHub's `"unknown"` polling state.
- **Parity**: **100% match**.

---

## 5. Architectural Evaluation: CLI vs. Direct REST API vs. MCP Tools

There are three primary strategies for interacting with GitLab:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Integration Strategies for GitLab Support                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ Strategy A: CLI-Based (`glab`)                                              │
│  Agent / Hub ──▶ Subprocess (`glab mr ...`) ──▶ GitLab                      │
│  • Pros: Follows existing `gh` subprocess architecture in merge_gate.py.    │
│  • Cons: `glab` is rarely pre-installed in default dev/agent containers;    │
│          version drift across distros; differences in JSON flag output.     │
│                                                                             │
│ Strategy B: Direct REST API (Hub HTTP Client)                               │
│  Agent / Hub ──▶ `httpx.AsyncClient` ──▶ GitLab API v4                      │
│  • Pros: Zero CLI dependencies on host; `agent_hub` already uses httpx;     │
│          deterministic JSON structures; handles self-hosted TLS/tokens;    │
│          much easier to unit-test with mock transports.                     │
│  • Cons: Hub needs forge credentials configured directly.                   │
│                                                                             │
│ Strategy C: Forge MCP Tools (Decoupled Engine)                              │
│  Worker/Alice ──▶ Hub MCP Tools ──▶ Hub Forge Adapter ──▶ GitLab / GitHub   │
│  • Pros: Workers require NO forge CLI or tokens in their workspace clones;  │
│          Alice doesn't need raw bash forge tools; forge operations stay      │
│          sandboxed, authenticated, and audited.                             │
│  • Cons: Adds MCP tools to Alice and worker toolsets.                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Recommendation: **Hybrid Strategy (Strategy B for Hub + Strategy A/C for Agents)**
1. **Hub Merge Gate**: Use **direct REST API calls via `httpx`** (or an abstracted runner that accepts either REST or CLI). The hub is a Python service running `httpx`; relying on `glab` CLI on the host adds unnecessary installation friction, whereas an HTTP client directly against GitLab API v4 is reliable, fast, and dependency-free.
2. **Workers**: Allow workers to use `glab` CLI if present, or provide lightweight worker MCP helper tools (e.g., `create_change_request`, `view_change_head`) so workers do not require forge CLI authentication in their clones.

---

## 6. Impact Analysis by Component

### 6.1 `packages/common` (`agent_hub_common`)
- **Models (`models.py`)**:
  - `ImplementerResult`, `ReviewerResult`, and `RebaseResult` have `pr_url: str | None`. The validator only asserts that `pr_url` is a non-empty string when completed. **It does not enforce `github.com` or `/pull/`**.
  - Recommendation: Retain `pr_url` and `pr_head_sha` as the wire/database names to preserve **schema compatibility (`hub.schema_version=1`)**, while documenting them as representing Change Request / Merge Request URLs and heads. Alternatively, add field aliases (`change_url`, `mr_url`).
- **Constants (`constants.py`)**:
  - `MetaKeys.PR_HEAD_SHA = "hub.pr_head_sha"` remains unchanged for backward compatibility.

### 6.2 `packages/hub` (`agent_hub`)
- **`merge_gate.py` (Major Refactoring)**:
  - Extract the forge-specific logic behind an interface:
    ```python
    class ForgeMergeGate(Protocol):
        async def check(self, change_url: str, expected_head_sha: str) -> GateReport: ...
    ```
  - Implement `GitHubMergeGate` (existing logic) and `GitLabMergeGate`.
  - Create a dispatcher that routes by URL pattern (`/pull/` vs `/-/merge_requests/` or hostname).
  - Expand URL parsing from `PR_URL_RE` to support nested GitLab namespaces:
    ```python
    # Matches https://<host>/<group>/[<subgroup>/...]/<project>/-/merge_requests/<number>
    GITLAB_MR_URL_RE = re.compile(
        r"https://(?P<host>[A-Za-z0-9.-]+)/(?P<path>.+)/-/merge_requests/(?P<number>[1-9][0-9]*)/?"
    )
    ```
- **Database & Store (`database.py`, `store.py`)**:
  - **Zero impact**. SQLite schema stores strings (`pr_head_sha TEXT`).

### 6.3 `packages/worker_mcp` (`worker_mcp`)
- **Zero impact**. `worker_mcp` is an A2A JSON-RPC client. It transports results from worker LLM to hub without parsing or inspecting forge URLs.

### 6.4 Role Guides (`guides/*.md`)
- **Impact**: Role guides currently instruct workers on specific `gh` CLI commands:
  - `implementer.md`: `gh pr create`, `gh pr view --json headRefOid`.
  - `reviewer.md`: `gh pr comment`.
  - `rebase.md`: `gh pr view`.
- **Solution**:
  - Option 1 (Dynamic Guides): The hub route `GET /guides/{role}.md` can accept a query parameter or use `HubSettings.forge` (`github` or `gitlab`), dynamically serving `glab` instructions for GitLab workflows.
  - Option 2 (Dual-Forge Guides): Provide both `gh` and `glab` syntax side-by-side in the served guides.

### 6.5 Alice Skill & Prompts (`skills/alice-orchestrator/SKILL.md`, `prompts/alice.md`)
- **Impact**: Alice executes `gh issue view` and `gh pr merge --match-head-commit`.
- **Solution**:
  - Update skill instructions to specify `glab mr merge <mr_url> --sha <approved_head> --squash --remove-source-branch` when working on GitLab.
  - Or provide a hub MCP tool `merge_change(pr_url, approved_head)` so Alice delegates merge execution to the hub, ensuring uniform behavior regardless of forge.

### 6.6 Run Scripts (`scripts/prepare-run.py`, `scripts/run_common.py`)
- **Impact**:
  - `parse_github_slug` assumes 2 path components (`owner/repo`). GitLab paths can be `group/subgroup/project`.
  - `check_gh_auth`, `repo_settings`, and `repo_has_workflows` make GitHub CLI and API calls.
- **Solution**:
  - Introduce `parse_repo_slug` capable of handling both GitHub and GitLab URLs/paths.
  - Add `prepare-run.py` flags `--forge [github|gitlab]` and corresponding GitLab preflight checks (`glab auth status` or `GITLAB_TOKEN` API check).

---

## 7. GitLab Self-Hosted Nuances & Considerations

Unlike GitHub, where the vast majority of users rely on `github.com`, GitLab is heavily deployed in private enterprise environments. Supporting GitLab requires handling:

1. **Custom Hostnames & Ports**:
   - URLs are not restricted to `gitlab.com` (e.g., `https://gitlab.corp.internal:8443`).
   - Host detection must rely on URL patterns (e.g., the `/-/` segment) or explicit configuration (`HUB_FORGE=gitlab`).
2. **Corporate TLS & Custom Root Certificates**:
   - Python HTTP clients (`httpx`) and git commands must respect `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `GIT_SSL_CAINFO`.
3. **Authentication & Token Scopes**:
   - GitLab offers multiple token types: **Personal Access Tokens**, **Project Access Tokens**, and **Group Access Tokens**.
   - Required scopes: `api` (or `read_api`, `read_repository`, `write_repository`).
4. **GitLab Version Discrepancies**:
   - Enterprise deployments may lag behind latest GitLab SaaS.
   - For example, `detailed_merge_status` was finalized in GitLab 15.6. Fallbacks to `merge_status` and `has_conflicts` should be retained for older enterprise installations.

---

## 8. Implementation Roadmap

```
Phase 1: URL & Model Generalization
  ├── Generalize URL regex in common/hub (support multi-segment paths and /-/merge_requests/)
  ├── Update scripts/run_common.py to parse nested slugs (group/subgroup/project)
  └── Add unit tests for GitLab URL parsing and validation

Phase 2: GitLab Merge Gate Implementation
  ├── Define ForgeAdapter protocol in agent_hub.merge_gate
  ├── Implement GitLabMergeGate using httpx / GitLab REST API v4
  ├── Add pipeline status, diverged commit, conflict, and SHA-bound verification
  └── Add comprehensive unit test suite with mock GitLab API responses (mirroring test_merge_gate.py)

Phase 3: Guide & Prompt Generalization
  ├── Update hub GET /guides/{role}.md to serve forge-appropriate commands (gh vs glab)
  ├── Update prompts/alice.md and alice-orchestrator skill to support GitLab MR merge syntax
  └── Test mock worker / mock Alice workflows with GitLab payloads

Phase 4: Run Preparation & Tooling
  ├── Add GitLab preflight validation to scripts/prepare-run.py
  ├── Support glab auth status and GitLab token verification
  └── Verify workspace clone bootstrapping from GitLab remotes

Phase 5: End-to-End Validation
  ├── Test full PLAN ➔ IMPLEMENT ➔ REVIEW ➔ REBASE ➔ MERGE lifecycle against a real GitLab repo
  └── Document setup in docs/user-guide.md
```

---

## 9. Conclusion

Adding GitLab support to `robo-agents` is **highly feasible and architecturally straightforward**. The core of robo-agents was designed around clean abstractions: pull-based A2A agent coordination, durable SQLite state, and strict invariant gates. Because GitLab natively supports head-SHA-bound merges (`sha` parameter on merge), pipeline status checks, divergence counts, and asynchronous mergeability computation, robo-agents can deliver the exact same safety guarantees on GitLab as it currently does on GitHub without altering the core protocol or state engine.
