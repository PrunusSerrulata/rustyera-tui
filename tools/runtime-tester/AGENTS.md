# TUI runtime test scenarios

本目录只保存由 `rustyera-test` 消费的版本化测试场景。驱动实现属于
`rustyera_tui.testing`，测试属于仓库顶层 `tests/`，不得在本目录重新加入独立 Python
状态机。

- 场景只能通过正式 `RuntimeWorker`、C ABI、展示投影、存储和 debug protocol 驱动游戏。
- `project` 和状态文件路径相对场景文件解析；不得提交本机绝对路径。
- eraTW、参考仓库、存档和 snapshot 均为只读输入，不得复制进本目录。
- trace、缓存、存档、snapshot 和 oracle 输出写入 `.rustyera/` 或显式临时目录，不得提交。
- 未设置 `seed` 时驱动生成随机 seed，并将有效值写入 trace；需要复现时在场景中显式填写。
- 新增游戏专用分支应表达为输入条件、目标或 checkpoint 策略，不得修改通用驱动解释游戏数据。
- 同一套全量场景每批次最多运行一次，修复后只重跑受影响场景。每个端到端场景必须每 5 秒
  输出全部可观察界面元素和 runtime 状态；若连续两次内容相同，立即按卡死失败退出。
  当前批次（按工作区根规则合并小项目、独立大项目）的所有测试共享 60 分钟墙钟预算，到期停止并报告具体阻塞点。
