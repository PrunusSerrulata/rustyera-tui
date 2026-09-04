import json
from pathlib import Path

import blake3
import pytest
import rustyera_tui.project as project_module

from compatibility_test_support import reference_identity
from project_test_support import png_header
from rustyera_tui.project import (
    FILE_ERB,
    IO_CONFLICT,
    SOURCE_INDEX_VERSION,
    ProjectBundle,
    ProjectFile,
)
from rustyera_tui.wire import unwrap_variant, variant


def test_quick_scan_reloads_only_the_selected_file(tmp_path: Path) -> None:
    (tmp_path / "ERB").mkdir()
    selected = tmp_path / "ERB" / "selected.erb"
    unselected = tmp_path / "ERB" / "unselected.erb"
    selected.write_text("@SELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    unselected.write_text("@UNSELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    bundle = ProjectBundle.scan_quick(tmp_path)
    bundle.compatibility = reference_identity()
    unselected_hash = bundle.files["ERB/unselected.erb"].content_hash
    selected.write_text("@SELECTED\nPRINTL v2\nRETURN\n", encoding="utf-8")
    unselected.write_text("@UNSELECTED\nPRINTL v2\nRETURN\n", encoding="utf-8")

    candidate, request = bundle.reload_file(selected)

    assert len(request[2]) == 1
    assert unwrap_variant(request[2][0])[1][0][0] == "ERB/selected.erb"
    assert candidate.files["ERB/selected.erb"].payload == variant(
        0, "@SELECTED\nPRINTL v2\nRETURN\n"
    )
    assert candidate.files["ERB/unselected.erb"].payload is None
    assert candidate.files["ERB/unselected.erb"].content_hash == unselected_hash
    refreshed = json.loads(
        (tmp_path / ".rustyera" / "cache" / "source-index-v1.json").read_text(encoding="utf-8")
    )
    assert refreshed["files"]["ERB/selected.erb"]["signature"] == (
        f"{selected.stat().st_size}:{selected.stat().st_mtime_ns // 1_000_000}"
    )
    repeated = ProjectBundle.scan_quick(tmp_path)
    repeated.compatibility = reference_identity()
    assert repeated.scan_metrics.source_files_reused == 2
    assert repeated.scan_metrics.source_files_hashed == 0


def test_folder_reload_preserves_unselected_source_generation(tmp_path: Path) -> None:
    selected_folder = tmp_path / "ERB" / "folder"
    unselected_folder = tmp_path / "ERB" / "single"
    selected_folder.mkdir(parents=True)
    unselected_folder.mkdir()
    selected = selected_folder / "command.erb"
    unselected = unselected_folder / "command.erb"
    removed = selected_folder / "removed.erb"
    selected.write_text("@SELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    unselected.write_text("@UNSELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    removed.write_text("@REMOVED\nRETURN\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    bundle = ProjectBundle.scan_quick(tmp_path)
    bundle.compatibility = reference_identity()
    unselected_hash = bundle.files["ERB/single/command.erb"].content_hash
    selected.write_text("@SELECTED\nPRINTL v2\nRETURN\n", encoding="utf-8")
    unselected.write_text("@UNSELECTED\nPRINTL v2\nRETURN\n", encoding="utf-8")
    removed.unlink()

    candidate, request = bundle.reload_folder(selected_folder)

    changes = [unwrap_variant(change) for change in request[2]]
    assert [(tag, fields[-1] if tag == 1 else fields[0][0]) for tag, fields in changes] == [
        (0, "ERB/folder/command.erb"),
        (1, "ERB/folder/removed.erb"),
    ]
    assert candidate.files["ERB/folder/command.erb"].payload == variant(
        0, "@SELECTED\nPRINTL v2\nRETURN\n"
    )
    assert "ERB/folder/removed.erb" not in candidate.files
    assert candidate.files["ERB/single/command.erb"].payload is None
    assert candidate.files["ERB/single/command.erb"].content_hash == unselected_hash

    final, single_request = candidate.reload_file(unselected)

    assert len(single_request[2]) == 1
    assert unwrap_variant(single_request[2][0])[1][0][0] == "ERB/single/command.erb"
    assert final.files["ERB/single/command.erb"].payload == variant(
        0, "@UNSELECTED\nPRINTL v2\nRETURN\n"
    )


def test_quick_scan_all_reload_submits_only_changed_sources(tmp_path: Path) -> None:
    (tmp_path / "ERB").mkdir()
    changed = tmp_path / "ERB" / "changed.erb"
    unchanged = tmp_path / "ERB" / "unchanged.erb"
    changed.write_text("@CHANGED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    unchanged.write_text("@UNCHANGED\nRETURN\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    bundle = ProjectBundle.scan_quick(tmp_path)
    bundle.compatibility = reference_identity()
    changed.write_text("@CHANGED\nPRINTL v2\nRETURN\n", encoding="utf-8")

    candidate, request = bundle.rescan()

    assert candidate.is_materialized
    assert len(request[2]) == 1
    assert unwrap_variant(request[2][0])[1][0][0] == "ERB/changed.erb"


def test_cache_hit_baseline_hydrates_the_first_scoped_reload_without_leaking_disk_changes(
    tmp_path: Path,
) -> None:
    selected_folder = tmp_path / "ERB" / "folder"
    unselected_folder = tmp_path / "ERB" / "single"
    selected_folder.mkdir(parents=True)
    unselected_folder.mkdir()
    selected = selected_folder / "command.erb"
    unselected = unselected_folder / "command.erb"
    selected.write_text("PRINTL FOLDER_VERSION=1\n", encoding="utf-8")
    unselected.write_text("PRINTL SINGLE_VERSION=1\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    baseline = ProjectBundle.scan_quick(tmp_path)
    baseline.compatibility = reference_identity()
    baseline.reload_baseline_pending = True
    old_unselected_hash = baseline.files["ERB/single/command.erb"].content_hash
    selected.write_text("PRINTL FOLDER_VERSION=2\n", encoding="utf-8")
    unselected.write_text("PRINTL SINGLE_VERSION=2\n", encoding="utf-8")

    candidate, request = baseline.reload_folder(selected_folder)

    submitted = [fields[0][0] for tag, fields in map(unwrap_variant, request[2]) if tag == 0]
    assert submitted == ["ERB/folder/command.erb"]
    untouched = candidate.files["ERB/single/command.erb"]
    assert untouched.payload is None
    assert untouched.content_hash == old_unselected_hash
    assert candidate.reload_baseline_pending is True

    _, second_request = candidate.reload_file(unselected)

    submitted = {
        fields[0][0]: fields[0][2]
        for tag, fields in map(unwrap_variant, second_request[2])
        if tag == 0
    }
    assert submitted == {
        "ERB/folder/command.erb": variant(0, "PRINTL FOLDER_VERSION=2\n"),
        "ERB/single/command.erb": variant(0, "PRINTL SINGLE_VERSION=2\n"),
    }


def test_cache_hit_first_scoped_reload_replaces_the_sparse_runtime_baseline(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "ERB" / "selected.erb"
    untouched = tmp_path / "ERB" / "untouched.erb"
    selected.parent.mkdir(parents=True)
    selected.write_text("PRINTL SELECTED_VERSION=1\n", encoding="utf-8")
    untouched.write_text("PRINTL UNTOUCHED_VERSION=1\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    baseline = ProjectBundle.scan_quick(tmp_path)
    baseline.compatibility = reference_identity()
    baseline.reload_baseline_pending = True
    selected.write_text("PRINTL SELECTED_VERSION=2\n", encoding="utf-8")

    candidate, request = baseline.reload_file(selected)

    submitted = {
        fields[0][0]: fields[0][2] for tag, fields in map(unwrap_variant, request[2]) if tag == 0
    }
    assert submitted == {
        "ERB/selected.erb": variant(0, "PRINTL SELECTED_VERSION=2\n"),
        "ERB/untouched.erb": variant(0, "PRINTL UNTOUCHED_VERSION=1\n"),
    }
    assert candidate.is_materialized
    assert candidate.reload_baseline_pending is False


def test_stable_read_retries_when_signature_changes_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@OLD\nRETURN\n", encoding="utf-8")
    original = project_module._source_signature
    calls = 0

    def changing_signature(path: Path) -> tuple[int, int, int, int, int]:
        nonlocal calls
        calls += 1
        signature = original(path)
        if calls == 2:
            return (*signature[:-1], signature[-1] + 1)
        return signature

    monkeypatch.setattr(project_module, "_source_signature", changing_signature)

    loaded = project_module._stable_read_project_file(tmp_path, source, FILE_ERB)

    assert loaded.payload == variant(0, "@OLD\nRETURN\n")
    assert calls >= 4


def test_stable_read_reports_conflict_after_repeated_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@MAIN\nRETURN\n", encoding="utf-8")
    counter = 0

    def always_changes(_path: Path) -> tuple[int, int, int, int, int]:
        nonlocal counter
        counter += 1
        return (1, 1, 1, 1, counter)

    monkeypatch.setattr(project_module, "_source_signature", always_changes)

    loaded = project_module._stable_read_project_file(tmp_path, source, FILE_ERB)

    tag, fields = unwrap_variant(loaded.payload)
    assert tag == 2
    assert fields[0][0] == IO_CONFLICT


@pytest.mark.parametrize("replacement", ["deleted", "directory"])
def test_stable_read_reports_delete_and_type_change_during_read(
    replacement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@MAIN\nRETURN\n", encoding="utf-8")
    original = project_module.read_project_file
    replaced = False

    def replace_then_read(root: Path, path: Path, category: int) -> ProjectFile:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.unlink()
            if replacement == "directory":
                path.mkdir()
        return original(root, path, category)

    monkeypatch.setattr(project_module, "read_project_file", replace_then_read)

    loaded = project_module._stable_read_project_file(tmp_path, source, FILE_ERB)

    assert unwrap_variant(loaded.payload)[0] == 2


def test_parallel_scan_preserves_multiple_io_errors_in_path_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "a.erb"
    second = tmp_path / "b.erb"
    first.write_text("@A", encoding="utf-8")
    second.write_text("@B", encoding="utf-8")
    original = Path.read_bytes

    def fail_sources(path: Path) -> bytes:
        if path == first:
            raise PermissionError("first denied")
        if path == second:
            raise OSError("second failed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_sources)

    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()

    assert list(bundle.files) == ["a.erb", "b.erb"]
    assert "first denied" in str(bundle.files["a.erb"].payload)
    assert "second failed" in str(bundle.files["b.erb"].payload)


@pytest.mark.parametrize("mode", ["scan", "quick", "materialize"])
def test_project_read_failures_are_preserved_as_deterministic_payloads(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@MAIN\nRETURN\n", encoding="utf-8")
    if mode == "quick":
        ProjectBundle.scan_quick(tmp_path)
        source.write_text("@CHANGED\nRETURN\n", encoding="utf-8")
    if mode == "materialize":
        ProjectBundle.scan_quick(tmp_path)
        baseline = ProjectBundle.scan_quick(tmp_path)
        baseline.compatibility = reference_identity()
    else:
        baseline = None
    original = Path.read_bytes

    def fail_target(path: Path) -> bytes:
        if path == source:
            raise PermissionError("read denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_target)
    bundle = (
        baseline.materialize()
        if baseline is not None
        else ProjectBundle.scan_quick(tmp_path)
        if mode == "quick"
        else ProjectBundle.scan(tmp_path)
    )

    tag, fields = unwrap_variant(bundle.files["main.erb"].payload)
    assert tag == 2
    assert "read denied" in str(fields)


def test_quick_scan_reuses_stat_index_and_materializes_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")

    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()
    assert not quick.is_materialized
    assert len(quick.identity()[1]) == 32
    assert (tmp_path / ".rustyera" / "cache" / "source-index-v1.json").is_file()

    original_read_bytes = Path.read_bytes

    def reject_source_read(path: Path) -> bytes:
        if path == source:
            pytest.fail("an unchanged source must not be reopened by the quick scan")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_source_read)
    repeated = ProjectBundle.scan_quick(tmp_path)
    repeated.compatibility = reference_identity()
    assert not repeated.is_materialized
    assert repeated.identity() == quick.identity()
    assert repeated.scan_metrics.source_index_present
    assert repeated.scan_metrics.source_files_reused == 1
    assert repeated.scan_metrics.source_files_hashed == 0
    monkeypatch.undo()

    materialized = repeated.materialize()
    assert materialized.is_materialized
    assert materialized.identity() == quick.identity()


def test_quick_scan_migrates_browser_index_and_keeps_incremental_reuse(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    index_path = tmp_path / ".rustyera" / "cache" / "source-index-v1.json"
    browser_index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = browser_index["files"]["main.erb"]
    entry["category"] = "erb"
    entry["signature"] = f"{source.stat().st_size}:{source.stat().st_mtime_ns // 1_000_000}"
    browser_index["version"] = 2
    index_path.write_text(json.dumps(browser_index), encoding="utf-8")

    migrated = ProjectBundle.scan_quick(tmp_path)
    migrated.compatibility = reference_identity()

    assert migrated.scan_metrics.source_files_reused == 1
    assert migrated.scan_metrics.source_files_hashed == 0
    canonical = json.loads(index_path.read_text(encoding="utf-8"))
    assert canonical["version"] == SOURCE_INDEX_VERSION
    assert canonical["files"]["main.erb"]["category"] == FILE_ERB
    assert isinstance(canonical["files"]["main.erb"]["signature"], str)

    source.write_text("@SYSTEM_TITLE\nPRINTL CHANGED\nRETURN\n", encoding="utf-8")
    updated = ProjectBundle.scan_quick(tmp_path)
    updated.compatibility = reference_identity()
    repeated = ProjectBundle.scan_quick(tmp_path)
    repeated.compatibility = reference_identity()

    assert updated.scan_metrics.source_files_reused == 0
    assert updated.scan_metrics.source_files_hashed == 1
    assert updated.scan_metrics.source_index_misses == ("main.erb",)
    assert "source_index_misses" not in updated.scan_metrics.telemetry()
    assert repeated.scan_metrics.source_files_reused == 1
    assert repeated.scan_metrics.source_files_hashed == 0
    assert repeated.scan_metrics.source_index_misses == ()


@pytest.mark.parametrize(
    ("version", "metadata"),
    [
        (1, None),
        (2, [0, 3, "invalid", False]),
    ],
)
def test_quick_scan_migrates_missing_or_invalid_cached_image_metadata(
    tmp_path: Path, version: int, metadata: object
) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    image = resources / "image.png"
    image.write_bytes(png_header(2, 3))
    ProjectBundle.scan_quick(tmp_path)
    index_path = tmp_path / ".rustyera" / "cache" / "source-index-v1.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["version"] = version
    if metadata is None:
        index["files"]["resources/image.png"].pop("image_metadata", None)
    else:
        index["files"]["resources/image.png"]["image_metadata"] = metadata
    index_path.write_text(json.dumps(index), encoding="utf-8")

    warm = ProjectBundle.scan_quick(tmp_path)
    warm.compatibility = reference_identity()

    assert warm.scan_metrics.source_files_reused == 1
    assert warm.scan_metrics.source_files_hashed == 0
    assert warm.files["resources/image.png"].payload == variant(
        3,
        {0: 24, 1: {0: 2, 1: 3, 2: "png", 3: False}},
    )
    migrated = json.loads(index_path.read_text(encoding="utf-8"))
    assert migrated["version"] == SOURCE_INDEX_VERSION
    assert migrated["files"]["resources/image.png"]["image_metadata"] == {
        "width": 2,
        "height": 3,
        "format": "png",
        "animated": False,
    }


def test_sparse_cache_hit_baseline_materializes_only_for_the_first_reload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ERB" / "main.erb"
    source.parent.mkdir()
    source.write_text("PRINTL VERSION=1\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    baseline = ProjectBundle.scan_quick(tmp_path)
    baseline.compatibility = reference_identity()
    baseline.reload_baseline_pending = True

    assert not baseline.is_materialized

    candidate, request = baseline.reload_file(source)

    assert candidate.is_materialized
    assert candidate.reload_baseline_pending is False
    assert [unwrap_variant(change)[0] for change in request[2]] == [0]


def test_quick_scan_rechecks_a_new_source_before_reusing_its_payload(tmp_path: Path) -> None:
    source = tmp_path / "main.erb"
    source.write_bytes(b"@OLD\nRETURN\n")
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()
    old_identity = quick.identity()

    source.write_bytes(b"@NEW\nRETURN\n")
    materialized = quick.materialize()

    assert materialized.files["main.erb"].payload == variant(0, "@NEW\nRETURN\n")
    assert materialized.identity() != old_identity


def test_compact_project_file_manifest_keeps_identity_from_content_hash(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"package")
    digest = blake3.blake3(b"@SYSTEM_TITLE\nRETURN\n").digest()
    compact = ProjectBundle.from_project_file_manifest(
        project_file,
        {
            0: 7,
            1: [{0: "main.erb", 1: FILE_ERB, 2: variant(0, ""), 3: digest}],
            2: reference_identity(),
        },
    )
    compact.compatibility = reference_identity()
    full = ProjectBundle(
        tmp_path,
        7,
        {
            "main.erb": ProjectFile(
                "main.erb", FILE_ERB, variant(0, "@SYSTEM_TITLE\nRETURN\n"), digest
            )
        },
    )
    full.compatibility = reference_identity()

    assert compact.identity() == full.identity()


def test_project_identity_matches_the_cross_host_fixed_vector(tmp_path: Path) -> None:
    entries = [
        ("ERB/a.erb", 2, bytes([1]) * 32),
        ("ERB/A.erb", 1, bytes([2]) * 32),
        ("CSV/config.csv", 0, bytes(range(32))),
        ("resources/icon.png", 4, bytes([255]) * 32),
    ]

    def bundle(items: list[tuple[str, int, bytes]]) -> ProjectBundle:
        return ProjectBundle(
            tmp_path,
            7,
            {
                path: ProjectFile(path, category, variant(0, ""), digest)
                for path, category, digest in items
            },
            compatibility=reference_identity(),
        )

    left = bundle(entries)
    right = bundle(list(reversed(entries)))

    assert left.identity() == right.identity()
    assert left.identity()[1] == bytes.fromhex(
        "15d72199f2e33c429e0bd4185e3441a23c0650c14278d5760c5127d1a70e07ec"
    )


def test_project_identity_includes_io_error_message(tmp_path: Path) -> None:
    denied = ProjectBundle(
        tmp_path,
        1,
        {"main.erb": ProjectFile("main.erb", 2, variant(2, {0: 1, 1: "denied"}), None)},
    )
    denied.compatibility = reference_identity()
    missing = ProjectBundle(
        tmp_path,
        1,
        {"main.erb": ProjectFile("main.erb", 2, variant(2, {0: 0, 1: "missing"}), None)},
    )
    missing.compatibility = reference_identity()

    assert len(denied.identity()[1]) == 32
    assert denied.identity() != missing.identity()


def test_rescan_produces_upsert_and_remove_changes(tmp_path: Path) -> None:
    first = tmp_path / "first.erb"
    second = tmp_path / "second.erh"
    first.write_text("@A", encoding="utf-8")
    second.write_text("#DIM X", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    first.write_text("@B", encoding="utf-8")
    second.unlink()

    candidate, request = bundle.rescan()
    assert candidate.revision == 2
    assert request[0] == 1
    assert request[1] == 2
    assert [unwrap_variant(change)[0] for change in request[2]] == [0, 1]
