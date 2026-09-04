"""Private RuntimeClient transport and pump responsibilities."""

from __future__ import annotations

from .client_hello import build_client_hello

from .runtime_dependencies import (
    Any,
    CHANNEL_DEBUG,
    CHANNEL_RUNTIME,
    DEBUG_VERSION,
    DEBUG_LIFECYCLE_PENDING,
    FrontendEvent,
    ProjectBundle,
    RUNTIME_VERSION,
    StorageBackend,
    debug_message,
    decode_envelope,
    encode_envelope,
    message_value,
    runtime_message,
    time,
)


class _RuntimeTransportMixin:
    """Own the wire envelopes and the atomic poll/acknowledgement pump boundary."""

    _EPOCH_TRANSITION_TAGS = frozenset({20, 23})
    _EPOCH_TRANSITION_REPLY_TAGS = frozenset({51, 53})

    def _submit_encoded_unmeasured(
        self, data: bytes, channel: str, payload_tag: int
    ) -> None:
        self.abi.submit(data)

    def _submit_encoded_measured(
        self, data: bytes, channel: str, payload_tag: int
    ) -> None:
        probe = self.performance_probe
        span = probe.start("runtime", "c_abi", "submit")
        self.abi.submit(data)
        probe.finish(
            span,
            fields={
                "envelopeCount": 1,
                "envelopeBytes": len(data),
                "channel": channel,
                "payloadTag": payload_tag,
            },
        )

    # Replaced once at client construction when the opt-in probe is enabled. This keeps
    # the production outbound hot path free of a per-envelope probe lookup and branch.
    _submit_encoded = _submit_encoded_unmeasured

    def _send_hello(self) -> None:
        self.send_runtime(0, build_client_hello(self.pending_bundle))

    @staticmethod
    def _storage_for_bundle(bundle: ProjectBundle) -> StorageBackend:
        if bundle.project_file is None:
            return StorageBackend(
                bundle.root,
                compatibility_profile=bundle.compatibility_profile,
                resource_bundle=bundle,
            )
        return StorageBackend(
            bundle.root,
            data_root=bundle.root / ".rustyera" / "packaged-projects",
            identity_path=bundle.project_file,
            compatibility_profile=bundle.compatibility_profile,
            resource_bundle=bundle,
        )

    def send_runtime(
        self, tag: int, value: Any | None = None, *, correlation_id: int | None = None
    ) -> int:
        message_id = self.next_message_id
        self.next_message_id += 1
        if self._runtime_output_batch_active or self._runtime_epoch_transition is not None:
            self._deferred_runtime_messages.append((message_id, tag, value, correlation_id))
            return message_id
        self._submit_runtime(message_id, tag, value, correlation_id)
        return message_id

    def _submit_runtime(
        self,
        message_id: int,
        tag: int,
        value: Any | None,
        correlation_id: int | None,
    ) -> None:
        data = encode_envelope(
            channel=CHANNEL_RUNTIME,
            channel_version=RUNTIME_VERSION,
            session=self.session,
            sequence=self.runtime_sequence,
            message_id=message_id,
            correlation_id=correlation_id,
            payload_tag=tag,
            payload=runtime_message(tag, value),
            epoch=self.epoch,
        )
        self.runtime_sequence += 1
        self._submit_encoded(data, "runtime", tag)
        if tag in self._EPOCH_TRANSITION_TAGS:
            self._runtime_epoch_transition = (message_id, self.epoch)

    def _flush_deferred_runtime_messages(self) -> None:
        messages = self._deferred_runtime_messages
        self._deferred_runtime_messages = []
        for message_id, tag, value, correlation_id in messages:
            if (
                self._runtime_epoch_transition is not None
                and tag not in self._EPOCH_TRANSITION_REPLY_TAGS
            ):
                self._deferred_runtime_messages.append((message_id, tag, value, correlation_id))
                continue
            self._submit_runtime(message_id, tag, value, correlation_id)

    def send_debug(self, tag: int, value: Any | None = None, *, pending: str = "") -> int:
        if self.session is None or self.epoch is None:
            raise RuntimeError("debug protocol requires an active runtime session")
        if (
            pending
            and pending not in DEBUG_LIFECYCLE_PENDING
            and sum(
                name not in DEBUG_LIFECYCLE_PENDING
                for name in self.debug_pending_by_message.values()
            )
            >= 256
        ):
            raise RuntimeError("debug request correlation budget is exhausted")
        message_id = self.next_message_id
        self.next_message_id += 1
        data = encode_envelope(
            channel=CHANNEL_DEBUG,
            channel_version=DEBUG_VERSION,
            session=self.session,
            sequence=self.debug_sequence,
            message_id=message_id,
            correlation_id=None,
            payload_tag=tag,
            payload=debug_message(tag, value),
            epoch=self.epoch,
        )
        self.debug_sequence += 1
        self._submit_encoded(data, "debug", tag)
        if pending:
            self.debug_pending_by_message[message_id] = pending
        return message_id

    def _handle_envelope(self, data: bytes) -> int | None:
        envelope = decode_envelope(data)
        transition = self._runtime_epoch_transition
        if transition is not None and (
            (envelope.epoch is not None and envelope.epoch != transition[1])
            or (envelope.payload_tag == 95 and envelope.correlation_id == transition[0])
        ):
            self._runtime_epoch_transition = None
        # A committed new game, restore, or hot reload may advance the epoch before its first
        # StateChanged message is observed. The common envelope already carries that authority;
        # adopt it before acknowledging the message so the acknowledgement cannot be stale.
        if envelope.epoch is not None:
            if self.epoch is not None and envelope.epoch != self.epoch:
                self._pending_sql_requests.clear()
                self.sql_provider.begin_epoch(envelope.epoch)
            self.epoch = envelope.epoch
            if self.storage is not None:
                self.storage.begin_epoch(self.epoch)
        if envelope.channel == CHANNEL_RUNTIME:
            if envelope.sequence != self.expected_runtime_output:
                raise RuntimeError(
                    f"runtime output sequence gap: expected {self.expected_runtime_output}, "
                    f"received {envelope.sequence}"
                )
            self.expected_runtime_output += 1
            value = message_value(envelope.payload, envelope.payload_tag)
            if self.audit_capture is not None:
                self.audit_capture.observe_runtime(envelope.payload_tag, value)
            self._handle_runtime(envelope.payload_tag, value, envelope.correlation_id)
            return envelope.sequence
        if envelope.channel == CHANNEL_DEBUG:
            if envelope.sequence != self.expected_debug_output:
                raise RuntimeError(
                    f"debug output sequence gap: expected {self.expected_debug_output}, "
                    f"received {envelope.sequence}"
                )
            self.expected_debug_output += 1
            value = message_value(envelope.payload, envelope.payload_tag)
            self._handle_debug(envelope.payload_tag, value, envelope.correlation_id)
        else:
            raise RuntimeError(f"unknown output channel {envelope.channel}")
        return None

    def pump(self) -> bool:
        probe = self.performance_probe
        measure = probe.enabled or self.metrics_threshold_ms is not None
        pump_started_ns = time.monotonic_ns() if measure else 0
        self._wait_event_dirty = False
        self._presentation_boundary_dirty = False
        # Sample automatic time only when the next drive is about to start. The worker drains
        # queued user commands before calling pump(), so a user action for the visible wait is
        # submitted before this timer tick instead of racing a tick left queued by the prior
        # presentation batch.
        self._advance_deadline()
        drive_started_ns = time.monotonic_ns() if measure else 0
        report = self.abi.drive()
        drive_ns = time.monotonic_ns() - drive_started_ns if measure else 0
        emitted = False
        probe_enabled = probe.enabled
        if probe_enabled:
            envelope_count = 0
            envelope_bytes = 0
            poll_ns = 0
            decode_ns = 0
        acknowledge_through: int | None = None
        self._runtime_output_batch_active = True
        try:
            if probe_enabled:
                while True:
                    poll_started_ns = time.monotonic_ns()
                    data = self.abi.poll()
                    poll_ns += time.monotonic_ns() - poll_started_ns
                    if not data:
                        break
                    emitted = True
                    envelope_count += 1
                    envelope_bytes += len(data)
                    decode_started_ns = time.monotonic_ns()
                    runtime_sequence = self._handle_envelope(data)
                    decode_ns += time.monotonic_ns() - decode_started_ns
                    if runtime_sequence is not None:
                        acknowledge_through = runtime_sequence
            else:
                while data := self.abi.poll():
                    emitted = True
                    runtime_sequence = self._handle_envelope(data)
                    if runtime_sequence is not None:
                        acknowledge_through = runtime_sequence
        except Exception:
            self._deferred_runtime_messages.clear()
            self._pending_sql_requests.clear()
            self.sql_provider.reset()
            raise
        finally:
            self._runtime_output_batch_active = False
        # SQL requests are deliberately executed after the complete runtime batch has been
        # observed, so a CancelExternalRequest emitted later in that same batch wins before
        # APSW can create side effects. Queue their responses with the other batch replies.
        self._runtime_output_batch_active = True
        try:
            self._flush_pending_sql_requests()
        finally:
            self._runtime_output_batch_active = False
        self._flush_presentation_events()
        # Runtime output acknowledgement is cumulative. Deferring it until the complete poll
        # batch also ensures an epoch-changing reload is acknowledged with its final epoch,
        # even when an earlier message in the same batch was emitted before the commit.
        if acknowledge_through is not None and self.session is not None:
            self.send_runtime(93, {0: acknowledge_through})
        self._flush_deferred_runtime_messages()
        pump_ns = time.monotonic_ns() - pump_started_ns if measure else 0
        drive_ms = drive_ns / 1e6
        pump_ms = pump_ns / 1e6
        if probe_enabled:
            common = {
                "vmInstructions": report.vm_instructions,
                "runtimeTransitions": report.runtime_transitions,
                "queuedEnvelopes": report.queued_envelopes,
                "runtimeState": report.state,
                "envelopeCount": envelope_count,
                "envelopeBytes": envelope_bytes,
            }
            probe.record(
                "sample",
                phase="runtime",
                stage="c_abi",
                operation="drive",
                duration_ns=drive_ns,
                fields=common,
            )
            probe.record(
                "sample",
                phase="runtime",
                stage="c_abi",
                operation="poll",
                duration_ns=poll_ns,
                fields={"envelopeCount": envelope_count, "envelopeBytes": envelope_bytes},
            )
            probe.record(
                "sample",
                phase="runtime",
                stage="protocol",
                operation="decode",
                duration_ns=decode_ns,
                fields={"envelopeCount": envelope_count, "envelopeBytes": envelope_bytes},
            )
            probe.record(
                "sample",
                phase="runtime",
                stage="pump",
                operation="complete",
                duration_ns=pump_ns,
                fields=common,
            )
        if (
            self.metrics_threshold_ms is not None
            and max(drive_ms, pump_ms) >= self.metrics_threshold_ms
        ):
            self.events.put(
                FrontendEvent(
                    "runtime_metrics",
                    {
                        "drive_ms": drive_ms,
                        "pump_ms": pump_ms,
                        "vm_instructions": report.vm_instructions,
                        "runtime_transitions": report.runtime_transitions,
                        "queued_envelopes": report.queued_envelopes,
                        "state": report.state,
                    },
                )
            )
        return emitted or report.state in (1, 2)
