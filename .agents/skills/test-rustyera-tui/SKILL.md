---
name: test-rustyera-tui
description: Run deterministic fixed-sequence, agent-driven, save/snapshot restore, state-inspection, and Emuera differential tests through rustyera-tui's real RuntimeWorker and C ABI. Use after TUI/runtime-facing changes, when reproducing game-flow failures, when auditing eraTW milestones or performance, or whenever a test must record and compare interactive game input, output, waits, and watched state.
---

# Test RustyEra TUI

Drive `rustyera-test`; do not recreate a Python input state machine or call Rust runtime internals.
Read [test-cli.md](references/test-cli.md) before authoring or changing a scenario.

## Enforce the batch budget

Use the batches defined by the root `AGENTS.md`: estimate each requested feature/change/fix first,
combine small items for implementation, refactoring review, and testing, and handle large items
independently with separate budgets. Keep a separate commit for each item regardless of batching.
All review counts, suite counts, gates, and deadlines below apply to the current batch.

Follow the root `AGENTS.md` parallel scheduling rules. Run independent checks concurrently when
their inputs, outputs, and mutable resources are isolated; pipeline dependent checks as prerequisites
pass. Parallelism never bypasses the required review, focused-before-full, or static-before-dynamic
gates. Delegate test execution as required by the component's `AGENTS.md`.

- Before starting any test command, confirm that any required refactoring subagent has completed
  its single permitted run and that every requirement it reported has been implemented. Refuse to
  start testing while any refactoring requirement remains. Once the first test starts, never spawn,
  resume, follow up with, or rerun a refactoring subagent during that batch.
- Start one shared 60-minute wall-clock budget with the batch's first test command. It includes all
  later tests, targeted reruns, end-to-end waits, and test-failure investigation. Bound every
  command by the remaining time.
- Start each distinct full test suite at most once. After a failure is fixed, rerun only the
  directly affected node IDs, test files, or scenarios; never rerun the full suite in that batch.
- Run every command that may outlive its initial tool response in a persistent PTY. Start it with
  `exec_command` using `tty: true` and a short yield, retain the returned `session_id`, and poll only
  with `write_stdin` at intervals no longer than 30 seconds until an explicit exit code is observed.
  Do not resume a yielded exec cell with a separate wait call: the cell may be reclaimed before its
  result is collected. If a PTY session disappears without an exit code, report the command as
  unverified; never restart a full suite, and rerun a targeted command only when the suite rules
  permit it.
- At 60 minutes, terminate every test process for that batch and report the active command, exact scenario/step,
  last stable-wait observation, elapsed time, completed checks, and unverified checks.

## Prepare the run

1. Inspect `pyproject.toml`, `AGENTS.md`, the relevant code diff, and the selected scenario.
2. Resolve the game project and C ABI library. Keep game resources and the Emuera repository
   read-only; place data and traces under `.rustyera/` or a temporary directory.
3. Use a committed scenario under `tools/runtime-tester/scenarios/` when it covers the task.
   Create a new scenario only for reusable behavior.
4. Leave `seed` absent for randomized exploration. Read the effective seed from the first trace
   event. Copy that value into the scenario or command artifact before claiming a reproduction.

## Run fixed tests

Run:

```sh
uv run rustyera-test run --scenario SCENARIO --runtime-library LIBRARY
```

For reference comparison, pass the command as one shell-quoted argument, for example
`--reference-command 'wine Emuera.ReferenceCli.exe'`; on macOS/Wine also pass
`--reference-path-command 'winepath -w'`. Treat an empty response, timeout, premature exit, schema
mismatch, or missing capability as a test-infrastructure failure, not a skipped comparison.

Stop at the first reported semantic difference. Preserve the trace and report the exact step,
input, Rust observation, reference observation, seed, and watches.

## Run autonomous tests

Start `serve` in a persistent terminal session. Parse each NDJSON observation before sending one
NDJSON command on stdin.

- Use `{"op":"step","input":"..."}` for the next action.
- Use `{"op":"inspect","watches":["FLAG:0"]}` only when output is insufficient.
- Use `{"op":"export_snapshot","path":"..."}` for a requested checkpoint.
- Use `{"op":"stop"}` only after the goal is satisfied or continuing cannot add coverage.

Prefer visible, valid choices. Never invent hidden state or bypass input validation. Continue until
the machine-readable goal succeeds, the first differential failure occurs, or the hard step/time
budget is exhausted. TUI runs observe complete state at stable input waits by default; use periodic
snapshots only when the user explicitly requests them. A budget exhaustion is a failed test unless
the scenario explicitly defines exploration-only acceptance; the batch-wide 60-minute deadline is
always a failure.

## Interpret and report

- Compare normalized text added since the previous stable wait, wait semantics, termination, and
  configured watches. Do not hide differences unless the scenario names the ignore rule.
- Traditional saves are comparable across implementations. VM snapshots are RustyEra-only unless
  the scenario supplies an equivalent reference save.
- A traditional save or VM snapshot owns its RNG state; do not reseed after restore.
- Report the scenario, command, exit code, effective seed, trace path, completed assertions,
  first difference, and every blocked or unverified check.

## Validate repository changes

Run the smallest relevant pytest before the complete TUI pytest, which may run once. Ruff may run
alongside independent pytest checks. Complete all applicable static gates before dynamic scenarios.
If the complete pytest fails, fix it and rerun only the directly affected node IDs or test files.
Run real C ABI fixture or eraTW scenarios when runtime behavior changed. If the Emuera reference
CLI changed, use the sibling core repository's `$test-rustyera-core` workflow for its required Rust
gates, platform smoke, and same-input differential evidence.
