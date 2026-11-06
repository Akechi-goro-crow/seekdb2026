---
id: decision-context-pack-boundary
kind: decision
status: active
summary: Keep durable SDD artifacts in Git and derive task packs on demand.
paths: [tools/sdd_context.py, .sdd]
tests: [tools/tests/test_sdd_context.py]
---

# Decision: derive packs on demand

The repository is the durable source of truth for specifications, decisions,
and links to evidence. A context pack is a generated view, never a second
mutable database. This makes handoffs reviewable in a diff and prevents an
old generated pack from silently overriding a newer decision.

The generator must therefore record the source revision, apply status and
expiry filtering at generation time, and report budget omissions.
