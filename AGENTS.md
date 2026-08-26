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

