from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def load_benchmark_module():
    path = Path(__file__).parents[1] / "tools" / "startup-benchmark.py"
    spec = importlib.util.spec_from_file_location("rustyera_startup_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_isolated_project_materializes_external_symlink(tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    external = tmp_path / "external.erb"
    external.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "linked.erb").symlink_to(external)
    destination = tmp_path / "isolated"
    destination.mkdir()

    copied = benchmark.isolated_project(source, destination)

    assert not (copied / "linked.erb").is_symlink()
    assert (copied / "linked.erb").read_text(encoding="utf-8") == external.read_text(
        encoding="utf-8"
    )


def test_project_file_scenario_accepts_exact_and_recompiled_packages() -> None:
    benchmark = load_benchmark_module()

    assert benchmark.cache_proof_matches("project_file", True)
    assert benchmark.cache_proof_matches("project_file", False)
    assert not benchmark.cache_proof_matches("project_file", None)
    assert benchmark.cache_proof_matches("cold", False)
    assert benchmark.cache_proof_matches("warm", True)


def test_selector_initialization_failure_cleans_process_and_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benchmark = load_benchmark_module()
    telemetry_read, telemetry_write = os.pipe()
    master, slave = os.pipe()
    runtime = tmp_path / "runtime.dylib"
    runtime.write_bytes(b"")
    process = object()
    terminated: list[object] = []
    monkeypatch.setattr(benchmark.os, "pipe", lambda: (telemetry_read, telemetry_write))
    monkeypatch.setattr(benchmark.pty, "openpty", lambda: (master, slave))
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(benchmark, "terminate", terminated.append)
    monkeypatch.setattr(
        benchmark.selectors,
        "DefaultSelector",
        lambda: (_ for _ in ()).throw(OSError("selector unavailable")),
    )

    with pytest.raises(OSError, match="selector unavailable"):
        benchmark.launch(
            tmp_path,
            project_file=False,
            runtime_library=runtime,
            timeout=1,
        )

    assert terminated == [process]
    for descriptor in (telemetry_read, telemetry_write, master, slave):
        with pytest.raises(OSError):
            os.fstat(descriptor)
