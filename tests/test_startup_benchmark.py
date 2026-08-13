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
    try:
        (source / "linked.erb").symlink_to(external)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")
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


def test_small_sample_percentiles_use_nearest_rank() -> None:
    benchmark = load_benchmark_module()

    assert benchmark.percentile([5.0, 1.0, 4.0, 2.0, 3.0], 0.5) == 3.0
    assert benchmark.percentile([5.0, 1.0, 4.0, 2.0, 3.0], 0.95) == 5.0


def test_source_index_proof_distinguishes_exact_scenarios() -> None:
    benchmark = load_benchmark_module()

    assert benchmark.source_index_proof_matches(
        "cold-no-index",
        {
            "source_index_present": False,
            "index_existed_before_launch": False,
            "source_files_reused": 0,
            "source_files_hashed": 3,
        },
    )
    assert benchmark.source_index_proof_matches(
        "cold-indexed",
        {
            "source_index_present": True,
            "index_existed_before_launch": True,
            "source_files_reused": 3,
            "source_files_hashed": 0,
        },
    )
    assert not benchmark.source_index_proof_matches(
        "warm",
        {
            "source_index_present": False,
            "index_existed_before_launch": True,
            "source_files_reused": 0,
            "source_files_hashed": 3,
        },
    )


@pytest.mark.parametrize(
    ("scenario", "cache_hit"),
    [
        ("cold-no-index", False),
        ("cold-indexed", False),
        ("warm", True),
        ("project_file", True),
        ("project_file", False),
    ],
)
def test_each_scenario_accepts_complete_metrics(scenario: str, cache_hit: bool) -> None:
    benchmark = load_benchmark_module()
    durations = {
        field: 1.0
        for field in benchmark.DIRECTORY_HOST_FIELDS
        | benchmark.COLD_CORE_FIELDS
        | benchmark.CACHE_CORE_FIELDS
        | {"source_materialize_ms"}
    }
    result = {
        "cache_hit": cache_hit,
        "durations": durations,
        "milestones": {field: 1.0 for field in benchmark.MILESTONE_FIELDS},
        "peak_rss_bytes": 1024,
    }

    benchmark.validate_sample(scenario, result)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("milestones", "start_submitted_ms"),
        ("durations", "submission_transfer_ms"),
        ("durations", "cache_decode_ms"),
    ],
)
def test_incomplete_metrics_are_rejected(section: str, field: str) -> None:
    benchmark = load_benchmark_module()
    result = {
        "cache_hit": True,
        "durations": {
            field_name: 1.0
            for field_name in benchmark.DIRECTORY_HOST_FIELDS | benchmark.CACHE_CORE_FIELDS
        },
        "milestones": {field_name: 1.0 for field_name in benchmark.MILESTONE_FIELDS},
        "peak_rss_bytes": 1024,
    }
    result[section].pop(field)

    with pytest.raises(RuntimeError, match=field):
        benchmark.validate_sample("warm", result)


def test_missing_peak_memory_is_rejected() -> None:
    benchmark = load_benchmark_module()
    result = {
        "cache_hit": True,
        "durations": {
            field: 1.0 for field in benchmark.DIRECTORY_HOST_FIELDS | benchmark.CACHE_CORE_FIELDS
        },
        "milestones": {field: 1.0 for field in benchmark.MILESTONE_FIELDS},
        "peak_rss_bytes": 0,
    }

    with pytest.raises(RuntimeError, match="peak_rss_bytes"):
        benchmark.validate_sample("warm", result)


def test_phase_metrics_are_merged_from_small_telemetry_events() -> None:
    benchmark = load_benchmark_module()
    host: dict[str, object] = {}
    core: dict[str, object] = {}

    benchmark.merge_phase_metrics(
        {
            "event": "host_metrics",
            "attempt_id": 1,
            "peak_rss_bytes": 10,
            "enumerate_ms": 12.5,
            "source_files_hashed": 3,
        },
        host,
        core,
    )
    benchmark.merge_phase_metrics(
        {"event": "core_phase", "phase": "parse_ms", "duration_ms": 20.0},
        host,
        core,
    )

    assert host == {"enumerate_ms": 12.5, "source_files_hashed": 3}
    assert core == {"parse_ms": 20.0}


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
