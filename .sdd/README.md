---
id: sdd-context-contract
kind: context
status: active
summary: Document the durable SDD artifact and context pack contract.
---

# SDD context contract

This directory is the durable handoff surface for coding agents. Markdown
files use a small front matter contract:

```yaml
---
id: unique-stable-id
kind: spec | decision | task | handoff
status: active | superseded | expired
summary: one sentence describing the artifact
paths: [src/example, tools/example.test]
tests: [tools/example.test]
---
```

`tools/sdd_context.py pack --task "..."` emits the active specifications and
the most relevant source and test files. `handoff` adds branch and worktree
state for the next session. `check` rejects duplicate IDs, malformed expiry
dates, and references to missing paths. A superseded artifact remains in Git
for auditability but is excluded from a new pack.

The contract keeps evidence separate from conclusions: specs and decisions
state what should be true, while `paths` and `tests` point to the code and
verification that can prove it.
