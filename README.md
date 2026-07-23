# RustyEra Textual TUI

This is a real terminal UI for the RustyEra runtime, not a line-oriented CLI. It keeps
filesystem access, terminal rendering, keyboard/mouse collection, and platform services in
the frontend. The runtime is loaded through its checked C ABI and is driven exclusively with
versioned CBOR envelopes.

## Run

Build the release dynamic library once, create the Python environment with `uv`, then start
the TUI from its prepared resource directory:

```sh
cargo build -p era-runtime-capi --release
cd frontends/era-tui
uv sync
uv run rustyera-tui
```

The optional positional argument is a resource directory and defaults to the current working
directory. The frontend finds the platform `era-runtime-capi` dynamic library and the `CSV/`
and `ERB/` source trees below that directory. This checkout prepares relative links in
`frontends/era-tui` for both source trees and every supported platform library name. Override
the library with `--runtime-library PATH` or `ERA_RUNTIME_LIBRARY=PATH`.

The frontend follows the resource directory's source-tree links and scans `.erb`, `.erh`,
`.csv`, and `.config` files as UTF-8. Save, GlobalSave, Data, Log, snapshots, compiler caches,
and writable Project-overlay storage default to the resource directory. Set
`ERA_TUI_DATA_DIR` to move runtime storage namespaces to an isolated per-project directory
below that base. The frontend renders normalized HTML text, styles, spacing, line breaks, and
buttons; HTML image tags are ignored. Video and audio remain intentionally unadvertised.

Successful builds are cached as an opaque runtime artifact at
`.rustyera/cache/compiled-project-v4.bin.zst` below that same storage root. Cold starts and
**重启** use a persistent stat/hash source index, so unchanged files are not reopened; an exact
cache hit also avoids transferring source payloads into the runtime. **重新载入文件夹** performs a
full content scan. **返回标题画面** and VM snapshot restore reuse the active loaded project without
scanning source files. Cache encoding starts in the background after a short delay, so it does not
hold up the title or day-one startup path; the frontend persists the completed payload atomically.

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
