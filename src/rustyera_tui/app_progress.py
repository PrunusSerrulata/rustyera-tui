"""Pure project-progress presentation values for the TUI shell."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

PROJECT_PROGRESS_PREFIXES = (
    "正在创建新的 Runtime session",
    "正在扫描 ",
    "正在载入项目缓存",
    "项目缓存未命中",
    "正在提交项目并编译脚本",
    "项目编译完成，正在进入标题画面",
    "项目缓存命中，正在进入标题画面",
    "项目缓存已保存，正在进入标题画面",
    "项目缓存保存失败，正在进入标题画面",
    "正在热重载",
)

PROJECT_PROGRESS_LABELS = (
    "正在读取项目文件",
    "正在整理项目文件",
    "正在加载项目数据",
    "正在解析脚本",
    "正在分析脚本",
    "正在编译脚本函数",
    "正在验证编译结果",
    "正在整理编译结果",
    "正在准备 Runtime 资源",
    "正在打包全量项目文件",
    "正在解析编译缓存",
    "正在解码编译缓存",
    "正在验证编译缓存",
)


@dataclass(frozen=True, slots=True)
class ProjectProgress:
    message: str
    blocks_interaction: bool
    updates_export_dialog: bool


def format_project_progress(
    stage: int,
    completed: int,
    total: int,
    *,
    project_file_exporting: bool,
    labels: Sequence[str] = PROJECT_PROGRESS_LABELS,
) -> ProjectProgress | None:
    if not 0 <= stage < len(labels):
        return None
    if stage == 0 and total <= 0:
        return ProjectProgress("正在枚举项目文件…", True, False)
    completed = max(0, min(completed, total)) if total > 0 else 0
    percent = min(100, completed * 100 // total) if total > 0 else 100
    filled = percent * 20 // 100
    bar = f"[{'█' * filled}{'░' * (20 - filled)}]"
    message = f"{labels[stage]}：{completed}/{total} {bar} {percent}%"
    return ProjectProgress(message, stage != 9 or project_file_exporting, True)
