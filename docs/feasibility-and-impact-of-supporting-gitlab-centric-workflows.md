# Feasibility and Impact of Supporting GitLab-Centric Workflows

## 1. Executive Summary & Verdict

This document assesses the feasibility and architectural impact of expanding **robo-agents** from its current GitHub-centric proof-of-concept (PoC) into supporting **GitLab**-centric development workflows (issues, merge requests, CI/CD pipelines, discussions, and automated merge gating).

### Verdict: **High Feasibility; Equivalent Safety is Achievable**
- **Feasibility is High**: The foundational coordination mechanics of robo-agents—the **hub-centric pull model**, A2A JSON-RPC transport, SQLite durable event ledger, worker heartbeats, task leasing, and prompt-injection defense—are **completely forge-agnostic**.
- **Equivalent Safety is Achievable**: GitLab provides the technical primitives necessary to satisfy every core safety rail in [`docs/poc-spec.md`](poc-spec.md) §5 (including head-SHA-bound merges, stale-base detection, and CI verification). However, achieving true invariant parity requires explicit accommodation of GitLab-specific behaviors rather than assuming a 1:1 mapping:
  1. Managing `glab` CLI's unsafe default auto-merge behavior and known version bugs.
  2. Supporting GitLab's diverse pipeline architectures (source-branch and merged-results pipelines), while explicitly declaring merge trains unsupported in initial phases.
  3. Formulating a queryable, fail-closed operational definition of `NO_CHECKS` versus `NO_WORKFLOWS` given GitLab's remote and compliance CI capabilities.
  4. Handling the full 24-state `detailed_merge_status` machine (including approval rules, unresolved discussions, external checks, and transient polling states) rather than simple conflict checks.
  5. Binding forge credentials securely to allowlisted hostnames for self-hosted instances.

---

## 2. Current Forge Coupling Footprint

In the current PoC, GitHub integration is embedded across code, configuration, scripts, guides, strings, and the specification itself:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Current robo-agents Architecture & Forge Touchpoints                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  [Specification & Locked Decisions]                                         │
│    • docs/poc-spec.md: §4.2, §8 (Merge/Review authority, CI handling, etc.)  │
│                                                                             │
│  [Alice (Orchestrator)]                                                     │
│    • skills/alice-orchestrator/SKILL.md: gh issue view, gh pr merge         │
│    • prompts/alice.md & skills/alice-relay/SKILL.md                         │
│                                                                             │
│  [Hub Service (agent_hub)]                                                  │
│    • MCP Tool: check_merge_gate                                             │
│    • packages/hub/src/agent_hub/merge_gate.py: gh CLI, PR_URL_RE             │
│    • store.py: DEFAULT_GOAL string ("Drive the assigned GitHub issue to a   │
│      merged pull request.")                                                 │
│    • protocol.py & store.py: remediation string ("Store work product in     │
│      GitHub")                                                               │
│                                                                             │
│  [Worker MCP (worker_mcp) & Guides]                                         │
│    • guides/implementer.md: gh pr create, gh pr view --json headRefOid      │
│    • guides/reviewer.md: gh pr comment                                      │
│    • guides/rebase.md: gh pr view --json headRefOid                         │
│    • guides/worker.md & prompts/worker.md ("GitHub is the work-product      │
│      store")                                                                │
│                                                                             │
│  [Data Models & Protocol (agent_hub_common)]                                │
│    • models.py: pr_url, head_sha, reviewed_head_sha                         │
│    • constants.py: MetaKeys.PR_HEAD_SHA = "hub.pr_head_sha"                 │
│    • database.py: task.pr_head_sha column                                   │
│                                                                             │
│  [Run Lifecycle & Tooling Scripts]                                          │
│    • scripts/prepare-run.py: gh auth status, gh repo view, gh api workflows │
│    • scripts/run_common.py: parse_github_slug, gh config git_protocol       │
│    • scripts/step6.py: hard-coded github.com URLs & .github workflows       │
│    • scripts/hub-report.py: internal PR_URL_RE parser                       │
│    • scripts/mock-alice.py & mock-worker.py                                 │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Coupling Points:
1. **`merge_gate.py`**:
   - Strictly matches GitHub pull request URLs via `PR_URL_RE` (`https://<host>/<owner>/<repo>/pull/<number>`).
   - Invokes `gh pr view`, `gh pr checks`, and `gh api repos/{owner}/{repo}/compare/...`.
   - Distinguishes absent checks from missing workflows via `gh api repos/{owner}/{repo}/actions/workflows`.
2. **`alice-orchestrator` Skill & Prompts**:
   - Instructs Alice to read issues via `gh issue view`.
   - Enforces SHA-bound merge via `gh pr merge <pr_url> --<method> --delete-branch --match-head-commit <head_sha>`.
3. **Role Guides (`guides/implementer.md`, `guides/reviewer.md`, `guides/rebase.md`, `guides/worker.md`)**:
   - Instructs implementers to create PRs via `gh pr create` and verify heads via `gh pr view --json headRefOid`.
   - Instructs reviewers to post review comments via `gh pr comment`.
   - Instructs rebase agents to verify the updated head via `gh pr view`.
   - Tells workers that "GitHub is the work-product store".
4. **Data Models & Database Schema**:
   - Wire metadata keys: `hub.pr_head_sha`.
   - SQLite table schema: `task.pr_head_sha`.
   - Result models: `ImplementerResult.pr_url`, `ReviewerResult.pr_url`, `RebaseResult.pr_url`.
5. **Specification (`docs/poc-spec.md`)**:
   - §4.2, §5, and §8 locked decisions (*Merge authority*, *Review authority*, *CI check handling*, *Stale base*) are explicitly framed around `gh` CLI semantics and GitHub review models.
6. **User-Facing Strings**:
   - `store.py:54`: `DEFAULT_GOAL = "Drive the assigned GitHub issue to a merged pull request."`.
   - `protocol.py:242,606` and `store.py:1191,1258,1416`: Payload-cap error messages tell agents to "Store work product in GitHub".
7. **Run Tooling & Automation**:
   - `scripts/prepare-run.py`, `scripts/run_common.py`, `scripts/step6.py`, `scripts/hub-report.py`, `scripts/mock-alice.py`, and `scripts/mock-worker.py` make explicit assumptions about 2-part `owner/repo` slugs, GitHub Actions paths, and `gh` authentication.

---

## 3. Paradigm & Feature Mapping: GitHub vs. GitLab

| Workflow Dimension | GitHub Paradigm | GitLab Paradigm | robo-agents Architectural Mapping |
|---|---|---|---|
| **Namespace & Hierarchy** | 2-level: `owner/repo` | Multi-level: `group/subgroup1/.../project` or `user/project` | GitLab projects can have arbitrary nesting. Slugs cannot assume `len(parts) == 2`. Project paths must be URL-encoded (`group%2Fsubgroup%2Fproject`) as `:id` for API calls. |
| **Change Request** | Pull Request (PR) | Merge Request (MR) | Conceptually equivalent. URLs use `/-/merge_requests/<iid>` instead of `/pull/<number>`. |
| **Issue Tracking** | Issue `#123` | Issue `#123` (identified by project-scoped `iid`) | Identical semantics. Closing keywords (`Closes #N`, `Fixes #N`) supported natively by both. |
| **Review & Comments** | PR Comments & Formal Reviews (`gh pr comment`, `gh pr review`) | Notes & Discussions (`glab mr note`, `POST /notes`) | Both support markdown comments and threads. Under a shared PoC account, both use comment-based approval. |
| **Merge Head Binding** | `gh pr merge --match-head-commit <sha>` | REST API `sha` param or `glab mr merge --sha <sha>` | Equivalent safety primitive: GitLab API `PUT /merge` rejects merges with HTTP 409 if `sha` does not match the MR HEAD. |
| **CI / Checks** | GitHub Actions Workflows & Check Runs | GitLab CI/CD Pipelines & External Status Checks | Diverse pipeline types (source, merged-results, merge trains). Requires conservative mapping of pipeline states. |
| **Stale Base Detection** | GitHub Compare API (`behind_by > 0`) | MR `diverged_commits_count > 0` or Compare API | MR API requires `?include_diverged_commits_count=true`. Compare fallback requires `from=<source>&to=<target_tip>` to count target-ahead commits. |
| **Mergeability** | `mergeable` (`MERGEABLE` / `CONFLICTING`) | `has_conflicts` & `detailed_merge_status` | GitLab mergeability covers approvals, unresolved threads, external checks, and security policies (24 statuses). |
| **Discussion Gating** | Branch protection setting | Native MR status (`discussions_not_resolved`) | GitLab natively reports whether unresolved discussions block the merge. |
| **Deployment Model** | Primarily SaaS (`github.com`) | Common SaaS (`gitlab.com`) + Ubiquitous Self-Hosted (CE/EE) | Requires configurable hostnames, custom ports, corporate TLS/CA bundles, and host-bound token allowlisting. |

### Forge Selection: Per-Workflow Dynamic Resolution
Rather than introducing a static, hub-wide configuration (`HUB_FORGE=gitlab`), the target forge should be **resolved dynamically per workflow** from the target repository/issue URL in the statement of work:
- During `initialize_workflow(goal, policy)`, the hub detects the forge from the goal URL (matching `github.com` vs configured `HUB_GITLAB_HOSTS`).
- The resolved forge is durably recorded in `workflow.policy_json` (e.g. `policy["forge"] = "gitlab"`).
- Guide serving (`GET /guides/{role}.md`) reads the active workflow's forge directly from SQLite, serving the appropriate instructions to workers without requiring any worker-supplied query parameter.
- The `--forge` flag in `scripts/prepare-run.py` acts as an explicit override or provides the forge type for bare local directories.

---

## 4. Deep-Dive: Workflow Invariants & GitLab Nuances (§5 Rails)

### 4.1 SHA-Bound Merge Invariant
- **Requirement**: Merging must be bound to the exact commit SHA approved by the reviewer. If a commit lands after approval, the merge must fail and route to re-review.
- **GitLab Support**:
  - GitLab REST API: `PUT /projects/:id/merge_requests/:mr_iid/merge` accepts parameter `sha=<sha>`. If the current MR HEAD does not equal `sha`, GitLab rejects the request with HTTP `409 Conflict` (`"SHA does not match HEAD of source branch"`).
- **The Critical `glab` CLI Hazard**:
  - In `glab mr merge`, **auto-merge defaults to `true` when a pipeline is running**. Running `glab mr merge <id> --sha <sha>` on an active pipeline will silently schedule auto-merge rather than performing an immediate merge or failing!
  - Furthermore, `glab` v1.66.0 suffers from an upstream issue ([glab#8485](https://gitlab.com/gitlab-org/cli/-/issues/8485)) where it can report "Merged!" while the MR is merely queued for auto-merge.
  - **Remedy**: Alice or the hub must **never rely on default `glab mr merge` behavior**. The workflow must either:
    1. Perform merges via the REST API with `sha=<sha>`, omitting `auto_merge` (or explicitly passing `auto_merge=false`), and verifying that returned `state == "merged"`. Note that the older parameter `merge_when_pipeline_succeeds` was deprecated in GitLab 17.11.
    2. Pass `--auto-merge=false` explicitly in `glab` and immediately re-verify that the MR state is `merged`.

### 4.2 CI Gate Invariant & Pipeline Status Mapping
In GitHub, `classify_checks()` reduces check run buckets into `CiStatus` (`pass`, `fail`, `pending`, `cancelled`, `no_checks`, `no_workflows`).
In GitLab, pipeline status does not map 1:1 without careful policy distinctions:

| GitLab Pipeline Status | Mapped `CiStatus` | Polled? | Rationale & Handling |
|---|---|---|---|
| `success` | `CiStatus.PASS` | No | Pipeline completed successfully. |
| `failed` | `CiStatus.FAIL` | No | Pipeline failed; routes to implementer CI repair. |
| `running`, `pending`, `preparing`, `waiting_for_resource`, `waiting_for_callback`, `created`, `scheduled` | `CiStatus.PENDING` | Yes | Active or queued states; gate continues bounded polling (up to 60 s). |
| `canceling`, `canceled` | `CiStatus.CANCELLED` | Yes | Cancelled run; polls like absent checks to allow a superseding run to appear (matching spec §8). |
| `manual` | Blocked / Escalated | No | **Must not be treated as PASS or PENDING**. Represents blocked jobs waiting for manual trigger. Does not resolve by waiting; gate returns non-polled blocked status to escalate. |
| `skipped` | Policy-Sensitive | No | Governed by project setting `allow_merge_on_skipped_pipeline`. If repository policy explicitly permits it, maps to `PASS`; otherwise fails closed / escalates. |
| *Unknown / Future Status* | Error / Fail Closed | No | Never assume unrecognised pipeline states are successful. |

#### Operational Definition: `NO_CHECKS` vs. `NO_WORKFLOWS`
The `allow_no_ci` escape hatch requires cleanly distinguishing whether CI is absent by design or simply hasn't started yet:
- In GitHub: Checked via `gh api repos/{owner}/{repo}/actions/workflows`.
- In GitLab:
  - Checking solely for `.gitlab-ci.yml` in the tree is **insufficient**: projects can configure custom CI paths (`ci_config_path` pointing to another project or remote URL), and compliance frameworks or pipeline execution policies can enforce pipelines without any in-repo configuration file.
  - **Operational Definition**:
    - **`NO_WORKFLOWS`**: Emitted **only from affirmative, queryable evidence** that no applicable CI check source exists:
      1. Default `.gitlab-ci.yml` does not exist at the approved head commit.
      2. No local `ci_config_path` exists at that commit, and no remote/other-project `ci_config_path` is set in project settings.
      3. Auto DevOps is disabled on the project.
      4. No external CI integrations (e.g. Jenkins, external status checks) are active.
      Under `allow_no_ci: true`, the gate allows merge on review approval alone. Under `allow_no_ci: false`, it escalates immediately.
    - **`NO_CHECKS`**: CI configuration is present or ambiguous (e.g. group-level pipeline execution policies that cannot be queried with a project token). The gate polls boundedly (default 60 s). If no pipeline appears within the timeout, it escalates.

### 4.3 Pipeline Architectures: Source, Merged-Results, and Merge Trains
GitLab supports three distinct pipeline ref conventions:
1. **Source / Detached MR Pipelines** (`refs/merge-requests/<iid>/head`): Run directly on the MR source branch HEAD. Here, `head_pipeline.sha == expected_head_sha` holds true.
2. **Merged-Results Pipelines** (`refs/merge-requests/<iid>/merge`): GitLab creates an internal merge commit combining the source branch and the target branch, and executes CI on that synthetic commit. `head_pipeline.sha` will be the temporary merge commit SHA, **not** the approved source HEAD.
   - *Adapter Strategy*: The adapter detects `refs/merge-requests/<iid>/merge` and verifies via git ancestry or the commit API that `expected_head_sha` is one of the parents of the tested merge commit.
3. **Merge Train Pipelines** (`refs/merge-requests/<iid>/train`): Run on internal queued merge commits.
   - *Architectural Incompatibility*: Merge trains are fundamentally asynchronous: adding an MR to a merge train queues the merge rather than merging immediately. Furthermore, GitLab 19.3's `merge_train_enforcement` rejects direct REST merges, and GitLab 19.2+'s `automatic_rebase_enabled` creates server-side rebased commits at merge time without CI verification on the approved head.
   - **Recommendation**: In the initial GitLab support phase, **merge trains, merge train enforcement, and automatic rebase are declared unsupported**. Preflight checks must detect these project settings and reject the run with an actionable error.

### 4.4 Stale Base & Rebase Invariant
- **Requirement**: An approved MR whose base branch has advanced must not merge directly; it must be rebased.
- **GitLab Specifics**:
  - The single MR endpoint (`GET /projects/:id/merge_requests/:iid`) returns `diverged_commits_count` **only when the query parameter `?include_diverged_commits_count=true` is passed**. Omitting this parameter results in `None`/missing data.
  - **Compare Fallback**: When using the compare API (`GET /projects/:id/repository/compare`), GitLab uses default three-dot (`from...to`) semantics returning commits in `to` after the merge base.
    - To detect whether the base has moved ahead of the source, the fallback must call:
      `GET /projects/:id/repository/compare?from=<source_head>&to=<current_target_tip>`
      The length of the returned `commits` array indicates the commits the target branch has that the source lacks. (Calling `from=<target>&to=<source>` returns the source's ahead commits).
  - **Authoritative `main_sha`**: To supply `main_sha` (the target branch tip), the adapter must query `GET /projects/:id/repository/branches/:target_branch`. `diff_refs.start_sha` reflects the target tip when the diff was generated, which may be stale. The merge base `base_sha` comes from `diff_refs.base_sha`.

### 4.5 Mergeability & Full `detailed_merge_status` Mapping
In GitHub, mergeability primarily checks for textual conflicts (`MERGEABLE` vs `CONFLICTING`).
In GitLab, `detailed_merge_status` evaluates 24 authoritative states:

| `detailed_merge_status` | Gate Classification | Polled? | Action / Routing |
|---|---|---|---|
| `mergeable` | `Mergeable.CLEAN` | No | Gate satisfied; eligible for merge. |
| `conflict` | `Mergeable.CONFLICTING` | No | Textual conflicts; routes to `rebase` task. |
| `need_rebase` | Stale Base | No | Base moved under fast-forward policy; routes to `rebase` task. |
| `checking`, `unchecked`, `approvals_syncing`, `preparing`, `ci_still_running` | `Mergeable.UNKNOWN` | Yes | Asynchronous computation in progress; gate continues polling. |
| `ci_must_pass` | CI Verification | Yes | Pipeline required; inspect underlying pipeline status. |
| `status_checks_must_pass`, `security_policy_pipeline_check` | External Check | Yes | External or security checks running; inspect check results. |
| `discussions_not_resolved` | Policy Blocker | No | Unresolved threads; routes to implementer `address` task or resolution. |
| `not_approved`, `requested_changes` | Approval Blocker | No | Native approval required. Under shared-account PoC, comment approval is used, so project must not enforce native approvals unless agents have distinct approval tokens. |
| `draft_status` | State Blocker | No | MR marked draft; escalate to Alice/operator. |
| `merge_request_blocked` | Dependency Blocker | No | Blocked by dependencies; escalate to Alice/operator. |
| `jira_association_missing` | Integration Blocker | No | Missing Jira issue; escalate to Alice/operator. |
| `locked_paths`, `locked_lfs_files` | File Lock Blocker | No | File locks prevent merge; escalate to operator. |
| `title_regex` | Rule Blocker | No | MR title fails project regex; escalate to Alice to rename MR. |
| `merge_time` | Timing Blocker | No | Merge outside permitted time window; wait or escalate. |
| `not_open` | State Blocker | No | MR is merged or closed; check recovered state. |
| `security_policy_violations`, `commits_status` | Policy Blocker | No | Compliance violation or commit rule failure; escalate. |
| *Unknown Future Status* | Fail Closed | No | Unknown blocker; log and escalate to operator. |

*Legacy Fallback*: For older self-hosted instances (prior to GitLab 15.6 when `detailed_merge_status` was introduced), the adapter falls back to `merge_status` (`can_be_merged`, `cannot_be_merged`, `checking`) combined with `has_conflicts`.

---

## 5. Architectural Evaluation: Integration Strategies

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Comparison of Integration Strategies for GitLab                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ Strategy A: CLI-Based (`glab`)                                              │
│  • Pros: Preserves Alice's shell-based merge pattern (spec §8).             │
│  • Cons: `glab` is rarely installed by default; version discrepancies       │
│          (e.g., v1.66.0 auto-merge bug); requires `api` tokens in agent     │
│          runtimes.                                                          │
│                                                                             │
│ Strategy B: Direct REST API (Hub HTTP Client)                               │
│  • Pros: Zero host CLI dependencies; deterministic JSON payloads; handles   │
│          self-hosted TLS and ports cleanly; easy to unit-test with mock     │
│          transports.                                                        │
│  • Cons: Requires adding `httpx` as a direct runtime dependency in          │
│          packages/hub/pyproject.toml; requires hub-managed tokens.          │
│                                                                             │
│ Strategy C: Forge MCP Tools (Decoupled Engine)                              │
│  • Pros: Workers require no forge CLI or API tokens in their workspace      │
│          clones; Alice doesn't execute raw bash merge commands; all forge   │
│          interactions are mediated and audited through the hub.             │
│  • Cons: Amends spec §8 decision ("No merge tool") to add a hub merge tool. │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Recommendation: **Hybrid Strategy with Explicit Roadmap Phase**
1. **Hub Merge Gate**: Implement via **direct REST API calls using `httpx.AsyncClient`**. Adding `httpx` directly to `packages/hub` provides a reliable, cross-platform client that avoids host CLI issues.
2. **Workers**: Workers need git remote write credentials (via SSH keys or HTTPS `write_repository` token) to push branches. For change creation and head verification, workers can either use `glab` if available, or call lightweight hub-provided MCP tools (`create_change_request`, `view_change_head`).
3. **Alice Merge Execution**:
   - *Phase 1 Baseline (Honoring Spec §8)*: Alice merges via shell using `glab mr merge <mr_url> --sha <approved_head> --auto-merge=false --squash --remove-source-branch`.
   - *Proposed Product Evolution (Amending Spec §8)*: Propose amending the locked decision to introduce a hub MCP tool `merge_change_request(url, approved_head, method)`. Mediating merges through the hub eliminates CLI dependency hazards and unsafe auto-merge defaults.

---

## 6. Component-by-Component Impact Analysis

### 6.1 `packages/common` (`agent_hub_common`)
- **Result Models (`models.py`)**:
  - `ImplementerResult.pr_url`, `ReviewerResult.pr_url`, and `RebaseResult.pr_url` require non-empty strings, but enforce no specific domain or `/pull/` path. They work transparently with GitLab MR URLs (`/-/merge_requests/<iid>`).
  - To preserve wire compatibility (`SCHEMA_VERSION = 1`) and database consistency, keep `pr_url` and `pr_head_sha` as the canonical wire keys, documenting them as representing Change Request URLs and heads.
- **Metadata Constants (`constants.py`)**:
  - `MetaKeys.PR_HEAD_SHA` remains unchanged.

### 6.2 `packages/hub` (`agent_hub`)
- **Dependencies (`pyproject.toml`)**:
  - Add `httpx` as a direct runtime dependency in `packages/hub/pyproject.toml`.
- **`merge_gate.py` Refactoring**:
  - Extract a `ForgeMergeGate` protocol:
    ```python
    class ForgeMergeGate(Protocol):
        async def check(self, change_url: str, expected_head_sha: str) -> GateReport: ...
    ```
  - Implement `GitHubMergeGate` (preserving existing `gh` logic) and `GitLabMergeGate` (using `httpx` against GitLab API v4).
  - Implement a hardened, non-backtracking, traversal-safe URL regex:
    ```python
    SEG = r"(?!\.\.?/)[A-Za-z0-9_.][A-Za-z0-9_.-]*"
    GITLAB_MR_URL_RE = re.compile(
        r"https://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?)"
        rf"/(?P<project>{SEG}(?:/{SEG})*)"
        r"/-/merge_requests/(?P<number>[1-9][0-9]*)/?"
    )
    ```
  - URL-encode the extracted project path (`quote(project, safe="")`) when calling the API:
    `url = f"https://{host}/api/v4/projects/{quote(project, safe='')}/merge_requests/{number}"`
  - *Subfolder Installations*: For enterprise installations at subpaths (e.g. `https://example.com/gitlab`), parsing uses `urlsplit` matched against configured trusted base URLs (`HUB_GITLAB_BASE_URLS`), extracting the project path relative to that base.
- **Strings (`store.py`, `protocol.py`)**:
  - Generalize `DEFAULT_GOAL` and payload-cap error messages from "Store work product in GitHub" to "Store work product in the forge (GitHub/GitLab)".

### 6.3 `packages/worker_mcp` (`worker_mcp`)
- If workers use CLI (`glab`), `worker_mcp` has **zero impact**.
- If Strategy C is adopted, `worker_mcp` will expose new helper tools (`create_change_request`, `view_change_head`).

### 6.4 Role Guides (`guides/*.md`)
- Dynamically serve role guides via `GET /guides/{role}.md` reading the active workflow's forge directly from SQLite.
- Document explicit `glab` invocation flags:
  - Implementer: `glab mr create --title ... --description ...`
  - Reviewer: `glab mr note -m "Reviewer agent <name> on behalf of <account>..."`
  - Rebase: `glab mr view --output json`

### 6.5 Alice Orchestrator Skill & Prompts
- Update `skills/alice-orchestrator/SKILL.md` to specify GitLab merge commands:
  `glab mr merge <mr_url> --sha <approved_head> --auto-merge=false --squash --remove-source-branch`
- Update `prompts/alice.md` comment identity account placeholders to support GitLab usernames.

### 6.6 Run Scripts & Automation
- `scripts/run_common.py`: Update `parse_github_slug` to a generalized `parse_forge_slug` supporting nested paths.
- `scripts/prepare-run.py`: Add `--forge [github|gitlab]` flag and implement GitLab preflight checks (token validity, project permissions, CI config discovery, and rejecting merge trains / automatic rebase).
- `scripts/step6.py`, `scripts/hub-report.py`, `mock-alice.py`, `mock-worker.py`: Parameterize repository URLs and workflow paths for multi-forge testing.

---

## 7. Infrastructure, Credentials & Security

Self-hosted GitLab instances (GitLab CE/EE) are common in private enterprise environments. Supporting them requires strict security controls:

### 7.1 Token Scopes & Principle of Least Privilege
GitLab distinguishes API access from Git repository access:
- **`api` scope**: Grants full read/write API access. Required by the hub to inspect merge requests, query pipelines, and execute merges. Also required by Alice/workers if invoking `glab` CLI commands.
- **`write_repository` scope**: Grants read/write access via Git-over-HTTP. **Explicitly does not authenticate API requests**.
- **Worker Credentials**: Under Strategy C (hub MCP tools), workers only need `write_repository` (or SSH keys) to push branches and hold zero forge API tokens. Under Strategy A (CLI), workers require their own `api`-scoped tokens.
- **Hub Credentials**: The hub's token must be loaded via `HubSettings.from_env()`, mapped per trusted host (`HUB_GITLAB_TOKEN` or `HUB_GITLAB_TOKEN_<HOST>`), never passed through CLI arguments, and scrubbed from all audit and call logs.

### 7.2 Host-Bound Token Allowlisting & SSRF Prevention
When the hub receives a merge request URL, it must **never send its bearer token to an arbitrary host parsed from untrusted text**:
- Configure `HUB_GITLAB_HOSTS` as an allowlist of trusted domains and ports (defaulting to `gitlab.com`).
- The hub's HTTP client must verify that the MR URL's host matches an allowlisted trusted host before attaching the `PRIVATE-TOKEN` header.
- Disallow unvalidated HTTP redirects to external hosts.

### 7.3 Corporate TLS & Custom CA Bundles
Enterprise GitLab instances frequently use internal enterprise PKI. Environment variable handling must be distinct across tools:
- **HTTPX (Hub)**: Respects `SSL_CERT_FILE` and `SSL_CERT_DIR`. (Does not read `REQUESTS_CA_BUNDLE`).
- **`glab` CLI**: Configured via `GLAB_CA_CERT` or per-host `ca_cert` in `~/.config/glab-cli/config.yml`.
- **Git (Workers/Alice)**: Configured via `GIT_SSL_CAINFO` or `http.sslCAInfo`.

---

## 8. Implementation Roadmap

```
Phase 1: Spec Revision & Model Generalization
  ├── Propose amendments to docs/poc-spec.md §4.2, §8 (forge-neutral gate rules,
  │   merge tool consideration, and declaring merge trains unsupported)
  ├── Generalize URL parsing in scripts/run_common.py for nested paths (group/subgroup/project)
  ├── Generalize user-facing remediation strings in store.py and protocol.py
  └── Add unit tests for hardened GitLab URL regex, dot-segment rejection, and ReDoS resistance

Phase 2: GitLab Merge Gate Adapter in Hub
  ├── Add httpx runtime dependency to packages/hub/pyproject.toml
  ├── Define ForgeMergeGate protocol in agent_hub.merge_gate
  ├── Implement GitLabMergeGate using httpx against GitLab API v4
  ├── Implement pipeline status mapping, merged-results handling, and ?include_diverged_commits_count=true
  ├── Implement 24-status detailed_merge_status mapping with legacy merge_status fallback
  ├── Enforce host-bound token allowlisting and SSRF protection
  └── Write comprehensive unit tests in tests/test_gitlab_merge_gate.py with mock HTTP fixtures

Phase 3: Guide Serving & Orchestrator Skill
  ├── Update hub GET /guides/{role}.md route to serve forge-specific guidance based on workflow.policy_json
  ├── Update guides/implementer.md, reviewer.md, rebase.md with glab syntax
  ├── Update skills/alice-orchestrator/SKILL.md with safe merge command (--auto-merge=false)
  └── Test mock-alice and mock-worker workflows against simulated GitLab responses

Phase 4: Run Preparation & Tooling Updates
  ├── Add --forge flag to scripts/prepare-run.py
  ├── Implement GitLab preflight checks (token validity, project permissions, rejecting merge trains)
  └── Update scripts/hub-report.py to handle GitLab MR references cleanly

Phase 5: Validation & End-to-End Testing
  ├── Validate against a real repository on GitLab.com
  ├── Validate against a local dockerized GitLab CE container (verifying self-hosted TLS and ports)
  └── Update docs/user-guide.md with GitLab setup instructions
```

---

## 9. Conclusion & Recommendation

Supporting GitLab workflows in `robo-agents` is **highly feasible and architecturally clean**. The core pull-coordination architecture, task state machine, and durable leasing engine require zero modifications. By implementing a dedicated `GitLabMergeGate` adapter over HTTPX, handling GitLab's pipeline and auto-merge nuances with care, rejecting incompatible merge train configurations in preflight, and scoping credentials properly, robo-agents can achieve equivalent safety on GitLab without sacrificing the rigor of its workflow invariants.
