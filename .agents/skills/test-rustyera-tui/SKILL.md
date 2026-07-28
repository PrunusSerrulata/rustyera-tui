---
name: test-rustyera-tui
description: Run deterministic fixed-sequence, agent-driven, save/snapshot restore, state-inspection, and Emuera differential tests through rustyera-tui's real RuntimeWorker and C ABI. Use after TUI/runtime-facing changes, when reproducing game-flow failures, when auditing eraTW milestones or performance, or whenever a test must record and compare interactive game input, output, waits, and watched state.
---

# Test RustyEra TUI

Drive `rustyera-test`; do not recreate a Python input state machine or call Rust runtime internals.
Read [test-cli.md](references/test-cli.md) before authoring or changing a scenario.

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
budget is exhausted. A budget exhaustion is a failed test unless the scenario explicitly defines
exploration-only acceptance.

## Interpret and report

- Compare normalized text added since the previous stable wait, wait semantics, termination, and
  configured watches. Do not hide differences unless the scenario names the ignore rule.
- Traditional saves are comparable across implementations. VM snapshots are RustyEra-only unless
  the scenario supplies an equivalent reference save.
- A traditional save or VM snapshot owns its RNG state; do not reseed after restore.
- Report the scenario, command, exit code, effective seed, trace path, completed assertions,
  first difference, and every blocked or unverified check.

## Validate repository changes

Run the smallest relevant pytest first, then the complete TUI pytest and Ruff checks. Run real C ABI
fixture or eraTW scenarios when runtime behavior changed. If the Emuera reference CLI changed, use
the sibling core repository's `$test-rustyera-core` workflow for its required Rust gates, platform
smoke, and same-input differential evidence.
