# TUI audit tools

本目录通过真实 `rustyera_tui` worker 和 C ABI 动态库执行长流程、snapshot restore 与
参考 fixture 比较。

- `ERA_AUDIT_PROJECT` 默认 `../../eraTW`（即 workspace 外层 `eraTW`）。
- `ERA_RUNTIME_CAPI` 默认外层共享 `target/release` 的当前平台动态库。
- `EMUERA_REFERENCE_ROOT` 默认外层 `emuera.em`。
- 工具只能读取游戏与参考仓库；输出写临时目录或被忽略的本地目录。
- 单元测试为 `test_tui_day1.py` 与 `test_tui_fixture_compare.py`，随普通 pytest 一起运行。
