# RustyEra TUI development

本仓库只包含 Python/Textual TUI、其测试、审计工具和发布配置。Runtime、协议与 C ABI
实现在独立 [rustyera-core](https://github.com/PrunusSerrulata/rustyera) 仓库；不得复制
Rust runtime 实现或绕过公共 C ABI。

- Python 版本、依赖和 lint 规则以 `pyproject.toml`、`uv.lock` 为准。
- `rustyera-core.rev` 是发布所使用的唯一 core revision；更新它时必须验证 C ABI、
  协议版本和所有 TUI 测试。
- 本地默认游戏为兄弟目录 `../eraTW`，共享动态库位于 `../target/release`。两者都不得提交。
- 参考 oracle 位于 `../emuera.em`，默认只读；差分脚本支持环境变量覆盖。
- 修改必须包含对应 pytest；提交前运行 pytest、Ruff 和相关打包冒烟测试。
- 不提交 venv、构建产物、存档、游戏资源、动态库或诊断缓存。
