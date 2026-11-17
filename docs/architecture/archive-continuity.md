# Archive continuity and selective loading

## Canonical files

- Private location guide: maintained locally, outside the source repository.
- Sole current private checkpoint: an untracked handoff selected by that guide.
- Local governing instructions: untracked repository-root `AGENTS.md`.
- Workflow contract: [preservation ADR](archive-preservation-architecture-adr.md).
- Tool ownership: [application structure](archive-application-structure.md).
- Research permissions/acceptance: [pilot scope](archive-research-pilot-scope.md).
- Next-phase direction and ordered TODO: [consolidation plan](archive-django-consolidation-plan.md).
- Launch/configuration/rebuild procedures: [pilot operating guide](../../Archive-tools/research_web/README.md).

This contract replaces the old work-root/unit-handoff convention. There is no
second editable checkpoint or compatibility handoff symlink. The home routing
file chooses archive or website before loading project instructions. The
website has its own checkpoint, separate from archive operations.

## What belongs where

The handoff contains current task, branch/working-tree state, operating risks,
next action and dated verification pointers. Maximum 900 whitespace-separated
words (approximately 1–2 pages); aim for 600–800. Replace superseded state at
closeout; do not prepend dated sessions or accumulate completed test logs.

Stable rules and architecture belong in the canonical documents above; reusable
commands belong in operating READMEs. Keep the ordered future work in the existing
plan, not a parallel backlog. Git history records source changes. Private source
bindings, case findings, review decisions, unit completion receipts, machine
configuration and operational history remain outside the source repository.
Do not publish research evidence merely to shorten a handoff.

Private location maps identify retained notes and session history. Their paths and
statements describe their original dates. Existing unit notes and receipts remain
beside the work they document. Completed units must not become active tasks merely
because historical instructions describe a next step. Captured backups, release
trees and export snapshots are evidence, never startup inputs.

Public README files and documentation describe repository structure, interfaces,
formats and reproducible examples. Do not include a workstation's directory map,
user-home paths, device names, private evidence links or live process details.
Keep those in local configuration, operator instructions and the private handoff.

## Startup and compaction

1. Select the workspace and read its governing instructions unless already present.
2. Read the current handoff when starting a new task, or when its state is missing
   or changed. Do not read every linked document.
3. Load only the headings/code spans needed for the next action. Outline long
   files first; keep searches scoped and output bounded.
4. After compaction, continue from the retained task summary. Reuse unchanged
   guidance and reread only missing details or changed files. Comparing file
   modification time or a saved digest can detect a change; it does not replace
   reading relevant changed instructions.
5. Before operations, recheck live mounts, process ownership, sockets, source
   bindings and Git state as appropriate. Never use an old PID as stop authority.

The retained summary should carry the user's task and authorization limits,
non-negotiable safeguards, decisions, changed files, completed verification and
its limits, next action, and exact document/section pointers already consulted.
Do not copy whole architecture documents into the summary or local memory.

## Closeout

Route durable discoveries to their existing owners, then replace only materially
changed checkpoint state. Label runtime observations with their date; separate
last verified results from fresh checks and record what remains unverified.
Check changed links, whitespace, the word ceiling and preservation of private
evidence. A documentation-only cleanup does not require application tests.

```sh
# Set this to the existing private checkpoint, outside the repository.
ARCHIVE_HANDOFF=/path/to/private/HANDOFF.md
wc -w "$ARCHIVE_HANDOFF"
test "$(wc -w < "$ARCHIVE_HANDOFF")" -le 900
git diff --check
```

Do not commit or publish private handoffs/evidence. A source commit does not
deploy a runtime, start a process, approve research or authorize accession.
