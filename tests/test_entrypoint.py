from pathlib import Path
from typing import Any

import pytest

import rustyera_tui.__main__ as entrypoint
from rustyera_tui.__main__ import build_parser


class FakeRuntimeWorker:
    def __init__(self, *_args: Any, fail_early: bool = False, **_kwargs: Any) -> None:
        self.ident: int | None = None
        self.alive = False
        self.fail_early = fail_early
        self.start_calls = 0
        self.stop_calls = 0
        self.join_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.ident = 1
        self.alive = not self.fail_early

    def is_alive(self) -> bool:
        return self.alive

    def stop(self) -> None:
        self.stop_calls += 1
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        assert timeout is None
        self.join_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.alive:
            self.stop()
        if self.ident is not None:
            self.join()


def test_resource_directory_is_omitted_when_not_specified() -> None:
    args = build_parser().parse_args([])

    assert args.resource_directory is None
    assert args.project_file is None


def test_resource_directory_accepts_an_explicit_path(tmp_path: Path) -> None:
    args = build_parser().parse_args([str(tmp_path)])

    assert args.resource_directory == tmp_path


def test_project_file_option_coexists_with_higher_priority_directory(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    args = build_parser().parse_args([str(tmp_path), "--project-file", str(project_file)])

    assert args.resource_directory == tmp_path
    assert args.project_file == project_file


@pytest.mark.parametrize("failure", [ImportError("UI import failed"), RuntimeError("UI failed")])
def test_started_worker_is_cleaned_when_ui_setup_fails(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeRuntimeWorker()
    monkeypatch.setattr(entrypoint, "_worker_class", lambda: lambda *_args, **_kwargs: worker)
    monkeypatch.setattr(entrypoint, "_app_class", lambda: (_ for _ in ()).throw(failure))
    monkeypatch.setattr("sys.argv", ["rustyera-tui"])

    with pytest.raises(type(failure), match=str(failure)):
        entrypoint.main()

    assert (worker.start_calls, worker.stop_calls, worker.join_calls, worker.shutdown_calls) == (
        1,
        1,
        1,
        1,
    )


def test_runtime_early_failure_is_joined_without_a_second_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeRuntimeWorker(fail_early=True)
    monkeypatch.setattr(entrypoint, "_worker_class", lambda: lambda *_args, **_kwargs: worker)
    monkeypatch.setattr(
        entrypoint,
        "_app_class",
        lambda: lambda **_kwargs: type("App", (), {"run": lambda _self: None})(),
    )
    monkeypatch.setattr("sys.argv", ["rustyera-tui"])

    entrypoint.main()

    assert (worker.start_calls, worker.stop_calls, worker.join_calls, worker.shutdown_calls) == (
        1,
        0,
        1,
        1,
    )


@pytest.mark.parametrize("app_error", [None, KeyboardInterrupt()])
def test_normal_exit_and_cancellation_stop_the_worker_once(
    app_error: BaseException | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = FakeRuntimeWorker()

    def run(_self: object) -> None:
        if app_error is not None:
            raise app_error

    monkeypatch.setattr(entrypoint, "_worker_class", lambda: lambda *_args, **_kwargs: worker)
    monkeypatch.setattr(
        entrypoint,
        "_app_class",
        lambda: lambda **_kwargs: type("App", (), {"run": run})(),
    )
    monkeypatch.setattr("sys.argv", ["rustyera-tui"])

    if app_error is None:
        entrypoint.main()
    else:
        with pytest.raises(KeyboardInterrupt):
            entrypoint.main()

    assert (worker.start_calls, worker.stop_calls, worker.join_calls, worker.shutdown_calls) == (
        1,
        1,
        1,
        1,
    )
