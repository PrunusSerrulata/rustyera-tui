# AGENTS.md

本文件适用于仓库根目录及其所有子目录。若更深层目录存在 `AGENTS.md`，以更具体的
规则为准。

## 项目边界

本仓库只实现 Python 3.12/Textual TUI、C ABI 客户端、测试工具和发布配置。Runtime、
协议与 C ABI 实现在独立的 `rustyera-core` 仓库；前端必须通过版本化公共 C ABI 和
CBOR 信封与 runtime 交互，不得复制 Rust runtime 逻辑、读取其内部对象或建立私有接口。

`rustyera-core.rev` 是构建和发布使用的唯一 core revision。更新它时必须同步验证动态库
C ABI、协议版本和完整 TUI 测试。兄弟目录 `../eraTW`、core 构建产物和参考实现均为本地
开发输入，不属于本仓库；除非任务明确要求，不得修改或提交它们。

## 仓库结构

- `src/rustyera_tui/`：应用源码。
  - `app.py`、`widgets.py`、`dialogs.py`、`app.tcss`：Textual 应用、控件、对话框与样式。
  - `abi.py`、`runtime.py`、`wire.py`、`protocol_text.py`：动态库加载、runtime 生命周期、
    CBOR 信封和协议文本处理。
  - `project.py`：项目扫描、源码解码、缓存及前端负责的文件 I/O。
  - `presentation.py`、`log_model.py`、`image_metadata.py`：规范化展示、日志与媒体元数据投影。
  - `diagnosis.py`：启动及运行故障诊断。
  - `testing.py`、`test_cli.py`：可复现的 runtime 场景测试和 NDJSON agent 接口。
- `tests/`：pytest 单元、组件和回归测试；文件通常与源码职责对应。
- `tools/runtime-tester/`：真实 C ABI 的固定流程、长流程测试工具和场景数据；其子目录规则
  以该目录的 `AGENTS.md` 为准。
- `entry.py`、`rustyera_tui.spec`：PyInstaller 入口和打包配置。
- `pyproject.toml`、`uv.lock`：Python 版本、依赖、pytest 与 Ruff 的权威配置。
- `rustyera-core.rev`：所绑定的 core 完整 Git revision。

## 蛇版兼容开发

- 使用 `codex/snake-compatibility` 分支及专用 worktree，`../rustyera-core` 必须是同组
  core worktree；开工核对分支和工作树，不修改原 master 工作区。位置、共享输入和
  构建/会话隔离要求遵循主工作区规范。
- 开工或续做前必须读取同组 core 的[改造思路](../rustyera-core/docs/snake-compatibility/SNAKE_EMUERA_MIGRATION_PLAN.md)
  和[分批次实施记录](../rustyera-core/docs/snake-compatibility/SNAKE_EMUERA_IMPLEMENTATION_LOG.md)，
  核对当前及上游批次依赖；实施前细化 TUI 方案，收尾/暂停时把实际改动、验收证据、
  commit、未完成项和恢复入口写回对应批次，范围或依赖变化同步更新改造思路。
- 原版 profile 为 `emuera.em`，蛇版为 `emuera.skia.snake`。TUI 仅负责文件摄取、输入、
  协议投影及能力协商，不复制 core 规则；图像、精确像素和音频能力须明确降级或拒绝，
  不返回伪造成功值。原版 eraTW 与蛇版 TW、两种 oracle 的结果分别记录。
- 本地 C ABI 必须由同组 core 构建，并通过显式 `--runtime-library` 或本 worktree
  专属链接加载；记录实际库路径、core SHA 和协议版本，不复用原工作区的库或虚拟环境。
  优先使用 `scripts/build_runtime_local.py` 保持 core 锁文件；共享同组 core 锁文件与
  构建目录的操作须串行或隔离，不能用不同前端目录掩盖共享输入。
- 本地联调不自动更新 `rustyera-core.rev`；确需绑定新的已提交 core SHA 时，按本仓库
  既有规则同步依赖并验证 C ABI/协议及相关流程。静态门禁和上游 core 门禁通过后，才可
  运行真实 `RuntimeWorker`/C ABI 的动态验证；不以 Textual mock 替代真实链路。

## 实现规范

- 使用 `pyproject.toml` 指定的 Python 版本、依赖和工具配置；依赖通过 `uv` 管理，非必要
  不手工改动 `uv.lock`。
- 保持职责边界：文件和终端 I/O 属于前端，游戏状态和规范化展示状态由 runtime 持有；
  UI 不得根据显示文本反推或暗中修改脚本状态。
- C ABI 输入输出均视为不可信边界。检查 ABI/协议版本、返回状态、长度和消息类型，确保
  native handle 在所有正常及异常路径上只释放一次。
- 协议字段、interaction token 和 runtime 产生的标识必须保持不透明；不要依赖未公开的
  编码细节。协议变更应同步更新编解码、调用方、诊断和测试。
- 阻塞的动态库调用、项目扫描、哈希或缓存工作不得阻塞 Textual 事件循环；跨线程结果只
  通过安全的消息或 worker 边界回到 UI。
- 项目源码按既定解码和路径规则提交给 runtime。所有写入必须限制在项目对应的 storage
  namespace，避免绝对路径泄漏、目录穿越和覆盖源文件。
- 保持展示投影的确定性和终端无关性；测试不得依赖本机终端尺寸、主题、区域设置、随机
  哈希顺序或真实用户目录。
- 实现思路、兼容性原因和非显然算法使用英文注释。优先小而清晰的模块，避免无关重构、
  批量格式化和跨层耦合。
- 不提交虚拟环境、`__pycache__`、构建产物、存档、游戏资源、动态库、trace、日志或缓存。

## 测试要求

用户提出多个开发/修改/修复点时，按工作区根 `AGENTS.md` 先评估规模：小项目合并实现、
重构和测试，共享一个批次；大项目各自独立实现、重构、测试及预算。无论是否合并执行，
每个功能点必须分开提交；跨组件分别提交，共用基础改动单独记录依赖。以下审查次数、
全量次数、静态门禁与 60 分钟预算均以当前批次为范围。循环迭代任务中，此范围进一步限定
为当前批次的当前轮次：各轮独立审查和计时，不能把单轮预算当成整个任务时限。未达用户
目标且未到用户时限时应继续迭代；用户要求暂停或确有阻塞时按根规则处理。有时限须预留
收尾并在截止前完成验证、分项提交或撤回本轮未完成改动，不留半成品。暂停时保留测试流程、
脚本、fixture 和必要临时材料及恢复记录，仅在用户明确表示任务完成或中止时才允许清理。

遵循工作区根 `AGENTS.md` 的并行与依赖调度规则：互不干扰的分析、开发、重构、测试尽可能
并行，依赖步骤流水线推进；共享可变资源须隔离。测试命令列表表达门禁依赖，不要求将无依赖
的检查全部串行；重构完成、最小回归通过、静态门禁通过等前置条件仍必须满足。

每个开发任务都必须包含与行为改动对应的最小测试，不能只以应用可启动或代码可导入作为
完成标准。修复 bug 时添加能稳定复现问题的回归用例；测试应使用最小 fixture，并清楚
断言可观察行为和错误路径。

同一套全量测试每个批次只能启动一次；发现问题并修复后，只重跑直接受影响的最小
测试集，不得重跑全量。TUI 端到端和长流程测试默认在稳定输入等待点记录全部可观察界面
元素与 runtime 状态，不要求周期快照，也不以相邻观察相同判定卡死；只有用户明确声明时
才启用周期快照要求。每个批次的测试流程从本批次首条测试命令开始共享 60 分钟墙钟预算；
该批次/轮次测试超时立即停止其测试进程，并报告命令、用例/阶段、最后观察状态、已用时间
及未验证项。

## 重构审查要求

涉及功能开发或修改、问题修复，或当前批次新增与改动的代码合计超过 100 行时，在最终
测试验收前必须委派独立的子智能体使用 `$refactor-rustyera-code` skill 审查当前批次涉及
的全部代码文件，尤其是新增和修改的部分。该子智能体须报告是否有重构必要；如有，须
提供可执行的重构方案。审查认为有必要重构时，必须先按该方案完成重构，再进行最终测试
验收；不得以时间、预算或“改动已能工作”为由跳过。最终交付必须说明审查结论，以及在
需要时已落实的方案。

每个触发上述条件的批次，重构子智能体必须且只能运行一次。它必须在该批次任何测试启动前
完成对本批次全部代码的完整审查；主智能体必须在本批次首条测试命令前解决其提出的所有重构
要求。该批次测试开始后不得为其新建、恢复、追加轮次或再次启动重构子智能体；也不得以二次
审查替代主智能体对首次审查要求的完整落实。

所有验证必须使用仓库 skill `$test-rustyera-tui`（位于
`.agents/skills/test-rustyera-tui/`）。该 skill 是测试命令顺序、真实 C ABI 场景、agent
驱动流程、Emuera 差分和结果报告的权威规范；编写或修改场景前还必须读取其
`references/test-cli.md`。不得绕过 skill 自建 Python 输入状态机或直接调用 Rust runtime
内部接口。

每条测试命令必须委派给运行 **gpt-5.6-terra low** 的子智能体。该子智能体只能执行测试
并返回各命令、退出码和相关输出，不得编辑、格式化或提交代码、fixture、文档及配置；
测试生成文件只能写入临时目录或已忽略目录。实现、格式化、测试编写、失败诊断和修复仍
由主智能体负责，不得用主智能体亲自运行测试替代测试子智能体。相关测试开始后若实现、
测试、fixture、依赖或构建输入发生变化，必须立即告知测试子智能体，要求其按需重建并
重跑所有受影响检查；旧结果一律作废。

- 先运行与改动直接相关的最小 pytest，通过后运行一次完整 TUI pytest；Ruff 与不依赖它的
  pytest 可并行，全部适用静态检查通过后才能启动动态测试：

  ```sh
  UV_CACHE_DIR=/tmp/rustyera-uv-cache uv run pytest
  UV_CACHE_DIR=/tmp/rustyera-uv-cache uv run ruff check src tests tools/runtime-tester
  ```

  完整 pytest 如果失败，修复后仅使用节点 ID、测试文件或最小标记集重跑受影响用例，
  不得再次执行完整 pytest。

- Textual UI 改动应覆盖相关 pilot/组件交互、焦点或事件路径，避免仅断言内部属性。
- ABI、wire、runtime 生命周期、存储或游戏流程改动应覆盖成功、错误、取消/关闭和资源
  释放路径，并通过真实 `RuntimeWorker` 与 C ABI 运行最小相关的已提交场景：

  ```sh
  uv run rustyera-test run --scenario SCENARIO --runtime-library LIBRARY
  ```

- 固定流程使用 `run`；需要 agent 交互探索时使用持久 `serve` 会话，逐条解析 NDJSON
  observation 后再提交一个命令。只选择可见、有效的输入，不得绕过输入校验或臆造隐藏状态。
- 优先复用 `tools/runtime-tester/scenarios/` 中已提交的场景。仅为可复用行为新增场景；随机
  探索不指定 `seed`，但复现问题前必须把 trace 首事件中的有效 seed 固定下来。
- 需要 Emuera 比较时，把 reference command 作为一个 shell 参数传入；空响应、超时、提前
  退出、schema 不匹配或能力缺失均属于测试基础设施失败，不能标记为跳过。出现语义差异时
  在第一处停止并保留 trace，不得用场景未声明的规则隐藏输出差异。
- 传统存档可以跨实现比较；VM snapshot 默认只适用于 RustyEra。存档或 snapshot 自带 RNG
  状态，恢复后不得重新播种。
- 项目扫描、缓存和源码解码改动应覆盖路径边界、内容变化、失效缓存及非法输入。
- 修改 `rustyera-core.rev`、打包入口或发布配置时，除完整 pytest 和 Ruff 外，还应执行
  对应平台的 PyInstaller/动态库加载冒烟测试。若修改了 Emuera reference CLI，必须转到
  core 仓库使用 `$test-rustyera-core` 完成其 Rust 门禁、平台冒烟和同输入差分验证。
- 只运行部分测试时，交付说明必须列出选择依据及未运行项目；不得把因环境缺失而跳过的
  测试描述为通过。场景测试还必须报告命令、退出码、有效 seed、trace 路径、已完成断言、
  第一处差异，以及全部受阻或未验证检查；预算耗尽默认属于失败。

## 工作区与 Git 安全

- 开始和结束任务时检查仓库状态；保留用户已有修改，不覆盖、回滚或格式化无关文件。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令。
- 完成实现与验证后运行 `git diff --check`，并检查最终 diff 中没有密钥、本机绝对路径或
  本地数据。
- 每次开发任务完成后，必须为本次改动生成 commit message，包含简洁的标题和说明动机、
  主要改动及测试结果的正文；随后仅暂存本任务涉及的文件并创建 commit。不得暂存或提交
  用户的无关修改。

## 任务交付

最终说明应简要列出：实现的行为、测试增改、实际执行及结果、未验证内容或已知限制、
已提交的 commit 及其 commit message（标题和正文）。若任务涉及 core revision、
C ABI 或协议变化，应同时说明双方版本关系及兼容性影响。
