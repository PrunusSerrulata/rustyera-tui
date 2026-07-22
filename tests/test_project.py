from pathlib import Path

import blake3
import pytest

from rustyera_tui.project import IO_CONFLICT, ProjectBundle, StorageBackend
from rustyera_tui.wire import unwrap_variant, variant


def test_project_scanner_is_utf8_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "ERB").mkdir()
    (tmp_path / "CSV").mkdir()
    (tmp_path / "ERB" / "main.erb").write_text("@EVENTFIRST\nPRINTL 你好", encoding="utf-8")
    (tmp_path / "CSV" / "Abl.csv").write_bytes(b"\xef\xbb\xbf0,test\n")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")

    bundle = ProjectBundle.scan(tmp_path)
    assert list(bundle.files) == ["CSV/Abl.csv", "ERB/main.erb"]
    manifest = bundle.manifest()
    assert manifest[0] == 1
    csv = manifest[1][0]
    assert csv[2] == variant(0, "0,test\n")
    assert csv[3] == blake3.blake3(b"0,test\n").digest()


def test_rescan_produces_upsert_and_remove_changes(tmp_path: Path) -> None:
    first = tmp_path / "first.erb"
    second = tmp_path / "second.erh"
    first.write_text("@A", encoding="utf-8")
    second.write_text("#DIM X", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    first.write_text("@B", encoding="utf-8")
    second.unlink()

    candidate, request = bundle.rescan()
    assert candidate.revision == 2
    assert request[0] == 1
    assert request[1] == 2
    assert [unwrap_variant(change)[0] for change in request[2]] == [0, 1]


def test_reload_file_rejects_paths_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.erb").write_text("@A", encoding="utf-8")
    outside = tmp_path / "outside.erb"
    outside.write_text("@B", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the active project"):
        ProjectBundle.scan(project).reload_file(outside)


def test_storage_enforces_revision_preconditions_and_lists_root(tmp_path: Path) -> None:
    backend = StorageBackend(tmp_path, data_root=tmp_path / "frontend-data")
    write = {
        0: 1,
        1: 1,
        2: "save01.sav",
        3: variant(1, b"one", True, variant(1)),
        4: "write-1",
    }
    result = backend.handle(write)[1]
    assert unwrap_variant(result)[0] == 1
    revision = unwrap_variant(result)[1][0]

    conflict = dict(write)
    conflict[0] = 2
    conflict[3] = variant(1, b"two", True, variant(2, "stale"))
    conflict[4] = "write-2"
    error_tag, error_fields = unwrap_variant(backend.handle(conflict)[1])
    assert error_tag == 4
    assert error_fields[0][0] == IO_CONFLICT

    overwrite = dict(conflict)
    overwrite[0] = 3
    overwrite[3] = variant(1, b"two", True, variant(2, revision))
    overwrite[4] = "write-3"
    assert unwrap_variant(backend.handle(overwrite)[1])[0] == 1

    listed = backend.handle({0: 4, 1: 1, 2: "", 3: variant(2, "save*.sav", False), 4: ""})
    entries = unwrap_variant(listed[1])[1][0]
    assert [entry[0] for entry in entries] == ["save01.sav"]


def test_storage_defaults_to_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_TUI_DATA_DIR", raising=False)

    backend = StorageBackend(tmp_path)

    assert backend.data_root == tmp_path.resolve()
    assert backend._namespace_root(1) == tmp_path.resolve() / "save"
