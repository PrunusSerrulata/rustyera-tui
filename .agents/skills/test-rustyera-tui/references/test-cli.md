# `rustyera-test` protocol

## Scenario schema

Use JSON `schema_version: 1`.

- `project`: Required absolute path or path relative to the scenario.
- `mode`: `fixed` or `autonomous`.
- `start.type`: `new_game`, `traditional_save`, or `vm_snapshot`. Restore types require `path`.
- `seed`: Optional non-negative signed 32-bit integer. When absent, the driver generates a random
  seed and records it. It applies only to `new_game`.
- `inputs`: Fixed prefix. An item is a string/integer or `{ "value": ..., "when": {
  "output_contains": ... } }`.
- `watches`: Debug expressions evaluated at stable waits on Rust and with reference `watch`.
- `goal`: AND-combined `output_contains`, `wait_kind`, `termination`, `watch_equals`,
  `line_count_lte`, and `status_contains` assertions.
- `limits`: Positive `max_steps` and `timeout_seconds`.
- `comparison.reference`: Enable the persistent reference CLI. `ignore_output` contains explicit
  regular expressions. Wait kinds use the built-in Rust-to-Emuera map; `wait_kind_map` may
  override individual entries for an intentional compatibility case.
- `checkpoint`: Request a snapshot at the first stable, deadline-free eligible wait. Omit `path`
  to write beside the trace.

Relative paths resolve from the scenario, never from the caller's current directory.

## NDJSON events

`run` and `serve` write the same event structure to stdout and `trace.ndjson`. The trace retains
complete `output` arrays; stdout omits those large arrays from observations and keeps
`output_tail` plus at most the last 30 lines of `output_delta.added` (with `added_omitted` when
truncated), so an agent always receives the next wait without transport truncation:

- `observation`: Rust state, optional reference state and comparison, goal status, output delta,
  tail, wait, watches, statuses, and metrics.
- `input`: Step, source (`fixed`, `agent`, or `automatic_enter`), and exact submitted value.
- `inspection`: Requested watch values.
- `checkpoint` / `checkpoint_requested`: Snapshot result or agent request.
- `result`: `passed`, `difference`, `goal_not_met`, `checkpoint_not_created`, `input_exhausted`,
  `budget_exhausted`, or `stopped`.
- `error`: Infrastructure or protocol failure.

From process launch through exit, the driver must also emit a complete status snapshot every 5
seconds. It contains the full rendered interface element tree plus runtime state, wait,
presentation, output, statuses, watches, and logs; if an HTML surface participates, it additionally
enumerates every current HTML element with tag, attributes, text/value, and visibility. Snapshot
equality ignores only timestamps and reporting-only metadata. If a snapshot is identical to the
immediately preceding snapshot, emit `error`, terminate the run at once as stalled, and do not wait
for the scenario timeout. All test commands in a task share the 60-minute wall-clock budget defined
by the repository rules.

Exit codes are `0` for passed/stopped fixed work, `1` for semantic failure, `2` for input/budget
exhaustion, and `3` for infrastructure, schema, or protocol failure.

## Wait kinds

Rust wait kinds are: `0` Enter, `1` any key, `2` integer, `3` string, `4` void, `5` any value,
`6` integer button, `7` string button, and `8` primitive mouse/key. The driver automatically
submits pure Enter waits without consuming a fixed or agent input.
