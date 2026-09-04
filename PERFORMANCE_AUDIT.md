# TUI performance audit infrastructure

`rustyera_tui.performance` is the TUI's only authoritative performance probe. It covers project
loading, public C ABI traffic, presentation projection, Textual mutation, and the next refresh.
`tools/startup-benchmark.py` and the legacy startup JSONL wire remain fully compatible for existing
startup regression jobs. They retain their CLI, scenarios, validation, summaries, and product
progress timings, while also feeding the unified probe when the new audit opt-in is active. They
are compatibility/reporting entry points, not a second runtime audit buffer or schema authority.

This change only installs infrastructure. It contains no real snake-TW trace, measurements, or
performance conclusions, and none of the examples below were run while implementing it.

## Opt-in and event contract

Normal application runs do not enable the probe. They use a disabled singleton, do not start
clocks, do not allocate detail maps, and do not append samples. An audit launcher passes a
non-blocking inherited FIFO as `RUSTYERA_PERF_FD`. The legacy
`RUSTYERA_STARTUP_TELEMETRY_FD` continues to emit only the original startup schema and does not
implicitly enable schema v2. The process resolves the new descriptor
once. Invalid, blocking, non-FIFO, oversized, or full-pipe output is dropped without changing
application behavior.

Telemetry uses schema version 2. Trace files independently use schema version 1. Every telemetry
record has `schemaVersion`, `traceVersion`, `epoch`, monotonic `sequence`, `event`, `phase`,
`stage`, `operation`, `monotonicNs`, `durationNs`, `droppedCount`, `identity`, `memory`,
`allocator`, and `support`. Ordinary hot-path records keep `memory` null; explicit low-frequency
memory checkpoints sample peak RSS. Stage-specific records add byte/count fields, VM instructions,
runtime transitions, snapshot/delta counts, logical line/run/resource counts, scene layers, and
terminal status where available.

The fixed-capacity deque overwrites its oldest entry in O(1) and increments `droppedCount`.
`reset()` starts a new epoch, clears the ring, and makes outstanding span tokens stale, preventing
late asynchronous loading results from entering a later audit. A terminal record is emitted via
the same buffer and sink. Ordinary timing rounds keep `allocator` JSON `null`. Allocation rounds
must additionally set `RUSTYERA_PERF_ALLOCATIONS=1`; only explicit low-frequency checkpoints use
standard-library `tracemalloc` to report current/peak traced Python bytes and snapshot diff
count/bytes. It does not cover Rust/C ABI native heap, which remains the responsibility of
`heap`/`malloc_history`. TUI has no DOM or next-paint API (`not_applicable`) and does not render protocol
canvas content (`unsupported`); it never reports Web-equivalent measurements for them. `rssBytes`
is also null where a portable current-RSS source is unavailable, while `peakRssBytes` uses the
platform resource API.

Audit metadata records a disabled-branch versus in-memory counting calibration as `offNs`,
`countingNs`, and `overheadPercent`. Sink and profiler measurements remain attribution-only if
their observed overhead exceeds the audit threshold.

The stage vocabulary is shared across all TUI layers:

- Loading: `project/scan_enumerate`, `project/index_read`, `project/stat`,
  `project/read_decode_hash`, `project/index_write/…`, `host/cache_read`,
  `host/source_materialize`, `host/submission_transfer`, and Core-reported normalization,
  CSV, parse, analyze, compile, validation, cache decode, and load phases.
- Runtime: `c_abi/submit`, `c_abi/drive`, `c_abi/poll`, `protocol/decode`, and
  `pump/complete`, including envelope bytes/counts, VM instructions, transitions, queue depth,
  and state.
- Frontend: `store/presentation_batch`, `presentation/commit`, and
  `render/next_refresh`, including snapshot/delta and presentation object counts.

## Capture, freeze, and replay boundary

`tools/performance-audit.py capture-template` writes only a `captureRequired: true` template. Such
a file is deliberately rejected by replay and cannot be presented as evidence. A real background
autonomous session uses `performance-audit.py capture`, the headless `RuntimeWorker` lifecycle and
public C ABI. It emits each observation to stdout and accepts one exact controller action on stdin;
EOF, an incomplete final action, or an exhausted step budget fails without publishing evidence. It
export each stable observation as a candidate step with `id`, `checkpoint`, `normalized`, and
`action`. `freeze` derives the replay `expect` object from that evidence; it only canonicalizes the
captured product and does not invent
inputs or observations.

The capture candidate and replay CLI must explicitly name profile `emuera.skia.snake`; the frozen
protocol-schema trace intentionally omits that CLI-owned field. It contains a scenario, seed,
actual project digest, and the TUI's actually advertised client features/capabilities (keyboard
and mouse modalities, timed viewport/device pump, and
`rustyera.sql`), setup messages, and at least one step. The final step must use action `none`.
Each candidate `normalized` object contains exactly `phase`, `wait`, `lines`, `resources`, `scene`,
`variables`, `services`, `storage`, and `otherOutboundTags`. Service/storage request IDs and
deadlines are removed, but payloads and order are retained. The lowercase `stateSignature` is the
SHA-256 of compact recursively key-sorted JSON. `traceDigest` uses the same algorithm after
removing `traceDigest` itself. Freeze removes `captureRequired`, `profile`, and `normalized` after
computing the signature, leaving exactly the shared strict field structure; the source candidate
remains the companion evidence. Core `perf-run` currently requires latch/get-key-state capabilities
that TUI truthfully does not advertise, so this TUI trace is not passed off as a directly acceptable
Core trace. A Web/Tauri capture or explicit semantic adapter may provide the stronger Core client
contract while preserving the same captured checkpoints and actions.

This is the shared semantic checkpoint/action boundary used by Web capture and Core/TUI tooling; a
second handwritten TUI trajectory or action dialect is invalid. `ReplayVerifier` compares every complete normalized
checkpoint, critical variable, wait, scene/service/storage payload, and signature before returning
the next captured action. Unexpected `idle`, `stopped`, or `faulted` state fails immediately.

The audit spec requires separate explicit source and isolated project paths and rejects equality,
nested copies, the original profile, zero iterations, mismatched scenario/trace identity, fake
templates, modified digests, and ambiguous profiler pause selection. The CLI `replay` command runs
the real `RuntimeWorker`/public C ABI lifecycle without constructing a Textual application or any
GUI. It performs one warmup followed by the requested measured replays, validates every stable
input checkpoint before applying its captured action, and emits one terminal record per round.
An audit-only decoder hook retains the ordered service, storage, and other outbound messages that
the normal worker consumes inside a pump. It removes request IDs and deadlines, and both capture
and replay include those payloads in the canonical signature; the hook is never constructed in a
normal production run.

```text
python tools/performance-audit.py capture-template \
  --scenario day-one --output .rustyera/perf/day-one.capture.json

python tools/performance-audit.py capture \
  --source-project /read-only/source-copy --project /isolated/audit-copy \
  --profile emuera.skia.snake --scenario day-one --seed 1 \
  --output .rustyera/perf/day-one.captured.json

python tools/performance-audit.py freeze \
  --capture .rustyera/perf/day-one.captured.json \
  --output .rustyera/perf/day-one.trace.json

python tools/performance-audit.py replay \
  --source-project /read-only/source-copy \
  --project /isolated/audit-copy \
  --profile emuera.skia.snake --scenario day-one \
  --trace .rustyera/perf/day-one.trace.json --iterations 5 \
  --output .rustyera/perf/day-one.tui.jsonl
```

## Deadlines and headless profiling

One `AuditBudget` is created before replay and shared unchanged by warmup, baseline, CPU profile,
and Python/native allocation profile rounds. Its deadline is exactly 60 minutes. Every round is a
runner-owned child process with a fresh process group; the final process manifest records its PID,
PGID, start identity, descendants, checkpoint session identity, exit status, and profile evidence.
Each child timeout is capped to `remaining()`; expiration terminates only process groups explicitly
created and registered by that audit and records the interrupted phase as incomplete. A new child
must never create a new budget.

The state watchdog is independent of operation waits and requires a complete refresh in under five
seconds (the implementation defaults to four). An explicit checkpoint pause is allowed for one
iteration, or for the selected zero-based iteration of a repeated run. Before pausing, the child
fsyncs JSONL and atomically publishes its PID/checkpoint/session identity. The supervisor verifies
that identity, runs the selected headless profilers within the same remaining budget, then releases
the child over its owned stdin pipe; EOF fails. `MallocStackLogging=1` is injected before spawning
only the allocation child and is removed from baseline child environments. No visible GUI is involved.
`profiler-plan` accepts only a live, identity-checked checkpoint manifest emitted by an owned
audit child, then prints argv arrays and unique capture targets for `/usr/bin/sample`,
`/usr/bin/heap`, `/usr/bin/vmmap`, `/usr/bin/leaks`, and `/usr/bin/malloc_history`; it never
executes them itself. The execution helper opens targets exclusively, captures stdout where the
tool has no output-file option, and returns path, byte count, and SHA-256 for every artifact.

```text
python tools/performance-audit.py profiler-plan \
  --checkpoint-manifest .rustyera/perf/day-one.checkpoint.json \
  --output .rustyera/perf/day-one
```
