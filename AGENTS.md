# Bear Code Repository Working Agreement

## Mandatory doc-first workflow

`RUNTIME_FLOW.md` is the single source of truth for the current project flow.

Before changing any implementation, test, configuration, command, persistence format, or user-facing behavior:

1. Read the relevant section of `RUNTIME_FLOW.md`.
2. Add a `planned` entry to its change ledger and update the affected flow description first.
3. Implement the smallest code change that matches the documented flow.
4. Run checks proportional to the change.
5. Change the ledger entry to `verified`, recording the checks that passed.

If a change is purely editorial and does not alter project behavior, still add a ledger entry stating `flow unchanged`. Do not silently let implementation and `RUNTIME_FLOW.md` diverge.

README and wiki pages are derived explanations. Update them when a change affects installation, commands, architecture, interview narratives, or user-visible behavior, but keep `RUNTIME_FLOW.md` authoritative.

## Mandatory main-merge improvement log

`wiki/阶段性改进.md` is the append-only record of what each merged feature branch actually added to Bear Code.

Before completing a feature-branch merge into `main`:

1. Identify the exact feature commit and its pre-merge `main` baseline from commit parents or the merge commit. Do not compare moving branch names.
2. Update `wiki/阶段性改进.md` using its fixed stage template.
3. Separate capabilities that already existed from capabilities introduced by the stage.
4. Record the core mechanism, non-goals or limitations, verification evidence, and an interview-ready explanation.
5. Register and verify the documentation change in the `RUNTIME_FLOW.md` ledger.

If the merge has already happened, add the stage record during the same merge-finalization work. Do not describe an existing capability as newly added merely because the branch strengthened or governed it.
