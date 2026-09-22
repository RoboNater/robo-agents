# Feasibility and Impact of Supporting GitLab-Centric Workflows

## 1. Executive Summary & Verdict

This document assesses the feasibility and architectural impact of expanding **robo-agents** from its current GitHub-centric proof-of-concept (PoC) into supporting **GitLab**-centric development workflows (issues, merge requests, CI/CD pipelines, discussions, and automated merge gating).

### Verdict: **High Feasibility; Equivalent Safety is Achievable**
- **Feasibility is High**: The foundational coordination mechanics of robo-agents—the **hub-centric pull model**, A2A JSON-RPC transport, SQLite durable event ledger, worker heartbeats, task leasing, and prompt-injection defense—are **completely forge-agnostic**.
- **Equivalent Safety is Achievable**: GitLab provides the technical primitives necessary to satisfy every core safety rail in [`docs/poc-spec.md`](poc-spec.md) §5 (including head-SHA-bound merges, stale-base detection, and CI verification). However, achieving true invariant parity requires explicit accommodation of GitLab-specific behaviors rather than assuming a 1:1 mapping:
  1. Managing `glab` CLI's unsafe default auto-merge behavior and known version bugs.
  2. Supporting GitLab's diverse pipeline architectures (source-branch, merged-results, and merge-train pipelines).
  3. Formulating an operational definition of `NO_CHECKS` versus `NO_WORKFLOWS` given GitLab's remote/compliance CI capabilities.
  4. Handling multi-faceted mergeability blockers (`detailed_merge_status`) beyond simple textual conflicts.
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
│    • store.py: DEFAULT_GOAL string ("Drive the assigned GitHub issue...")   │
│    • protocol.py & store.py: remediation string ("Store work product in     │
│      GitHub")                                                               │
│                                                                             │
│  [Worker MCP (worker_mcp) & Guides]                                         │
│    • guides/implementer.md: gh pr create, gh pr view --json headRefOid      │
│    • guides/reviewer.md: gh pr comment                                      │
│    • guides/rebase.md: gh pr view --json headRefOid                         │
│    • guides/worker.md & prompts/worker.md                                   │
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
│    • scripts/mock-alice.py, mock-worker.py, hub-report.py                   │
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
4. **Data Models & Database Schema**:
   - Wire metadata keys: `hub.pr_head_sha`.
   - SQLite table schema: `task.pr_head_sha`.
   - Result models: `ImplementerResult.pr_url`, `ReviewerResult.pr_url`, `RebaseResult.pr_url`.
5. **Specification (`docs/poc-spec.md`)**:
   - §4.2, §5, and §8 locked decisions (*Merge authority*, *Review authority*, *CI check handling*, *Stale base*) are explicitly framed around `gh` CLI semantics and GitHub review models.
6. **User-Facing Strings**:
   - `store.py:54`: `DEFAULT_GOAL = "Drive the assigned GitHub issue to a reviewed, gate-checked merge..."`.
   - `protocol.py:242,606` and `store.py:1191,1258,1416`: Payload-cap error messages tell agents to "Store work product in GitHub".
7. **Run Tooling & Automation**:
   - `scripts/prepare-run.py`, `scripts/run_common.py`, `scripts/step6.py`, `scripts/mock-alice.py`, `scripts/mock-worker.py`, and `scripts/hub-report.py` make explicit assumptions about 2-part `owner/repo` slugs, GitHub Actions paths, and `gh` authentication.

---

## 3. Paradigm & Feature Mapping: GitHub vs. GitLab

| Workflow Dimension | GitHub Paradigm | GitLab Paradigm | robo-agents Architectural Mapping |
|---|---|---|---|
| **Namespace & Hierarchy** | 2-level: `owner/repo` | Multi-level: `group/subgroup1/.../project` or `user/project` | GitLab projects can have arbitrary nesting. Slugs cannot assume `len(parts) == 2`. Must URL-encode the path (`group%2Fsubgroup%2Fproject`) as `:id` for API calls. |
| **Change Request** | Pull Request (PR) | Merge Request (MR) | Conceptually equivalent. URLs use `/-/merge_requests/<iid>` instead of `/pull/<number>`. |
| **Issue Tracking** | Issue `#123` | Issue `#123` (identified by project-scoped `iid`) | Identical semantics. Closing keywords (`Closes #N`, `Fixes #N`) supported natively by both. |
| **Review & Comments** | PR Comments & Formal Reviews (`gh pr comment`, `gh pr review`) | Notes & Discussions (`glab mr note`, `POST /notes`) | Both support markdown comments and threads. Under a shared PoC account, both use comment-based approval. |
| **Merge Head Binding** | `gh pr merge --match-head-commit <sha>` | REST API `sha` param or `glab mr merge --sha <sha>` | Equivalent safety primitive: GitLab API `PUT /merge` rejects merges with HTTP 409 if `sha` does not match the MR HEAD. |
| **CI / Checks** | GitHub Actions Workflows & Check Runs | GitLab CI/CD Pipelines & External Status Checks | Diverse pipeline types (source, merged-results, merge trains). Requires conservative mapping of pipeline states. |
| **Stale Base Detection** | GitHub Compare API (`behind_by > 0`) | MR `diverged_commits_count > 0` or Compare API | MR API requires `?include_diverged_commits_count=true`. Compare API returns commit arrays rather than integer counts. |
| **Mergeability** | `mergeable` (`MERGEABLE` / `CONFLICTING`) | `has_conflicts` & `detailed_merge_status` | GitLab mergeability covers approvals, unresolved threads, external checks, and security policies. |
| **Discussion Gating** | Branch protection setting | Native MR status (`discussions_not_resolved`) | GitLab natively reports whether unresolved discussions block the merge. |
| **Deployment Model** | Primarily SaaS (`github.com`) | Common SaaS (`gitlab.com`) + Ubiquitous Self-Hosted (CE/EE) | Requires configurable hostnames, custom ports, corporate TLS/CA bundles, and host-bound token allowlisting. |

### Forge Selection: Per-Workflow vs. Per-Hub
Rather than introducing a static, hub-wide configuration (`HUB_FORGE=gitlab`), the target forge should be **resolved dynamically per workflow** from the target repository/issue URL in the statement of work. A single hub instance can then orchestrate GitHub and GitLab runs interchangeably without restarting or altering environment configuration.

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
    1. Perform merges via the REST API with `sha=<sha>` and `merge_when_pipeline_succeeds=false`.
    2. Pass `--auto-merge=false` explicitly in `glab` and immediately re-verify that the MR state is `merged`.

### 4.2 CI Gate Invariant & Pipeline Status Mapping
In GitHub, `classify_checks()` reduces check run buckets into `CiStatus` (`pass`, `fail`, `pending`, `cancelled`, `no_checks`, `no_workflows`).
In GitLab, pipeline status does not map 1:1 without careful policy distinctions:

| GitLab Pipeline Status | Mapped `CiStatus` | Rationale & Handling |
|---|---|---|
| `success` | `CiStatus.PASS` | Pipeline completed successfully. |
| `failed` | `CiStatus.FAIL` | Pipeline failed; triggers implementer CI repair. |
| `running`, `pending`, `preparing`, `waiting_for_resource`, `waiting_for_callback`, `created`, `scheduled` | `CiStatus.PENDING` | Active or queued states; gate continues bounded polling. |
| `canceling`, `canceled` | `CiStatus.CANCELLED` | Cancelled run; requires re-trigger or escalation. |
| `manual` | `CiStatus.PENDING` (or Fail Closed) | **Must not be treated as PASS**. Represents blocked jobs awaiting human or external trigger. |
| `skipped` | Policy-Sensitive (`PASS` or `PENDING`) | In GitLab, project setting `allow_merge_on_skipped_pipeline` governs mergeability. The gate should only treat as `PASS` if the repository policy explicitly permits it; otherwise treat as pending or escalate. |
| *Unknown / Future Status* | Error / Fail Closed | Never assume unrecognised pipeline states are successful. |

#### Operational Definition: `NO_CHECKS` vs. `NO_WORKFLOWS`
The `allow_no_ci` escape hatch requires cleanly distinguishing whether CI is absent by design or simply hasn't started yet:
- In GitHub: Checked via `gh api repos/{owner}/{repo}/actions/workflows`.
- In GitLab:
  - Checking solely for `.gitlab-ci.yml` in the tree is **insufficient**: projects can configure custom CI paths (`ci_config_path` pointing to another project or remote URL), and compliance frameworks or pipeline execution policies can enforce pipelines without any in-repo configuration file.
  - **Operational Definition**:
    - **`NO_WORKFLOWS`**: The project has CI disabled, has no `ci_config_path`, has no active pipeline execution policies, and has zero pipeline history. Under `allow_no_ci: true`, the gate allows merge on review approval alone. Under `allow_no_ci: false`, it escalates immediately.
    - **`NO_CHECKS`**: CI is configured/enabled on the project, but no pipeline has been created yet for the target commit (a transient race immediately following push). The gate polls boundedly (default 60 s).

### 4.3 Pipeline Architectures & Head Binding
Universal verification of `head_pipeline.sha == expected_head_sha` works for standard detached/source pipelines, but breaks under advanced GitLab CI configurations:
1. **Source / Detached Pipelines**: Run directly on the MR source branch HEAD commit. Here, `head_pipeline.sha == expected_head_sha` holds true.
2. **Merged-Results Pipelines**: GitLab automatically creates an internal merge commit (`refs/merge-requests/:iid/merge`) combining the source branch and the target branch, and executes CI on that synthetic commit. `head_pipeline.sha` will be the temporary merge commit SHA, **not** the approved source HEAD.
3. **Merge Trains**: Executes CI on a queued sequence of merged commits combining multiple in-flight MRs.

**Adapter Strategy**:
The GitLab gate adapter must inspect the pipeline ref and type:
- If running as a standard source pipeline, enforce `head_pipeline.sha == expected_head_sha`.
- If running as a merged-results or merge-train pipeline, verify that `head_pipeline.ref` is `refs/merge-requests/:iid/merge` and verify via git ancestry or the GitLab API that the synthetic commit has `expected_head_sha` as one of its parents.
- Account for additional external blockers: `detailed_merge_status` may report `status_checks_must_pass` or `security_policy_pipeline_check`.

### 4.4 Stale Base & Rebase Invariant
- **Requirement**: An approved MR whose base branch has advanced must not merge directly; it must be rebased.
- **GitLab Specifics**:
  - The single MR endpoint (`GET /projects/:id/merge_requests/:iid`) returns `diverged_commits_count` **only when the query parameter `?include_diverged_commits_count=true` is passed**. Omitting this parameter results in `None`/missing data.
  - The standard repository compare API (`GET /projects/:id/repository/compare?from=<target>&to=<source>`) returns arrays of `commits` and `diffs`, rather than integer `behind_by` counts. Divergence must be evaluated from commit list length and `compare_same_ref`.
  - Base Commit Tracking: To provide `main_sha` (the base branch tip), the adapter must query `GET /projects/:id/repository/branches/:target_branch`. `diff_refs.start_sha` reflects the target tip when the diff was generated, which may be stale.

### 4.5 Mergeability & `detailed_merge_status`
In GitHub, mergeability primarily checks for textual conflicts (`MERGEABLE` vs `CONFLICTING`).
In GitLab, `detailed_merge_status` is an extensive state machine:

| `detailed_merge_status` | Gate Classification | Action / Routing |
|---|---|---|
| `mergeable` | `Mergeable.CLEAN` | Gate satisfied; eligible for merge. |
| `conflict` | `Mergeable.CONFLICTING` | Textual conflicts; routes to `rebase` task. |
| `need_rebase` | Stale Base | Base branch moved under fast-forward policy; routes to `rebase` task. |
| `checking`, `ci_still_running` | `Mergeable.UNKNOWN` | Asynchronous computation; gate continues polling. |
| `discussions_not_resolved` | Policy Blocker | Unresolved threads; requires address task or resolution. |
| `not_approved` | Policy Blocker | Native approval required. Under shared-account PoC, comment approval is used, so repository must not require native approvals or agent must be provisioned with formal approval token. |
| `draft_status`, `blocked_status` | Policy Blocker | MR is marked draft or blocked by dependencies; escalate to Alice/operator. |
| `status_checks_must_pass`, `security_policy_pipeline_check` | CI / Check Blocker | External compliance checks pending or failed; treat as pending/fail. |
| *Unknown Value* | Fail Closed | Unknown blocker; log and escalate to operator.

---

## 5. Architectural Evaluation: Integration Strategies

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Comparison of Integration Strategies for GitLab                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ Strategy A: CLI-Based (`glab`)                                              │
│  • Pros: Matches existing `gh` subprocess pattern in merge_gate.py.         │
│  • Cons: `glab` is rarely installed by default; version discrepancies       │
│          (e.g., v1.66.0 auto-merge bug); awkward JSON output parsing.       │
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
│  • Cons: Requires adding MCP tool endpoints to hub and worker-mcp.          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Recommendation: **Hybrid Approach (Strategy B for Hub + Strategy A/C for Agents)**
1. **Hub Merge Gate**: Implement via **direct REST API calls using `httpx.AsyncClient`**. Adding `httpx` to `packages/hub` gives a dependency-free, robust, cross-platform client that avoids host CLI issues.
2. **Workers**: Workers need git remote write credentials (via SSH keys or `write_repository` token) to push branches. For change creation and head verification, workers can either use `glab` if available, or call lightweight hub-provided MCP tools (`create_change_request`, `view_change_head`).
3. **Alice Merge Execution**: Alice should execute merges through an MCP tool (`merge_change_request`) or a carefully guarded REST call, completely avoiding the unsafe auto-merge defaults of the `glab` CLI.

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
  - Implement a hardened regex that accommodates multi-tier subgroups, optional ports, and avoids path-traversal vulnerabilities:
    ```python
    # Pattern: https://<host>[:<port>]/<group>/[<subgroup>/...]/<project>/-/merge_requests/<number>
    GITLAB_MR_URL_RE = re.compile(
        r"https://(?P<host>[A-Za-z0-9.-]+(?::[0-9]+)?)"
        r"/(?P<project>(?:(?!/-/)[A-Za-z0-9_.][A-Za-z0-9_.-]*/?)+)"
        r"/-/merge_requests/(?P<number>[1-9][0-9]*)/?"
    )
    ```
  - URL-encode the extracted project path when calling the API:
    ```python
    project_id = quote(match.group("project").rstrip("/"), safe="")
    url = f"https://{host}/api/v4/projects/{project_id}/merge_requests/{number}"
    ```
- **Strings (`store.py`, `protocol.py`)**:
  - Generalize `DEFAULT_GOAL` and payload-cap error messages from "Store work product in GitHub" to "Store work product in the forge (GitHub/GitLab)".

### 6.3 `packages/worker_mcp` (`worker_mcp`)
- If workers use CLI (`glab`), `worker_mcp` has **zero impact**.
- If Strategy C is adopted, `worker_mcp` will expose new helper tools (`create_change_request`, `view_change_head`).

### 6.4 Role Guides (`guides/*.md`)
- Dynamically serve role guides via `GET /guides/{role}.md?forge=gitlab` or provide dual-forge command documentation (`gh` and `glab`).
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
- `scripts/prepare-run.py`: Add `--forge [github|gitlab]` flag and implement GitLab preflight checks (`glab auth status` or token validation via API).
- `scripts/step6.py`, `mock-alice.py`, `mock-worker.py`: Parameterize repository URLs and workflow paths for multi-forge testing.

---

## 7. Infrastructure, Credentials & Security

Self-hosted GitLab instances (GitLab CE/EE) are common in private enterprise environments. Supporting them requires strict security controls:

### 7.1 Token Scopes & Principle of Least Privilege
GitLab distinguishes API access from Git repository access:
- **`api` scope**: Grants full read/write API access. Required by the hub to inspect merge requests, query pipelines, and execute merges.
- **`write_repository` scope**: Grants read/write access via Git-over-HTTP. **Explicitly does not authenticate API requests**.
- **Worker Credentials**: Workers pushing code over HTTPS need `write_repository` (or dedicated SSH deployment keys), but should **not** hold the hub's `api` token.
- **Hub Credentials**: The hub's `GITLAB_TOKEN` must be loaded via `HubSettings.from_env()`, never passed through CLI arguments, and scrubbed from all audit and call logs.

### 7.2 Host-Bound Token Allowlisting & SSRF Prevention
When the hub receives a merge request URL, it must **never send its bearer token to an arbitrary host parsed from untrusted text**:
- Configure `HUB_GITLAB_HOSTS` (defaulting to `gitlab.com`).
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
  ├── Update docs/poc-spec.md §4.2, §8 to define forge-agnostic gate and merge rules
  ├── Generalize URL parsing in scripts/run_common.py for nested paths (group/subgroup/project)
  ├── Generalize user-facing remediation strings in store.py and protocol.py
  └── Add unit tests for GitLab URL parsing and path quote escaping

Phase 2: GitLab Merge Gate Adapter in Hub
  ├── Add httpx runtime dependency to packages/hub/pyproject.toml
  ├── Define ForgeMergeGate protocol in agent_hub.merge_gate
  ├── Implement GitLabMergeGate using httpx against GitLab API v4
  ├── Implement pipeline status mapping, merged-results handling, and ?include_diverged_commits_count=true
  ├── Enforce host-bound token allowlisting and SSRF protection
  └── Write comprehensive unit tests in tests/test_gitlab_merge_gate.py with mock HTTP fixtures

Phase 3: Guide Serving & Orchestrator Skill
  ├── Update hub GET /guides/{role}.md route to serve forge-specific guidance
  ├── Update guides/implementer.md, reviewer.md, rebase.md with glab syntax
  ├── Update skills/alice-orchestrator/SKILL.md with safe merge command (--auto-merge=false)
  └── Test mock-alice and mock-worker workflows against simulated GitLab responses

Phase 4: Run Preparation & Tooling Updates
  ├── Add --forge flag to scripts/prepare-run.py
  ├── Implement GitLab preflight checks (token validity, project permissions, CI config discovery)
  └── Update scripts/hub-report.py to handle GitLab MR references cleanly

Phase 5: Validation & End-to-End Testing
  ├── Validate against a real repository on GitLab.com
  ├── Validate against a local dockerized GitLab CE container (verifying self-hosted TLS and ports)
  └── Update docs/user-guide.md with GitLab setup instructions
```

---

## 9. Conclusion & Recommendation

Supporting GitLab workflows in `robo-agents` is **highly feasible and architecturally clean**. The core pull-coordination architecture, task state machine, and durable leasing engine require zero modifications. By implementing a dedicated `GitLabMergeGate` adapter over HTTPX, handling GitLab's pipeline and auto-merge nuances with care, and scoping credentials properly, robo-agents can provide the exact same rigorous safety guarantees on GitLab as it currently achieves on GitHub.
