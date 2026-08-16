# `rustyera-test` protocol

## Scenario schema

Use JSON `schema_version: 1`.

- `project`: Required absolute path or path relative to the scenario.
- `mode`: `fixed` or `autonomous`.
- `start.type`: `new_game`, `traditional_save`, or `vm_snapshot`. Restore types require `path`.
- `seed`: Optional unsigned 64-bit integer. Use a decimal string for the full range when authoring
  portable JSON. When absent, the driver generates a random seed and records it. It applies only
  to `new_game`.
- `inputs`: Fixed prefix. An item is a string/integer or `{ "value": ..., "when": {
  "output_contains": ... } }`. Use `{ "action": "skip_message" }` to submit the visible
  Enter/AnyKey wait with continuous message skipping in Rust-only scenarios; reference comparison
  is unavailable for that frontend gesture.
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

In a `serve` session, source-reload testing may also submit these operations one at a time after an
observation:

- `{"op":"wait_status","text":"项目缓存已保存。"}` waits for a frontend status without changing
  the active input. Observing this status also authorizes a requested compiled-cache handoff, so a
  cold-start producer does not need an artificial source reload before publishing its cache.
- `{"op":"restart"}` recreates the runtime against the same isolated project.
- `{"op":"edit_source","path":"ERB/main.erb","expected":"v1","replacement":"v2"}` replaces one
  exact UTF-8 source fragment inside the isolated project while the game stays running.
- `{"op":"reload","scope":"all"}` reloads all sources; `folder` and `file` scopes additionally
  require a project-relative `path`.

These produce `status_observed`, `source_edited`, or `runtime_action` trace events. They are Rust-only
frontend operations and must not be used with reference comparison.

Cross-host cache tests may set `RUSTYERA_TEST_COMPILED_CACHE_INPUT` and
`RUSTYERA_TEST_COMPILED_CACHE_OUTPUT` to explicit opaque cache files. The real RuntimeWorker consumes
and persists those files through its project storage. `RUSTYERA_TEST_SOURCE_INDEX_INPUT` and
`RUSTYERA_TEST_SOURCE_INDEX_OUTPUT` transfer the matching portable source index and require the
consumer to report actual file reuse. `RUSTYERA_TEST_PROJECT_OUTPUT` exports the isolated,
hot-reloaded source tree without `.rustyera` for a following Browser/WASM run.

By default, the TUI driver records its complete observable state when it reaches a stable input
wait. It does not emit periodic snapshots or treat equal consecutive observations as a stall unless
the user explicitly requests that policy. All test commands in a task share the 60-minute
wall-clock budget defined by the repository rules.

Exit codes are `0` for passed/stopped fixed work, `1` for semantic failure, `2` for input/budget
exhaustion, and `3` for infrastructure, schema, or protocol failure.

## Wait kinds

Rust wait kinds are: `0` Enter, `1` any key, `2` integer, `3` string, `4` void, `5` any value,
`6` integer button, `7` string button, and `8` primitive mouse/key. The driver automatically
submits pure Enter waits without consuming a fixed or agent input.
