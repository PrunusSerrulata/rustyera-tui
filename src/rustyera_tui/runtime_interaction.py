"""Runtime services and wait-bound frontend interaction."""

from __future__ import annotations

from .runtime_dependencies import (
    Any,
    FrontendEvent,
    LogLevel,
    PendingGameInput,
    SERVICE_KINDS,
    cell_len,
    copy,
    datetime,
    decode,
    decode_image_metadata,
    encode,
    enum_text,
    html_printed_str,
    is_message_skip_wait,
    is_message_wait,
    log_event,
    message_wait_intent,
    plain_line,
    secrets,
    time,
    unwrap_variant,
    variant,
)


class _RuntimeInteractionMixin:
    def _handle_wait_change(self, change: list[Any]) -> None:
        tag, fields = unwrap_variant(change)
        if tag in (0, 1):
            self._set_active_wait(fields[0])
        elif tag == 2 and self.active_wait and self.active_wait.get(0) == fields[0]:
            self._set_active_wait(None)
        self._wait_event_dirty = True

    def _set_active_wait(self, wait: dict[int, Any] | None) -> None:
        self.active_wait = wait

    def _acknowledge_effects(self, batch: dict[int, Any]) -> None:
        # The TUI cannot play device effects. Reporting Unsupported is semantically different
        # from silently claiming playback succeeded and lets scripts observe honest outcomes.
        outcomes = []
        for effect in batch.get(0, []):
            kind, _fields = unwrap_variant(effect[1])
            if kind == 4:
                self.events.put(FrontendEvent("open_configuration"))
                outcomes.append({0: effect[0], 1: 0})
            else:
                outcomes.append({0: effect[0], 1: 1, 2: "TUI does not provide this device effect"})
        self.send_runtime(43, {0: outcomes})

    def _handle_storage(self, request: dict[int, Any], correlation_id: int | None) -> None:
        if self.storage is None:
            result = {0: request[0], 1: variant(4, {0: 6, 1: "no active project storage"})}
        else:
            result = self.storage.handle(request)
        self.send_runtime(51, result, correlation_id=correlation_id)

    def _handle_service(self, request: dict[int, Any], correlation_id: int | None) -> None:
        request_id, kind, operation = request[0], request[1], request[2]
        try:
            if kind == 9 and operation == "random_seed":
                response = {0: secrets.randbits(64)}
            elif kind == 8 and operation == "local_date_time":
                now = datetime.now().astimezone()
                offset = now.utcoffset()
                response = {
                    0: now.year,
                    1: now.month,
                    2: now.day,
                    3: now.hour,
                    4: now.minute,
                    5: now.second,
                    6: now.microsecond // 1000,
                    7: int(offset.total_seconds() // 60) if offset else 0,
                }
            elif kind == 7 and operation == "get_key_state":
                response = {0: True, 1: False, 2: False}
            elif kind == 1 and operation == "image_metadata":
                query = decode(request[4])
                bundle = next(
                    (
                        candidate
                        for candidate in (self.reload_candidate, self.pending_bundle, self.bundle)
                        if candidate is not None
                    ),
                    None,
                )
                if bundle is None:
                    raise RuntimeError("image metadata requested without a project")
                response = decode_image_metadata(
                    bundle.resource_prefix(query[0], query[1], 1024 * 1024)
                )
            elif kind == 10 and operation == "get_display_line":
                query = decode(request[4])
                index = query[1]
                text = ""
                if 0 <= index < len(self.presentation.lines):
                    text = plain_line(self.presentation.lines[index])
                response = {0: query[0], 1: text}
            elif kind == 10 and operation == "html_get_printed_str":
                query = decode(request[4])
                response = {
                    0: query[0],
                    1: html_printed_str(self.presentation.lines, query[1]),
                }
            elif kind == 10 and operation == "serialize_physical_history":
                query = decode(request[4])
                body = "\n".join(plain_line(line) for line in self.presentation.lines)
                response = {0: query[0], 1: body if query[2] else f"{query[1]}\n\n{body}"}
            elif kind == 0 and operation == "gget_text_size":
                query = decode(request[4])
                response = {0: query[0], 1: cell_len(query[1]), 2: 1}
            else:
                service = enum_text(kind, SERVICE_KINDS, "ServiceKind")
                raise NotImplementedError(f"unsupported frontend service {service}/{operation}")
            result = variant(0, encode(response))
        except Exception as error:  # noqa: BLE001 - external-service boundary
            result = variant(1, {0: "frontend.unsupported_service", 1: str(error)})
        self.send_runtime(53, {0: request_id, 1: result}, correlation_id=correlation_id)

    def _advance_deadline(self) -> None:
        if (
            self.pending_diagnosis is not None
            or not self.active_wait
            or self.active_wait.get(8) is None
        ):
            return
        now = time.monotonic_ns()
        if now - self.last_time_advance_ns >= 50_000_000:
            self.last_time_advance_ns = now
            self.send_runtime(31, {0: now})

    def submit_text(self, text: str) -> None:
        if self.phase == 7:
            self.events.put(log_event("调试暂停解除前暂不提交游戏输入。", LogLevel.WARNING))
            return
        wait = self.active_wait
        if wait is None:
            self.events.put(log_event("当前没有可提交的输入等待。", LogLevel.WARNING))
            return
        kind = wait[1]
        if is_message_wait(wait):
            intent = message_wait_intent(wait, text or "\n")
        elif kind in (2, 3, 5, 6, 7):
            intent = variant(2, text)
        elif kind == 4:
            intent = variant(4)
        elif kind == 8:
            try:
                values = [int(part.strip()) for part in text.split(",")]
                values += [0] * (5 - len(values))
                primitive = {index: value for index, value in enumerate(values[:5])}
                intent = variant(6, primitive)
            except ValueError:
                self.events.put(
                    FrontendEvent(
                        "error", "原始输入需使用 type,result1,result2,result3,result4 格式。"
                    )
                )
                return
        else:
            intent = variant(2, text)
        self._submit_input(wait, intent)

    def activate(self, button_token: dict[int, int]) -> None:
        if self.phase == 7 or self.active_wait is None:
            return
        self._submit_input(self.active_wait, variant(3, button_token))

    def skip_message_waits(self) -> None:
        wait = self.active_wait
        if not is_message_skip_wait(wait):
            return
        intent = message_wait_intent(wait)
        self._submit_input(wait, intent, message_skip=True)

    def _submit_input(
        self,
        wait: dict[int, Any],
        intent: list[Any],
        *,
        message_skip: bool = False,
        stale_retries: int = 0,
    ) -> None:
        if self.pending_diagnosis is not None:
            return
        message_id = self.send_runtime(
            30,
            {
                0: wait[0],
                1: wait[11],
                2: time.monotonic_ns(),
                3: intent,
                4: message_skip,
            },
        )
        if len(self._input_messages) >= 256:
            self._input_messages.pop(next(iter(self._input_messages)))
        self._input_messages[message_id] = PendingGameInput(
            copy.deepcopy(wait), copy.deepcopy(intent), message_skip, stale_retries
        )
        if self.single_step_enabled and self.stop_token is None:
            self.request_debug_action("pause_only")

    def _retry_stale_input(self, request: PendingGameInput) -> bool:
        if self.pending_diagnosis is not None:
            return False
        wait = self.active_wait
        if (
            request.stale_retries != 0
            or wait is None
            or wait.get(1) != request.wait.get(1)
            or (wait.get(0), wait.get(11)) == (request.wait.get(0), request.wait.get(11))
        ):
            return False
        self._submit_input(
            wait,
            request.intent,
            message_skip=request.message_skip,
            stale_retries=1,
        )
        return True

    def input_undo(self, token: dict[int, Any] | None) -> None:
        if token is not None and self.pending_diagnosis is None:
            self.send_runtime(37, {0: token})
