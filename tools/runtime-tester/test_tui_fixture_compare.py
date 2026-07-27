from types import SimpleNamespace

from rustyera_tui.runtime import FrontendEvent, PresentationBatch

from tui_fixture_compare import presentation_result


class FakePresentationModel:
    def __init__(self) -> None:
        self.lines = [SimpleNamespace(segments=[SimpleNamespace(text="ORACLE_READY")])]
        self.snapshots: list[dict[int, object]] = []
        self.deltas: list[dict[int, object]] = []

    def apply_snapshot(self, value: dict[int, object]) -> None:
        self.snapshots.append(value)

    def apply_delta(self, value: dict[int, object]) -> None:
        self.deltas.append(value)


def test_presentation_batch_updates_projection_and_returns_the_stable_wait() -> None:
    model = FakePresentationModel()
    snapshot = {0: 1}
    delta = {0: 2}
    wait = {1: 2, 5: False}

    result = presentation_result(
        model,  # type: ignore[arg-type]
        FrontendEvent(
            "presentation_batch", PresentationBatch(snapshot, delta, wait, True)
        ),
    )

    assert model.snapshots == [snapshot]
    assert model.deltas == [delta]
    assert result == {
        "termination": "waitingInput",
        "output": ["ORACLE_READY"],
        "wait_kind": 2,
        "system_input": False,
    }
