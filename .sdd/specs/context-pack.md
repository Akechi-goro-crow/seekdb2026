---
id: sdd-context-pack-v1
kind: spec
status: active
summary: Generate a bounded, traceable context pack for each engineering task.
paths: [tools/sdd_context.py]
tests: [tools/tests/test_sdd_context.py]
---

# Context pack specification

## Goal

An AI coding assistant must receive the current task boundary, the active
specifications and decisions, the smallest useful set of related source files,
and the tests that can verify a change. The pack must remain useful after a
new session starts or another developer takes over.

## Invariants

1. Every included SDD artifact has an active status. Superseded, rejected, or
   expired artifacts are retained for audit but never injected into a fresh
   pack.
2. Every source or test recommendation is a repository-relative path. A pack
   records the Git revision used to select it so that a later session can
   detect drift.
3. A character budget limits the number of SDD artifacts. Omitted artifacts are
   named explicitly instead of being silently dropped.
4. A handoff records the branch and worktree state, and tells the next session
   how to resume.

## Acceptance checks

- `python tools/sdd_context.py check` passes.
- `python tools/sdd_context.py pack --task "SDD context management"` includes
  this spec and the context tool.
- `python tools/sdd_context.py handoff --task "SDD context management"`
  includes the current branch and a resumable next step.
