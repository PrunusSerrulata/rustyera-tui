from pathlib import Path

import blake3
import pytest

from rustyera_tui.project import (
    FILE_RESOURCE,
    FILE_RESOURCE_MANIFEST,
    IO_CONFLICT,
    ProjectBundle,
    ProjectFile,
    StorageBackend,
)
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


def test_project_scanners_submit_nested_sprite_manifests_and_images(tmp_path: Path) -> None:
    (tmp_path / "CSV").mkdir()
    portraits = tmp_path / "resources" / "剧情肖像"
    portraits.mkdir(parents=True)
    (tmp_path / "CSV" / "GAMEBASE.CSV").write_text("コード,1\n", encoding="utf-8")
    manifest = "\ufeff萝乐娜_泣,Rorona-portraits.webp,1040,1182,1000,1125\n"
    image = b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\xe7\x03\x00\x64\x04\x00"
    (portraits / "Portraits.csv").write_text(manifest, encoding="utf-8")
    (portraits / "Rorona-portraits.webp").write_bytes(image)

    scanned = ProjectBundle.scan(tmp_path)
    quick = ProjectBundle.scan_quick(tmp_path)

    resource_manifest = scanned.files["resources/剧情肖像/Portraits.csv"]
    resource_image = scanned.files["resources/剧情肖像/Rorona-portraits.webp"]
    assert resource_manifest.category == FILE_RESOURCE_MANIFEST
    assert resource_manifest.payload == variant(0, manifest.removeprefix("\ufeff"))
    assert resource_image.category == FILE_RESOURCE
    assert resource_image.payload == variant(1, image)
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_normalize_cp932_sources_to_utf8(tmp_path: Path) -> None:
    source = "サブディレクトリを検索する:YES\r\n"
    path = tmp_path / "_fixed.config"
    path.write_bytes(source.encode("cp932"))

    scanned = ProjectBundle.scan(tmp_path)
    quick = ProjectBundle.scan_quick(tmp_path)

    expected_hash = blake3.blake3(source.encode("utf-8")).digest()
    assert scanned.files["_fixed.config"].payload == variant(0, source)
    assert scanned.files["_fixed.config"].content_hash == expected_hash
    assert quick.files["_fixed.config"].content_hash == expected_hash
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_normalize_gbk_sources_to_utf8(tmp_path: Path) -> None:
    source = ";阶层怪物列表\r\n#DIM KAI_LIST\r\n"
    path = tmp_path / "main.erh"
    path.write_bytes(source.encode("gbk"))

    scanned = ProjectBundle.scan(tmp_path)
    quick = ProjectBundle.scan_quick(tmp_path)

    expected_hash = blake3.blake3(source.encode("utf-8")).digest()
    assert scanned.files["main.erh"].payload == variant(0, source)
    assert scanned.files["main.erh"].content_hash == expected_hash
    assert quick.files["main.erh"].content_hash == expected_hash
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanner_reports_text_invalid_in_supported_encodings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.erb"
    path.write_bytes(b"\x81")

    bundle = ProjectBundle.scan_quick(tmp_path)

    assert bundle.is_materialized
    payload = bundle.files["main.erb"].payload
    assert payload is not None
    assert unwrap_variant(payload)[0] == 2


def test_project_scanners_follow_resource_directory_links(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    resources = tmp_path / "resources"
    (sources / "CSV").mkdir(parents=True)
    (sources / "ERB").mkdir()
    resources.mkdir()
    (sources / "CSV" / "GAMEBASE.CSV").write_text("コード,1\n", encoding="utf-8")
    (sources / "ERB" / "main.erb").write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    try:
        (resources / "CSV").symlink_to(sources / "CSV", target_is_directory=True)
        (resources / "ERB").symlink_to(sources / "ERB", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    scanned = ProjectBundle.scan(resources)
    quick = ProjectBundle.scan_quick(resources)

    assert list(scanned.files) == ["CSV/GAMEBASE.CSV", "ERB/main.erb"]
    assert list(quick.files) == ["CSV/GAMEBASE.CSV", "ERB/main.erb"]
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_ignore_uninstalled_sources_outside_canonical_roots(
    tmp_path: Path,
) -> None:
    (tmp_path / "CSV").mkdir()
    (tmp_path / "ERB" / "GUIDE").mkdir(parents=True)
    (tmp_path / "GUIDE").mkdir()
    (tmp_path / "resources").mkdir()
    (tmp_path / "patch" / "ERB").mkdir(parents=True)
    (tmp_path / "CSV" / "GAMEBASE.CSV").write_text("コード,1\n", encoding="utf-8")
    (tmp_path / "ERB" / "GUIDE" / "main.erb").write_text("@SYSTEM_TITLE", encoding="utf-8")
    (tmp_path / "GUIDE" / "main.erb").write_text("@UNINSTALLED_GUIDE", encoding="utf-8")
    (tmp_path / "resources" / "notes.txt").write_text("not a resource manifest\n", encoding="utf-8")
    (tmp_path / "patch" / "ERB" / "optional.erb").write_text("@UNINSTALLED_PATCH", encoding="utf-8")
    (tmp_path / "emuera.config").write_text("描画インターフェース:TEXTRENDERER", encoding="utf-8")

    scanned = ProjectBundle.scan(tmp_path)
    quick = ProjectBundle.scan_quick(tmp_path)

    expected = ["CSV/GAMEBASE.CSV", "emuera.config", "ERB/GUIDE/main.erb"]
    assert list(scanned.files) == expected
    assert list(quick.files) == expected
    assert quick.materialize().identity() == scanned.identity()


def test_reload_accepts_a_file_below_a_resource_directory_link(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    resources = tmp_path / "resources"
    sources.mkdir()
    resources.mkdir()
    source = sources / "main.erb"
    source.write_text("@A", encoding="utf-8")
    try:
        (resources / "ERB").symlink_to(sources, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    bundle = ProjectBundle.scan(resources)
    source.write_text("@B", encoding="utf-8")

    candidate, request = bundle.reload_file(resources / "ERB" / "main.erb")

    assert candidate.files["ERB/main.erb"].payload == variant(0, "@B")
    assert unwrap_variant(request[2][0])[1][0][0] == "ERB/main.erb"


def test_quick_scan_reuses_stat_index_and_materializes_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.erb"
    source.write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")

    quick = ProjectBundle.scan_quick(tmp_path)
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
    assert repeated.identity() == quick.identity()
    monkeypatch.undo()

    materialized = quick.materialize()
    assert materialized.is_materialized
    assert materialized.identity() == quick.identity()


def test_project_identity_includes_io_error_message(tmp_path: Path) -> None:
    denied = ProjectBundle(
        tmp_path,
        1,
        {"main.erb": ProjectFile("main.erb", 2, variant(2, {0: 1, 1: "denied"}), None)},
    )
    missing = ProjectBundle(
        tmp_path,
        1,
        {"main.erb": ProjectFile("main.erb", 2, variant(2, {0: 0, 1: "missing"}), None)},
    )

    assert len(denied.identity()[1]) == 32
    assert denied.identity() != missing.identity()


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
    assert entries[0][2] is None
    change_token = entries[0][3]
    chunk = backend.handle({0: 5, 1: 1, 2: "save01.sav", 3: variant(5, 0, 1, change_token), 4: ""})
    chunk_tag, chunk_fields = unwrap_variant(chunk[1])
    assert chunk_tag == 6
    assert chunk_fields[:3] == [b"t", 0, False]


def test_storage_defaults_to_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_TUI_DATA_DIR", raising=False)

    backend = StorageBackend(tmp_path)

    assert backend.data_root == tmp_path.resolve()
    assert backend._namespace_root(1) == tmp_path.resolve() / "save"
    assert backend.compiled_cache_path() == (
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v7.bin.zst"
    )
    assert backend.obsolete_compiled_cache_paths() == (
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v6.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v5.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v4.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v3.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v2.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v1.bin.gz",
    )
