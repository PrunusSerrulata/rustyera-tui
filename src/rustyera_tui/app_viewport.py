"""Viewport projection scheduling for the Textual app facade."""

from __future__ import annotations

from .widgets import GameViewport


class _ViewportProjectionMixin:
    def _schedule_viewport_projection(self) -> None:
        if not self._projection_refresh_scheduled:
            self._projection_refresh_scheduled = True
            self.call_after_refresh(self._send_viewport_projection)

    def _send_viewport_projection(self) -> None:
        self._projection_refresh_scheduled = False
        if not self.is_mounted:
            return
        viewport = self.query_one(GameViewport)
        self._send_projection(*viewport.content_dimensions)

    def _send_projection(self, width: int, height: int) -> None:
        # Each observation is bound to the currently applied presentation revision. The
        # runtime therefore treats this as a causal observation revision, even when only
        # presentation content (rather than terminal geometry) changed.
        self.environment_revision += 1
        self.worker.send(
            "projection",
            (
                max(1, width),
                max(1, height),
                self.environment_revision,
                self.presentation.revision,
            ),
        )
