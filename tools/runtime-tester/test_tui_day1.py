from rustyera_tui.runtime import FrontendEvent, LogLevel, LogMessage, PresentationBatch

from tui_day1 import apply_presentation_event, frontend_event_line


def test_frontend_event_line_exposes_status_logs_and_worker_stop() -> None:
    assert frontend_event_line(FrontendEvent("status", "loading")) == "STATUS: loading"
    assert (
        frontend_event_line(
            FrontendEvent("log", LogMessage(LogLevel.ERROR, "storage failed", True))
        )
        == "LOG: storage failed"
    )
    assert frontend_event_line(FrontendEvent("worker_stopped")) == (
        "WORKER_STOPPED before the day-one milestone"
    )
    assert frontend_event_line(FrontendEvent("wait", {0: 1})) is None


class FakePresentationModel:
    def __init__(self) -> None:
        self.snapshots: list[dict[int, object]] = []
        self.deltas: list[dict[int, object]] = []

    def apply_snapshot(self, snapshot: dict[int, object]) -> None:
        self.snapshots.append(snapshot)

    def apply_delta(self, delta: dict[int, object]) -> None:
        self.deltas.append(delta)


def test_apply_presentation_event_exposes_atomic_wait_after_updates() -> None:
    model = FakePresentationModel()
    wait = {0: 7, 1: 1}
    result = apply_presentation_event(
        model,  # type: ignore[arg-type]
        FrontendEvent(
            "presentation_batch",
            PresentationBatch({0: "snapshot"}, {0: "delta"}, wait, True),
        ),
    )

    assert result == (wait, 2)
    assert model.snapshots == [{0: "snapshot"}]
    assert model.deltas == [{0: "delta"}]
    assert apply_presentation_event(model, FrontendEvent("status", "ready")) is None  # type: ignore[arg-type]
