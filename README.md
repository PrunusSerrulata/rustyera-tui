# RustyEra Textual TUI

这是 RustyEra runtime 的 Python 3.12/Textual 终端前端，而不是逐行命令行工具。文件
访问、终端渲染、键盘/鼠标采集和平台服务都由前端负责；runtime 通过经过检查的 C ABI
动态加载，并且只接收版本化 CBOR 信封。

这一实现的主要目的，是把以下链路放在一个真实前端中持续验证：

- Rust runtime 与固定 Emuera 参考实现的可观察行为；
- `reference/eraTW` 等真实游戏脚本；
- `era-runtime-capi` 的加载、消息传输、生命周期和存储边界；
- runtime 规范化展示模型能否由独立客户端消费。

它不是终端排版质量、字体兼容性或性能优化的标杆，也不限制未来 GUI、Web 或其他前端
的架构。

## 运行

先编译 release 动态库并安装 Python 依赖：

```sh
cargo build -p era-runtime-capi --release
uv sync --project frontends/era-tui
```

启动：

```sh
uv --project frontends/era-tui run rustyera-tui [RESOURCE_DIRECTORY]
```

`RESOURCE_DIRECTORY` 可省略，默认使用当前工作目录。前端从该目录寻找：

- `CSV/` 和 `ERB/` 源码树；
- macOS 的 `libera_runtime_capi.dylib`；
- Linux 的 `libera_runtime_capi.so`；
- Windows 的 `era_runtime_capi.dll`。

可以通过 `--runtime-library PATH` 或 `ERA_RUNTIME_LIBRARY=PATH` 覆盖动态库位置。
开发工作区可在 `frontends/era-tui` 中建立指向 `../../reference/eraTW/{CSV,ERB}` 和
`../../target/release` 对应动态库的相对符号链接；这些本地链接已加入 `.gitignore`，
不会提交。

前端会跟随资源目录中的源码目录链接，并扫描规范 `ERB/`、`CSV/` 树及项目配置；当
项目没有规范源码树时，也接受直接位于所选目录下的最小项目文件。源码首先按严格
UTF-8（含 BOM）解码；无效时按参考实现的规则回退到 CP932，再统一向 runtime 提交
UTF-8。规范源码树之外的指南、模板和未安装补丁不会被误作游戏源码。Save、
GlobalSave、Data、Log、snapshot、编译缓存和可写 Project overlay 默认都位于资源
目录。设置 `ERA_TUI_DATA_DIR` 可以把 runtime storage namespace 移动到指定目录下按
项目隔离的位置；资源读取仍使用当前资源目录。

## 缓存与重新加载

成功编译的项目缓存为：

```text
.rustyera/cache/compiled-project-v5.bin.zst
```

冷启动和“重启”使用持久化 stat/hash 源文件索引，因此不会重新读取未修改文件；精确
命中编译缓存时，也不必重新把源码 payload 传入 runtime。“重新载入文件夹”执行完整
内容扫描；“返回标题画面”和 VM snapshot restore 复用已经加载的项目。

缓存编码在短暂延迟后于后台启动，避免阻塞标题和第一天启动路径；完成后由前端原子
写入。

## 展示范围

当前前端能够投影规范化 HTML 文本、样式、空白、换行、响应式列、分隔线和交互按钮。
HTML image tag 会被忽略；视频和音频能力目前不会向 runtime 声明。

C# 参考客户端在加载时产生的窗口级提示、CSV warning 和 elapsed-time 行不属于
runtime 规范化游戏展示，因此 TUI 差分只把双方共同的脚本输出作为游戏输出比较。

## 操作

- Enter：按当前 runtime wait 提交输入；
- 点击 Era 按钮：提交不透明 interaction token；
- 在主 viewport 右键：跳过连续的可跳过 Enter wait；
- Ctrl+Z：请求 runtime 持有的 input undo；
- F10：启用 single-step 时执行一个源码行步骤；
- Ctrl+Q：正常退出。

调试菜单会显式完成 debug protocol handshake。变量、纤程与调用栈、console dialog 会先
暂停 VM，再请求一致的 stopped-state view，不读取 Rust 内部对象。纤程表按窗口高度占据
表格区约三分之一，最多显示八个数据行；TUI 不展示操作数栈，但底层调试协议仍保留该能力。

## 测试

```sh
UV_CACHE_DIR=/tmp/rustyera-uv-cache \
  uv --project frontends/era-tui run pytest

UV_CACHE_DIR=/tmp/rustyera-uv-cache \
  uv --project frontends/era-tui run ruff check \
  frontends/era-tui/src frontends/era-tui/tests
```
