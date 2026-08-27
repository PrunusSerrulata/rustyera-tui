# Snake services unavailable in TUI

Three minimal projects exercise the terminal's intentional lack of HTML pixel measurement,
pointer observation and canvas-pixel sampling. Each project selects `emuera.skia.snake`, emits
its `SNAKE_UNSUPPORTED_*` marker and waits for the visible integer input before calling the API.
Reaching `SNAKE_UNSUPPORTED_UNEXPECTED_SUCCESS` or a second wait is a failure.

The real-worker test derives the HTML substring and lines variants only in the temporary project
copy; the checked-in HTML source remains the length case. HTML requires PresentationQuery
`html_string_len`, `html_substring`, or `html_string_lines` v2.0. Pointer requires InputState
`pointer_state` v1.0 and canvas requires Canvas `sample_canvas_pixel` v1.0.

Run the corresponding explicit-opt-in cases in `tests/test_runtime_real_cabi.py` only after all
shared static gates and the final core binding/build. They require an explicit `ERA_RUNTIME_LIBRARY`;
when enabled, missing libraries fail instead of skipping or discovering a different workspace's
library. Seed is fixed at 123456. The test copies source into pytest's temporary directory and puts
frontend data there; no user game or fixture directory is mutated during execution.

These are expected capability faults, not successful graphical implementations. The existing
`rustyera-test` CLI treats runtime faults as infrastructure errors and has no expected-fault goal.
No fake CLI goal or exception-to-success workaround is added. Successful non-graphical integration
continues to use `tools/runtime-tester/scenarios/snake-data.json`.
