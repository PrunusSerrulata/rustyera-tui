"""Private RuntimeClient responsibilities extracted from the compatibility facade."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    DEBUG_VERSION,
    FrontendEvent,
    _debug_action_owner,
    unwrap_variant,
    variant,
    version_range,
)


class _RuntimeDebugMixin:
    def enable_debug(self) -> None:
        if self.debug_grant is not None:
            self.events.put(FrontendEvent("debug_enabled", True))
            return
        if not self.debug_requested:
            self.debug_requested = True
            self.send_debug(0, {0: version_range(*DEBUG_VERSION), 1: list(range(10))})

    def disable_debug(self) -> None:
        self.pending_debug_actions.clear()
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
            self.pending_debug_actions.append((action, value))
            self.enable_debug()
            return
        if self.stop_token is None:
            self.pending_debug_actions.append((action, value))
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
        self.send_debug(10, {0: self.debug_grant[1], 1: command}, pending=pending)

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
            self.events.put(FrontendEvent("debug_enabled", False))
        elif tag == 11:
            response_tag, fields = unwrap_variant(value)
            pending = self.debug_pending_by_message.pop(correlation_id or 0, "")
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
            pending = self.debug_pending_by_message.pop(correlation_id or 0, "")
            if pending == "step":
                self.debug_step_in_flight = False
            if self.debug_disable_pending:
                self._revoke_debug()
            self.events.put(FrontendEvent("runtime_error", f"调试请求失败：{value.get(1, '')}"))
