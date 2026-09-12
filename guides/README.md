# Role guides

Runtime-agnostic instructions for workers, served by the hub at
`GET /guides/{role}.md` (spec §4.2) and fetched through the worker's
`get_role_guide(role)` tool. Every runtime gets identical text, which is why
worker behaviour cannot live in a Claude Code skill.

The content is written in plan Step 5: `worker.md`, `implementer.md` and
`reviewer.md` (spec §5). Until then every request for those is a `404`.
`rebase.md` is already here: it came with the REBASE step (GitHub issue #41),
and `assign_task(role="rebase")` points workers at it.

File names are role names as they appear in `assign_task(role=...)`: lowercase
slugs matching `[a-z][a-z0-9-]*`, with a `.md` suffix. Anything else here —
this README included — is not a role and is not served.
