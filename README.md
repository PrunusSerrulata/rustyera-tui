# RustyEra Textual TUI

This is a real terminal UI for the RustyEra runtime, not a line-oriented CLI. It keeps
filesystem access, terminal rendering, keyboard/mouse collection, and platform services in
the frontend. The runtime is loaded through its checked C ABI and is driven exclusively with
versioned CBOR envelopes.

## Run

Build the dynamic library once, create the Python environment with `uv`, then start the TUI:

```sh
cargo build -p era-runtime-capi --release
cd frontends/era-tui
uv sync
uv run rustyera-tui ../../reference/eraTW
```

The dynamic library is discovered in the workspace `target/release` directory. Override it
with `--runtime-library PATH` or `ERA_RUNTIME_LIBRARY=PATH`.

The optional project argument may be omitted; use **文件 → 重新载入文件夹...** to select a
project. The frontend scans `.erb`, `.erh`, `.csv`, and `.config` files as UTF-8. Save,
GlobalSave, Data, Log, and writable Project-overlay storage defaults to namespaced directories
inside the selected project. Set `ERA_TUI_DATA_DIR` to move these namespaces to an isolated
per-project directory below that base. Resource reads continue to use the selected project
directory. The frontend renders normalized HTML text, styles, spacing, line breaks, and
buttons; HTML image tags are ignored. Video and audio remain intentionally unadvertised.

## Controls

- Enter submits the prompt according to the active runtime wait.
- Click an inline Era button to submit its opaque interaction token.
- Right-click the main viewport to skip consecutive skippable Enter waits.
- Ctrl+Z requests runtime-owned input undo when available.
- F10 performs one source-line step while single-step mode is enabled.
- Ctrl+Q exits cleanly.

The debug menu performs an explicit debug protocol handshake. Variable, stack, and console
dialogs pause the VM before requesting a coherent stopped-state view and never inspect Rust
objects directly.

## Test

```sh
uv run pytest
```
