"""Private RuntimeClient responsibilities extracted from the compatibility facade."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    DEBUG_LIFECYCLE_PENDING,
    DEBUG_VERSION,
    FrontendEvent,
    LogLevel,
    _debug_action_owner,
    log_event,
    unwrap_variant,
    variant,
    version_range,
)
from .text_budget import retained_text_utf8_length

MAX_PENDING_DEBUG_ACTIONS = 256
MAX_PENDING_DEBUG_ACTION_BYTES = 1024 * 1024
MAX_PENDING_DEBUG_CORRELATIONS = 256
MAX_PENDING_DEBUG_CONSOLE_REQUESTS = 32
MAX_PENDING_DEBUG_CONSOLE_BYTES = 1024 * 1024
_DEBUG_REFRESH_PENDING = frozenset(
    {"variables", "variable_value", "fibers", "call_stack"}
)


class _RuntimeDebugMixin:
    def _queue_debug_action(self, action: str, value: Any) -> None:
        cost = retained_text_utf8_length(value, stop_after=MAX_PENDING_DEBUG_ACTION_BYTES)
        if cost > MAX_PENDING_DEBUG_ACTION_BYTES:
            self._report_debug_backpressure("调试操作超过单次内存预算")
            return
        if not action.startswith("console_"):
            for index in range(len(self.pending_debug_actions) - 1, -1, -1):
                if self.pending_debug_actions[index][0] == action:
                    self.pending_debug_actions.pop(index)
                    break
        if action.startswith("console_"):
            console_actions = [
                item for item in self.pending_debug_actions if item[0].startswith("console_")
            ]
            console_bytes = sum(
                retained_text_utf8_length(item[1]) for item in console_actions
            )
            if (
                len(console_actions) >= MAX_PENDING_DEBUG_CONSOLE_REQUESTS
                or console_bytes + cost > MAX_PENDING_DEBUG_CONSOLE_BYTES
            ):
                self._report_debug_backpressure("调试控制台请求队列已达到内存预算")
                return
        retained_bytes = sum(
            retained_text_utf8_length(item[1]) for item in self.pending_debug_actions
        )
        while self.pending_debug_actions and (
            len(self.pending_debug_actions) >= MAX_PENDING_DEBUG_ACTIONS
            or retained_bytes + cost > MAX_PENDING_DEBUG_ACTION_BYTES
        ):
            removable = next(
                (
                    index
                    for index, item in enumerate(self.pending_debug_actions)
                    if item[0] != "pause_only"
                ),
                None,
            )
            if removable is None:
                self._report_debug_backpressure("调试操作队列已达到内存预算")
                return
            _, removed_value = self.pending_debug_actions.pop(removable)
            retained_bytes -= retained_text_utf8_length(removed_value)
            self._report_debug_backpressure("调试操作队列已合并或丢弃旧的刷新请求")
        self.pending_debug_actions.append((action, value))

    def _report_debug_backpressure(self, message: str) -> None:
        if message in self.debug_backpressure_warnings:
            return
        self.debug_backpressure_warnings.add(message)
        self.events.put(log_event(message, LogLevel.WARNING))

    def enable_debug(self) -> None:
        if self.debug_grant is not None:
            self.events.put(FrontendEvent("debug_enabled", True))
            return
        if not self.debug_requested:
            self.debug_requested = True
            self.send_debug(0, {0: version_range(*DEBUG_VERSION), 1: list(range(10))})

    def disable_debug(self) -> None:
        self.pending_debug_actions.clear()
        self.deferred_debug_refresh.clear()
        self.deferred_debug_console.clear()
        self.debug_backpressure_warnings.clear()
        self.single_step_enabled = False
        self.transient_pause_owner = None
        self.transient_close_pending = None
        self.debug_disable_pending = True
        if self.debug_step_in_flight:
            return
        if self.stop_token is not None:
            self._debug_request(variant(1, self.stop_token), "disable_continue")
        else:
            self._revoke_debug()

    def _revoke_debug(self) -> None:
        if self.debug_grant is not None:
            self.send_debug(
                2,
                {
                    0: self.debug_grant[1][0],
                    1: "frontend disabled debugging",
                },
            )
        self.debug_requested = False
        self.debug_grant = None
        self.stop_token = None
        self.selected_fiber = None
        self.debug_pending_by_message.clear()
        self.debug_pending_cost_by_message.clear()
        self.deferred_debug_refresh.clear()
        self.deferred_debug_console.clear()
        self.debug_backpressure_warnings.clear()
        self.debug_step_in_flight = False
        self.debug_disable_pending = False
        self.events.put(FrontendEvent("debug_enabled", False))

    def set_single_step(self, enabled: bool) -> None:
        self.single_step_enabled = enabled
        if enabled:
            if self.phase == 4 and self.stop_token is None:
                self.request_debug_action("pause_only")
        elif self.stop_token is not None:
            self._debug_request(variant(1, self.stop_token), "continue")

    def request_debug_action(self, action: str, value: Any = None) -> None:
        if self.debug_grant is None:
            self._queue_debug_action(action, value)
            self.enable_debug()
            return
        if self.stop_token is None:
            self._queue_debug_action(action, value)
            owner = _debug_action_owner(action)
            if owner is not None and self.transient_pause_owner is None:
                self.transient_pause_owner = owner
            self._debug_request(variant(0), "pause")
            return
        self._run_debug_action(action, value)

    def close_debug_surface(self, owner: str) -> None:
        if self.transient_pause_owner != owner:
            return
        self.transient_close_pending = owner
        self._resume_transient_pause_if_ready()

    def _resume_transient_pause_if_ready(self) -> None:
        if (
            self.stop_token is None
            or self.transient_pause_owner is None
            or self.transient_close_pending != self.transient_pause_owner
            or any(
                pending not in {"continue", "transient_continue"}
                for pending in self.debug_pending_by_message.values()
            )
            or self.deferred_debug_refresh
            or self.deferred_debug_console
        ):
            return
        self._debug_request(variant(1, self.stop_token), "transient_continue")

    def _run_debug_action(self, action: str, value: Any) -> None:
        if self.stop_token is None:
            return
        if action == "variables":
            self._debug_request(variant(10, self.stop_token, None, 500), "variables")
        elif action == "read_variable":
            descriptor, indices = value if isinstance(value, tuple) else (value, None)
            storage = descriptor[2]
            if storage == 3:
                self.events.put(
                    FrontendEvent("error", "局部变量读取需要先在栈查看器中选择具体 frame。")
                )
                return
            reference = {
                0: descriptor[0],
                1: storage,
                4: self.stop_token[2],
                6: list(indices) if indices is not None else [0 for _ in descriptor.get(4, [])],
            }
            if storage == 2:
                reference[5] = 0
            self._debug_request(variant(11, self.stop_token, reference), "variable_value")
        elif action == "fibers":
            self._debug_request(variant(30, self.stop_token, value, 1024), "fibers")
        elif action == "call_stack":
            self._debug_request(variant(31, self.stop_token, int(value)), "call_stack")
        elif action == "console_evaluate":
            self._debug_request(variant(40, self.stop_token, variant(0, str(value))), "console")
        elif action == "console_execute":
            self._debug_request(variant(40, self.stop_token, variant(1, str(value))), "console")
        elif action == "pause_only":
            return

    def _debug_request(self, command: list[Any], pending: str) -> None:
        if self.debug_grant is None:
            return
        cost = retained_text_utf8_length(
            command, stop_after=MAX_PENDING_DEBUG_CONSOLE_BYTES
        )
        if (
            pending in DEBUG_LIFECYCLE_PENDING
            and pending in self.debug_pending_by_message.values()
        ):
            return
        if pending in _DEBUG_REFRESH_PENDING and (
            pending in self.debug_pending_by_message.values()
            or pending in self.deferred_debug_refresh
        ):
            self.deferred_debug_refresh[pending] = (command, cost)
            return
        if pending == "console" and not self._reserve_console_request(cost):
            return
        normal_pending = sum(
            name not in DEBUG_LIFECYCLE_PENDING
            for name in self.debug_pending_by_message.values()
        )
        if (
            pending not in DEBUG_LIFECYCLE_PENDING
            and normal_pending >= MAX_PENDING_DEBUG_CORRELATIONS
        ):
            if pending in _DEBUG_REFRESH_PENDING:
                self.deferred_debug_refresh[pending] = (command, cost)
            elif pending == "console":
                self.deferred_debug_console.append((command, cost))
            self._report_debug_backpressure("调试请求已在提交前受到背压")
            return
        self._submit_debug_request(command, pending, cost)

    def _reserve_console_request(self, cost: int) -> bool:
        in_flight = [
            message_id
            for message_id, name in self.debug_pending_by_message.items()
            if name == "console"
        ]
        count = len(in_flight) + len(self.deferred_debug_console)
        retained_bytes = sum(
            self.debug_pending_cost_by_message.get(message_id, 0)
            for message_id in in_flight
        ) + sum(item[1] for item in self.deferred_debug_console)
        if (
            count >= MAX_PENDING_DEBUG_CONSOLE_REQUESTS
            or retained_bytes + cost > MAX_PENDING_DEBUG_CONSOLE_BYTES
        ):
            self._report_debug_backpressure("调试控制台请求已达到数量或字节预算")
            return False
        return True

    def _submit_debug_request(self, command: list[Any], pending: str, cost: int) -> None:
        message_id = self.send_debug(
            10, {0: self.debug_grant[1], 1: command}, pending=pending
        )
        self.debug_pending_cost_by_message[message_id] = cost

    def _complete_debug_request(self, correlation_id: int | None) -> str:
        message_id = correlation_id or 0
        self.debug_pending_cost_by_message.pop(message_id, None)
        return self.debug_pending_by_message.pop(message_id, "")

    def _drain_deferred_debug_requests(self) -> None:
        if self.debug_grant is None or self.stop_token is None:
            return
        while self.deferred_debug_refresh:
            normal_pending = sum(
                name not in DEBUG_LIFECYCLE_PENDING
                for name in self.debug_pending_by_message.values()
            )
            if normal_pending >= MAX_PENDING_DEBUG_CORRELATIONS:
                return
            pending, (command, cost) = self.deferred_debug_refresh.popitem()
            self._submit_debug_request(command, pending, cost)
        while self.deferred_debug_console:
            normal_pending = sum(
                name not in DEBUG_LIFECYCLE_PENDING
                for name in self.debug_pending_by_message.values()
            )
            if normal_pending >= MAX_PENDING_DEBUG_CORRELATIONS:
                return
            command, cost = self.deferred_debug_console.pop(0)
            self._submit_debug_request(command, "console", cost)

    def debug_step(self) -> None:
        if self.stop_token is not None and self.selected_fiber is not None:
            self.debug_step_in_flight = True
            self._debug_request(variant(2, self.stop_token, self.selected_fiber or 0, 1), "step")

    def _handle_debug(self, tag: int, value: Any, correlation_id: int | None) -> None:
        if tag == 1:
            self.debug_grant = value
            self.events.put(FrontendEvent("debug_enabled", True))
            pending = list(self.pending_debug_actions)
            self.pending_debug_actions.clear()
            for action in pending:
                self.request_debug_action(*action)
        elif tag == 2:
            self.debug_grant = None
            self.stop_token = None
            self.selected_fiber = None
            self.debug_step_in_flight = False
            self.debug_disable_pending = False
            self.transient_pause_owner = None
            self.transient_close_pending = None
            self.debug_pending_by_message.clear()
            self.debug_pending_cost_by_message.clear()
            self.deferred_debug_refresh.clear()
            self.deferred_debug_console.clear()
            self.debug_backpressure_warnings.clear()
            self.events.put(FrontendEvent("debug_enabled", False))
        elif tag == 11:
            response_tag, fields = unwrap_variant(value)
            pending = self._complete_debug_request(correlation_id)
            if response_tag == 8 and fields:
                self.stop_token = fields[0].get(0, self.stop_token)
            self.events.put(FrontendEvent("debug_response", (pending, response_tag, fields)))
            if pending in {
                "continue",
                "disable_continue",
                "transient_continue",
                "auto_continue",
                "step",
            }:
                self.stop_token = None
            if pending == "disable_continue":
                self._revoke_debug()
                return
            if pending == "transient_continue":
                self.transient_pause_owner = None
                self.transient_close_pending = None
            self._resume_transient_pause_if_ready()
            self._drain_deferred_debug_requests()
        elif tag == 12:
            self.stop_token = value[0]
            self.selected_fiber = value.get(2)
            self.debug_step_in_flight = False
            if self.debug_disable_pending:
                self._debug_request(variant(1, self.stop_token), "disable_continue")
                return
            self._presentation_boundary_dirty = True
            self.events.put(FrontendEvent("debug_stopped", value))
            pending = list(self.pending_debug_actions)
            self.pending_debug_actions.clear()
            for action, argument in pending:
                self._run_debug_action(action, argument)
            if self.single_step_enabled and unwrap_variant(value[1])[0] == 3:
                self._debug_request(variant(1, self.stop_token), "auto_continue")
            self._resume_transient_pause_if_ready()
        elif tag == 13:
            pending = self._complete_debug_request(correlation_id)
            if pending == "step":
                self.debug_step_in_flight = False
            if self.debug_disable_pending:
                self._revoke_debug()
            self.events.put(FrontendEvent("runtime_error", f"调试请求失败：{value.get(1, '')}"))
            self._drain_deferred_debug_requests()
