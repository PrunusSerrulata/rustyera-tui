"""Human-readable names for enum values carried by the public wire protocols."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


RUNTIME_PHASES = {
    0: "Negotiating（协议协商）",
    1: "LoadingProject（加载项目）",
    2: "Ready（就绪）",
    3: "Starting（启动中）",
    4: "Running（运行中）",
    5: "WaitingInput（等待输入）",
    6: "WaitingExternal（等待外部操作）",
    7: "DebugPaused（调试暂停）",
    8: "Reloading（重新加载）",
    9: "Stopping（停止中）",
    10: "Stopped（已停止）",
    11: "Faulted（故障）",
    12: "AnalyzingProject（分析项目）",
}

SNAPSHOT_INELIGIBLE_REASONS = {
    0: "StableWaitRequired（需要稳定输入等待）",
    1: "ExternalOperationPending（仍有外部操作待完成）",
    2: "VmSnapshotUnavailable（VM 不支持快照）",
    3: "SnapshotStateUnavailable（当前快照状态不可用）",
}

DEBUG_STOP_REASONS = {
    0: "PauseRequested（收到暂停请求）",
    1: "Breakpoint（命中断点）",
    2: "StepCompleted（单步完成）",
    3: "HostWait（等待 Host）",
    4: "FiberCompleted（Fiber 已完成）",
    5: "Fault（故障）",
    6: "Reload（重新加载）",
}

FAULT_CODES = {
    0: "InvalidState（状态无效）",
    1: "InvalidMessage（消息无效）",
    2: "ProjectLoad（项目加载失败）",
    3: "VmFault（VM 故障）",
    4: "ServiceFailure（服务失败）",
    5: "ResourceLimit（资源限制）",
    6: "Internal（内部故障）",
    7: "UnsupportedRuntimeFeature（Runtime 功能不受支持）",
}

COMMAND_ERROR_CODES = {
    0: "InvalidState（状态无效）",
    1: "InvalidValue（值无效）",
    2: "StaleRequest（请求已过期）",
    3: "VersionMismatch（版本不匹配）",
    4: "PermissionDenied（权限不足）",
    5: "FeatureUnavailable（功能不可用）",
    6: "ResourceLimit（资源限制）",
}

DIAGNOSTIC_SEVERITIES = {
    0: "Information（信息）",
    1: "Warning（警告）",
    2: "Error（错误）",
}

SERVICE_KINDS = {
    0: "FontMetrics（字体度量）",
    1: "Image（图像）",
    2: "Canvas（画布）",
    3: "Audio（音频）",
    4: "Network（网络）",
    5: "OpenUrl（打开链接）",
    6: "Extension（扩展）",
    7: "InputState（输入状态）",
    8: "Clock（时钟）",
    9: "Entropy（熵源）",
    10: "PresentationQuery（展示查询）",
}

ERA_STATUSES = {
    0: "Ok（成功）",
    1: "Empty（无可用数据）",
    2: "Busy（正忙）",
    3: "InvalidArgument（参数无效）",
    4: "AbiMismatch（ABI 不匹配）",
    5: "InvalidHandle（句柄无效）",
    6: "ResourceLimit（资源限制）",
    7: "InternalError（内部错误）",
}


def enum_text(value: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Return a stable textual enum name while keeping unknown future values readable."""

    if isinstance(value, int):
        return names.get(value, f"Unknown{enum_name}（未知值 {value}）")
    return f"Invalid{enum_name}（无效值 {value!r}）"


def enum_list_text(values: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Format a wire list of index-only enum values without leaking numeric-only output."""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return enum_text(values, names, enum_name)
    return "、".join(enum_text(value, names, enum_name) for value in values)


def variant_enum_text(value: Any, names: Mapping[int, str], enum_name: str) -> str:
    """Format minicbor's ``[tag, fields]`` enum representation."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and value:
        return enum_text(value[0], names, enum_name)
    return enum_text(value, names, enum_name)
