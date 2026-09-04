import json
import unicodedata
from pathlib import Path

import blake3
import pytest
import rustyera_tui.project as project_module

from compatibility_test_support import reference_identity, snake_identity
from project_test_support import png_header
from rustyera_tui.project import (
    FILE_RESOURCE,
    IO_CONFLICT,
    ProjectBundle,
    ProjectFile,
    StorageBackend,
)
from rustyera_tui.wire import decode, unwrap_variant, variant


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


@pytest.mark.parametrize("operation", [variant(5, 0, 1), variant(5, 0, 1, None)])
def test_storage_range_accepts_an_omitted_optional_token(tmp_path: Path, operation: list) -> None:
    backend = StorageBackend(tmp_path)
    request = {0: 1, 1: 1, 2: "save00.sav", 3: operation}
    tag, fields = unwrap_variant(backend.handle(request)[1])
    assert tag == 4
    assert fields[0][0] == 0  # NotFound, rather than an invalid tuple shape.
    saved = backend._namespace_root(1) / "save00.sav"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"header")
    tag, fields = unwrap_variant(backend.handle(request)[1])
    assert tag == 6
    assert fields[:3] == [b"h", 0, False]
    assert isinstance(fields[3], str)


def test_storage_idempotency_results_are_epoch_scoped_and_lru_bounded(tmp_path: Path) -> None:
    backend = StorageBackend(tmp_path)
    backend.maximum_idempotent_results = 2
    backend.begin_epoch(7)

    def write(request_id: int, key: str, value: bytes) -> None:
        backend.handle(
            {
                0: request_id,
                1: 1,
                2: "save.sav",
                3: variant(1, value, True, variant(0)),
                4: key,
            }
        )

    write(1, "first", b"1")
    write(2, "second", b"2")
    backend.handle(
        {
            0: 3,
            1: 1,
            2: "save.sav",
            3: variant(1, b"ignored", True, variant(0)),
            4: "first",
        }
    )
    write(4, "third", b"3")

    assert list(backend.idempotent_results) == ["first", "third"]
    backend.begin_epoch(8)
    assert backend.idempotent_results == {}


def test_storage_idempotency_results_are_byte_bounded(tmp_path: Path) -> None:
    backend = StorageBackend(tmp_path)
    backend.maximum_idempotent_bytes = 15
    backend.begin_epoch(7)
    (tmp_path / "sav").mkdir()

    for request_id, key in enumerate(("first-key", "second-key"), start=1):
        (tmp_path / "sav" / f"{request_id}.sav").write_bytes(b"x")
        backend.handle(
            {
                0: request_id,
                1: 1,
                2: f"{request_id}.sav",
                3: variant(3, variant(0)),
                4: key,
            }
        )

    assert backend.idempotent_result_bytes <= backend.maximum_idempotent_bytes
    assert list(backend.idempotent_results) == ["second-key"]

    (tmp_path / "sav" / "large.sav").write_bytes(b"x")
    backend.handle(
        {
            0: 3,
            1: 1,
            2: "large.sav",
            3: variant(3, variant(0)),
            4: "oversized-key" * 4,
        }
    )
    assert "oversized-key" * 4 not in backend.idempotent_results
    backend.begin_epoch(8)
    assert backend.idempotent_result_bytes == 0
    assert backend._idempotent_result_sizes == {}


@pytest.mark.parametrize("namespace", [0, 3])
def test_project_data_storage_fallbacks_preserve_private_writes(
    tmp_path: Path, namespace: int
) -> None:
    project = tmp_path / "game"
    xml = project / "XML"
    xml.mkdir(parents=True)
    skill = xml / "SKILL_LIFE.xml"
    skill.write_text("<skilldef />", encoding="utf-8")
    backend = StorageBackend(project, data_root=tmp_path / "frontend-data")

    read = backend.handle({0: 1, 1: namespace, 2: "XML/SKILL_LIFE.xml", 3: variant(0), 4: ""})
    assert unwrap_variant(read[1])[1][0] == b"<skilldef />"

    listed = backend.handle(
        {0: 2, 1: namespace, 2: "XML", 3: variant(2, "SKILL*.xml", False), 4: ""}
    )
    entries = unwrap_variant(listed[1])[1][0]
    assert [entry[0] for entry in entries] == ["XML/SKILL_LIFE.xml"]

    private_skill = backend._namespace_root(namespace) / "XML" / "SKILL_LIFE.xml"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("<private />", encoding="utf-8")
    override = backend.handle({0: 3, 1: namespace, 2: "XML/SKILL_LIFE.xml", 3: variant(0), 4: ""})
    assert unwrap_variant(override[1])[1][0] == b"<private />"

    written = backend.handle(
        {
            0: 4,
            1: namespace,
            2: "XML/SKILL_LIFE.xml",
            3: variant(1, b"<written />", True, variant(0)),
            4: "write-private",
        }
    )
    assert unwrap_variant(written[1])[0] == 1
    assert private_skill.read_bytes() == b"<written />"
    assert skill.read_bytes() == b"<skilldef />"


def test_storage_defaults_to_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERA_TUI_DATA_DIR", raising=False)

    backend = StorageBackend(tmp_path)

    assert backend.data_root == tmp_path.resolve()
    assert backend._namespace_root(1) == tmp_path.resolve() / "sav"
    assert backend._namespace_root(2) == tmp_path.resolve() / "sav"
    assert backend.compiled_cache_path() == (
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project.reracache"
    )
    assert backend.obsolete_compiled_cache_paths() == (
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project.reraproj",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v8.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v7.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v6.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v5.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v4.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v3.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v2.bin.zst",
        tmp_path.resolve() / ".rustyera" / "cache" / "compiled-project-v1.bin.gz",
    )


def test_project_scanner_includes_audio_resources_for_full_exports(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    audio = b"OggS\x00example"
    (resources / "theme.ogg").write_bytes(audio)

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()

    assert scanned.files["resources/theme.ogg"].category == FILE_RESOURCE
    assert scanned.files["resources/theme.ogg"].payload == variant(3, {0: len(audio), 1: None})

    temporary, size = scanned.write_full_manifest_temp()
    try:
        encoded = temporary.read_bytes()
        assert len(encoded) == size
        manifest = decode(encoded)
        resource = next(file for file in manifest[1] if file[0] == "resources/theme.ogg")
        assert resource[2] == variant(1, audio)
    finally:
        temporary.unlink(missing_ok=True)


def test_resource_scan_hashes_from_a_stream_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = tmp_path / "large.png"
    payload = png_header(8, 9) + b"x" * 1024
    resource.write_bytes(payload)

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: pytest.fail("resource scan must stream the file"),
    )
    loaded = project_module.read_project_file(tmp_path, resource, FILE_RESOURCE)

    assert loaded.content_size == len(payload)
    assert loaded.content_hash == blake3.blake3(payload).digest()


def test_storage_stat_hashes_from_a_stream_without_materializing_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = StorageBackend(tmp_path)
    save = tmp_path / "sav" / "large.sav"
    save.parent.mkdir()
    save.write_bytes(b"state")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: pytest.fail("storage stat must stream the file"),
    )

    result = backend.handle({0: 1, 1: 1, 2: "large.sav", 3: variant(4), 4: ""})

    tag, fields = unwrap_variant(result[1])
    assert tag == 5
    assert fields[0][0] == 5
    assert fields[0][1] == blake3.blake3(b"state").hexdigest()


def test_configuration_write_is_atomic_and_detects_external_changes(tmp_path: Path) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[display]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None

    bundle.write_configuration(digest, "[display]\nfont_size = 20\n")
    assert config.read_text(encoding="utf-8") == "[display]\nfont_size = 20\n"

    with pytest.raises(RuntimeError, match="其他程序修改"):
        bundle.write_configuration(digest, "[display]\nfont_size = 22\n")


def test_configuration_creation_is_idempotent_and_requires_utf8(tmp_path: Path) -> None:
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    contents = "[meta]\nschema_version = 1\n"
    bundle.write_configuration(b"", contents)
    bundle.write_configuration(b"", contents.replace("\n", "\r\n"))
    assert (tmp_path / "reraconfig.toml").read_text(encoding="utf-8") == contents

    (tmp_path / "reraconfig.toml").write_bytes(b"\x81")
    invalid = ProjectBundle.scan(tmp_path).files["reraconfig.toml"].payload
    assert invalid is not None
    assert unwrap_variant(invalid)[0] == 2


def test_packaged_project_configuration_appends_the_runtime_update(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"base-incomplete")
    bundle = ProjectBundle(tmp_path, 1, {}, project_file)
    bundle.compatibility = reference_identity()
    captured: list[tuple[bytes, bytes, str]] = []

    def prepare(project: bytes, expected: bytes, contents: str) -> tuple[int, bytes]:
        captured.append((project, expected, contents))
        return 4, b"journal"

    bundle.write_configuration(b"digest", "[display]\nfont_size = 20\n", prepare)

    assert captured == [(b"base-incomplete", b"digest", "[display]\nfont_size = 20\n")]
    assert project_file.read_bytes() == b"basejournal"


@pytest.mark.parametrize("namespace", [0, 3])
@pytest.mark.parametrize(
    "operation",
    [variant(0), variant(4), variant(5, 0, 64, None), variant(2, "sentinel.xml", False)],
)
def test_snake_mutable_reads_never_fall_back_to_reference_root(
    tmp_path: Path, namespace: int, operation: list
) -> None:
    root = tmp_path / "game"
    (root / "shared").mkdir(parents=True)
    sentinel = root / "shared/sentinel.xml"
    sentinel.write_bytes(b"reference sentinel")
    reference = StorageBackend(root)
    snake = StorageBackend(
        root, compatibility_profile="emuera.skia.snake", resource_bundle=ProjectBundle.scan(root)
    )
    relative = "shared" if operation[0] == 2 else "shared/sentinel.xml"
    request = {0: 1, 1: namespace, 2: relative, 3: operation, 4: ""}
    assert unwrap_variant(reference.handle(request)[1])[0] != 4
    snake_result = unwrap_variant(snake.handle(request)[1])
    assert snake_result[0] == 4 or (snake_result[0] == 2 and snake_result[1][0] == [])
    resource = snake.handle({0: 2, 1: 5, 2: "shared/sentinel.xml", 3: variant(0), 4: ""})
    assert unwrap_variant(resource[1])[1][0] == b"reference sentinel"
    assert sentinel.read_bytes() == b"reference sentinel"


def test_resource_storage_uses_manifest_and_keeps_data_overlay_separate(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    (plugins / "nested").mkdir(parents=True)
    (plugins / "a.xml").write_bytes(b"source")
    (plugins / "nested/b.txt").write_bytes(b"nested")
    (tmp_path / "main.erb").write_text("@MAIN\nRETURN\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = snake_identity()
    backend = StorageBackend(
        tmp_path, compatibility_profile="emuera.skia.snake", resource_bundle=bundle
    )
    overlay = backend._namespace_root(3) / "plugins/a.xml"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"overlay")

    def request(path: str, operation: list, namespace: int = 5) -> tuple:
        return unwrap_variant(backend.handle({0: 1, 1: namespace, 2: path, 3: operation})[1])

    assert request("plugins/a.xml", variant(0))[1][0] == b"source"
    assert request("plugins/a.xml", variant(0), 3)[1][0] == b"overlay"
    listed = request("plugins", variant(2, "*", True))[1][0]
    assert [item[0] for item in listed] == ["plugins/a.xml", "plugins/nested/b.txt"]
    assert [item[0] for item in request("plugins", variant(2, "*.xml", False))[1][0]] == [
        "plugins/a.xml"
    ]
    assert request("PLUGINS/a.xml", variant(4))[1][0] == {
        0: 6,
        1: blake3.blake3(b"source").hexdigest(),
    }
    assert request("plugins/a.xml", variant(5, 2, 3))[1][:3] == [b"urc", 2, False]
    assert request("plugins/a.xml", variant(5, 2, 3, listed[0][3]))[1][:3] == [b"urc", 2, False]
    assert request("plugins/a.xml", variant(5, 0, 3, "stale"))[1][0][0] == IO_CONFLICT
    assert request("main.erb", variant(0))[1][0][0] == 1
    assert request("../outside.xml", variant(0))[1][0][0] == 2


def test_resource_mutations_never_replay_writable_namespace_results(tmp_path: Path) -> None:
    (tmp_path / "seed.xml").write_bytes(b"seed")
    backend = StorageBackend(tmp_path, resource_bundle=ProjectBundle.scan(tmp_path))
    write = variant(1, b"changed", True, variant(0))
    assert backend.handle({0: 1, 1: 3, 2: "seed.xml", 3: write, 4: "same"})[1][0] == 1
    for path, operation in [("missing/sub/seed.xml", write), ("seed.xml", variant(3, variant(0)))]:
        result = backend.handle({0: 2, 1: 5, 2: path, 3: operation, 4: "same"})[1]
        assert unwrap_variant(result)[1][0][0] == 4
    assert not (tmp_path / "missing").exists()
    assert (tmp_path / "seed.xml").read_bytes() == b"seed"


@pytest.mark.parametrize(
    "operation", [variant(0), variant(4), variant(5, 0, 1, None), variant(2, "*.xml", True)]
)
def test_resource_storage_reports_changed_contents_without_not_found(
    tmp_path: Path, operation: list
) -> None:
    from dataclasses import replace

    source = tmp_path / "seed.xml"
    source.write_bytes(b"one")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.files["seed.xml"] = replace(bundle.files["seed.xml"], source_signature=None)
    bundle.compatibility = reference_identity()
    backend = StorageBackend(tmp_path, resource_bundle=bundle)
    source.write_bytes(b"two")
    result = backend.handle({0: 1, 1: 5, 2: "" if operation[0] == 2 else "seed.xml", 3: operation})[
        1
    ]
    assert unwrap_variant(result)[1][0][0] == IO_CONFLICT


def test_resource_storage_bounds_reads_and_lists_without_materializing_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rustyera_tui.resource_storage as resources

    (tmp_path / "a.xml").write_bytes(b"seed")
    (tmp_path / "b.xml").write_bytes(b"seed")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    backend = StorageBackend(tmp_path, resource_bundle=bundle)
    monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("resource stat must stream"))
    monkeypatch.setattr(resources, "MAXIMUM_FULL_READ_BYTES", 2)
    assert backend.handle({0: 1, 1: 5, 2: "a.xml", 3: variant(4)})[1][0] == 5
    for operation in [
        variant(0),
        variant(5, -1, 1, None),
        variant(5, 0, 4 * 1024 * 1024 + 1, None),
    ]:
        assert backend.handle({0: 1, 1: 5, 2: "a.xml", 3: operation})[1][0] == 4
    monkeypatch.setattr(resources, "MAXIMUM_LIST_ENTRIES", 1)
    assert backend.handle({0: 1, 1: 5, 2: "", 3: variant(2, "*", True)})[1][0] == 4


def test_resource_storage_rejects_retargeted_symlinks_and_lists_no_external_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    source = root / "seed.xml"
    source.write_bytes(b"seed")
    bundle = ProjectBundle.scan(root)
    bundle.compatibility = reference_identity()
    backend = StorageBackend(root, resource_bundle=bundle)
    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"outside")
    source.unlink()
    source.symlink_to(outside)
    for path, operation in [("seed.xml", variant(0)), ("", variant(2, "*", True))]:
        assert unwrap_variant(backend.handle({0: 1, 1: 5, 2: path, 3: operation})[1])[1][0][0] == 1
    data = root / "data"
    data.mkdir()
    (data / "loop").symlink_to(data, target_is_directory=True)
    assert backend.handle({0: 1, 1: 3, 2: "", 3: variant(2, "*", True)})[1][0] == 4


def test_packaged_resource_storage_is_manifest_only_and_rejects_normalized_collisions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    item = ProjectFile(
        "é.xml", FILE_RESOURCE, variant(1, b"embedded"), blake3.blake3(b"embedded").digest(), 8
    )
    bundle = ProjectBundle(tmp_path, 1, {item.relative_path: item}, tmp_path / "game.reraproj")
    bundle.compatibility = reference_identity()
    backend = StorageBackend(tmp_path, resource_bundle=bundle)
    assert (
        unwrap_variant(backend.handle({0: 1, 1: 5, 2: "e\u0301.xml", 3: variant(0)})[1])[1][0]
        == b"embedded"
    )
    (tmp_path / "external.xml").write_bytes(b"external")
    bundle.files["external.xml"] = ProjectFile(
        "external.xml",
        FILE_RESOURCE,
        variant(3, {0: 8, 1: None}),
        blake3.blake3(b"external").digest(),
        8,
    )
    assert (
        unwrap_variant(backend.handle({0: 1, 1: 5, 2: "external.xml", 3: variant(0)})[1])[1][0][0]
        == 2
    )
    bundle.files["e\u0301.xml"] = replace(item, relative_path="e\u0301.xml")
    result = backend.handle({0: 1, 1: 5, 2: "", 3: variant(2, "*", True)})[1]
    assert unwrap_variant(result)[1][0][0] == 2


def test_data_list_distinguishes_missing_directory_from_dangling_entry(tmp_path: Path) -> None:
    (tmp_path / "seed.xml").write_bytes(b"resource")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = snake_identity()
    backend = StorageBackend(
        tmp_path, compatibility_profile="emuera.skia.snake", resource_bundle=bundle
    )
    request = {0: 1, 1: 3, 2: "", 3: variant(2, "*", True)}
    assert backend.handle(request)[1] == variant(2, [])
    assert backend.handle({**request, 1: 5})[1][1][0][0][0] == "seed.xml"

    root = backend._namespace_root(3)
    root.mkdir(parents=True)
    (root / "good.txt").write_bytes(b"good")
    (root / "dangling.txt").symlink_to(root / "missing.txt")
    result = backend.handle(request)[1]
    assert result[0] == 4
    assert result[1][0][0] == 2
    assert "dangling" in result[1][0][1]


def test_data_list_rejects_a_file_deleted_after_discovery_without_partial_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rustyera_tui import storage_listing

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend._namespace_root(3)
    root.mkdir(parents=True)
    good, doomed = root / "good.txt", root / "doomed.txt"
    good.write_bytes(b"good")
    doomed.write_bytes(b"doomed")

    def disappearing_entries(_path: Path):
        yield good
        doomed.unlink()
        yield doomed

    monkeypatch.setattr(storage_listing, "directory_entries", disappearing_entries)
    result = backend.handle({0: 1, 1: 3, 2: "", 3: variant(2, "*", True)})[1]
    assert result[0] == 4
    assert result[1][0][0] == 2
    assert good.read_bytes() == b"good"


@pytest.mark.parametrize(
    ("failure", "kind"),
    [(FileNotFoundError(2, "gone"), 2), (PermissionError(13, "denied"), 1), (OSError(5, "I/O"), 6)],
)
def test_data_list_preserves_traversal_error_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    kind: int,
) -> None:
    from rustyera_tui import storage_listing

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend._namespace_root(3)
    child = root / "child"
    child.mkdir(parents=True)
    (root / "good.txt").write_bytes(b"good")
    original = storage_listing.directory_entries

    def interrupted_entries(path: Path):
        if path == child:
            raise failure
        yield from original(path)

    monkeypatch.setattr(storage_listing, "directory_entries", interrupted_entries)
    result = backend.handle({0: 1, 1: 3, 2: "", 3: variant(2, "*", True)})[1]
    assert result[0] == 4
    assert result[1][0][0] == kind
    if kind in (1, 6):
        assert result[1][0][2] == failure.errno


def test_data_list_rejects_target_removed_after_normalized_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rustyera_tui import storage

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    child = backend._namespace_root(3) / "child"
    child.mkdir(parents=True)
    original = storage.resolve_data_path

    def remove_selected(root: Path, relative: str):
        selected = original(root, relative)
        child.rmdir()
        return selected

    monkeypatch.setattr(storage, "resolve_data_path", remove_selected)
    result = backend.handle({0: 1, 1: 3, 2: "child", 3: variant(2, "*", True)})[1]
    assert result[0] == 4
    assert result[1][0][0] == 2


@pytest.mark.parametrize("name", ["bad\\name.txt", "C:seed.txt"])
@pytest.mark.parametrize("prefix", ["", "nested"])
@pytest.mark.parametrize("profile", ["emuera.em", "emuera.skia.snake"])
def test_data_list_and_lookup_reject_invalid_actual_basename(
    tmp_path: Path,
    name: str,
    prefix: str,
    profile: str,
) -> None:
    backend = StorageBackend(tmp_path, compatibility_profile=profile)
    root = backend._namespace_root(3)
    directory = root / prefix
    directory.mkdir(parents=True)
    (directory / name).write_bytes(b"invalid name")
    good = directory / "good.txt"
    good.write_bytes(b"unchanged")
    operations = [
        ("", variant(2, "*.txt", True)),
        (prefix, variant(2, "*.txt", False)),
    ]
    if profile == "emuera.skia.snake":
        operations.append((f"{prefix}/good.txt".lstrip("/"), variant(0)))
    for relative, operation in operations:
        result = backend.handle({0: 1, 1: 3, 2: relative, 3: operation})[1]
        assert result[0] == 4
        assert result[1][0][0] == 2
    assert good.read_bytes() == b"unchanged"


def test_snake_data_safe_directory_alias_retains_logical_prefix(tmp_path: Path) -> None:
    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend._namespace_root(3)
    real = root / "real"
    real.mkdir(parents=True)
    (real / "Seed.TXT").write_bytes(b"seed")
    (root / "LiNk").symlink_to(real, target_is_directory=True)

    def request(relative: str, operation: list) -> list:
        return backend.handle({0: 1, 1: 3, 2: relative, 3: operation})[1]

    assert request("link/seed.txt", variant(0))[1][0] == b"seed"
    assert request("LINK/SEED.TXT", variant(4))[1][0][0] == 4
    listed = request("link", variant(2, "*.txt", True))
    assert listed[0] == 2
    assert [entry[0] for entry in listed[1][0]] == ["LiNk/Seed.TXT"]
    assert request(listed[1][0][0][0], variant(0))[1][0] == b"seed"


def test_reference_recursive_listing_does_not_follow_safe_directory_alias(tmp_path: Path) -> None:
    backend = StorageBackend(tmp_path)
    root = backend._namespace_root(3)
    real = root / "real"
    real.mkdir(parents=True)
    (real / "seed.txt").write_bytes(b"seed")
    (root / "alias").symlink_to(real, target_is_directory=True)
    (root / "file.txt").symlink_to(real / "seed.txt")
    result = backend.handle({0: 1, 1: 3, 2: "", 3: variant(2, "*.txt", True)})[1]
    assert result[0] == 2
    assert [entry[0] for entry in result[1][0]] == ["file.txt", "real/seed.txt"]


def test_reference_listing_fallback_depends_on_directory_existence(tmp_path: Path) -> None:
    source = tmp_path / "plugins" / "nested"
    source.mkdir(parents=True)
    (source / "resource.txt").write_bytes(b"resource")
    backend = StorageBackend(tmp_path)
    request = {0: 1, 1: 3, 2: "plugins", 3: variant(2, "*.txt", True)}
    assert [item[0] for item in backend.handle(request)[1][1][0]] == ["plugins/nested/resource.txt"]
    overlay = backend._namespace_root(3) / "plugins" / "overlay.txt"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"overlay")
    assert [item[0] for item in backend.handle(request)[1][1][0]] == ["plugins/overlay.txt"]
    overlay.unlink()
    assert backend.handle(request)[1] == variant(2, [])


def test_resource_list_reports_removed_committed_file_as_conflict(tmp_path: Path) -> None:
    source = tmp_path / "seed.xml"
    source.write_bytes(b"resource")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = snake_identity()
    backend = StorageBackend(tmp_path, resource_bundle=bundle)
    source.unlink()
    result = backend.handle({0: 1, 1: 5, 2: "", 3: variant(2, "*", True)})[1]
    assert result[0] == 4
    assert result[1][0][0] == IO_CONFLICT


def test_snake_storage_shared_pattern_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from rustyera_tui import storage_listing
    from rustyera_tui.storage_pattern import SnakeStoragePattern

    vectors = Path(__file__).resolve().parent / "fixtures/snake-storage-patterns.json"
    cases = json.loads(vectors.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        name, pattern = case["name"], case["pattern"]
        if "error" in case:
            with pytest.raises(ValueError):
                SnakeStoragePattern(pattern).matches(name)
        else:
            assert SnakeStoragePattern(pattern).matches(name) is case["expected"], case["id"]
        # Empty strings exercise the matcher but cannot represent a filesystem basename.
        if not name:
            continue
        root = tmp_path / case["id"]
        root.mkdir()
        item = ProjectFile(
            name, FILE_RESOURCE, variant(1, b"seed"), blake3.blake3(b"seed").digest(), 4
        )
        bundle = ProjectBundle(root, 1, {name: item}, compatibility=snake_identity())
        backend = StorageBackend(
            root, compatibility_profile="emuera.skia.snake", resource_bundle=bundle
        )
        data = backend._namespace_root(3)
        data.mkdir(parents=True)
        source = data / "backing-file"
        source.write_bytes(b"seed")
        # The shared length/work-limit vectors exceed physical basename limits. Project a
        # discovered entry onto a real small file so the storage boundary still executes
        # matching/validation, without relying on the host filesystem's NAME_MAX.
        entry = SimpleNamespace(
            name=name,
            lstat=source.lstat,
            resolve=source.resolve,
            stat=source.stat,
        )
        monkeypatch.setattr(
            storage_listing, "directory_entries", lambda _path, entry=entry: iter([entry])
        )
        for namespace in (3, 5):
            result = backend.handle({0: 1, 1: namespace, 2: "", 3: variant(2, pattern, False)})[1]
            if "error" in case:
                assert result[0] == 4, (case["id"], namespace, result)
                assert result[1][0][0] == 2, (case["id"], namespace, result)
            else:
                assert result[0] == 2, (case["id"], namespace, result)
                expected = [unicodedata.normalize("NFC", name)] if case["expected"] else []
                assert [row[0] for row in result[1][0]] == expected, (case["id"], namespace)


@pytest.mark.parametrize("pattern", [None, ""])
@pytest.mark.parametrize("name", ["x" * 4097, "a\0b", "İ" * 2048])
def test_snake_unfiltered_pattern_still_validates_name(pattern: str | None, name: str) -> None:
    from rustyera_tui.storage_pattern import SnakeStoragePattern

    with pytest.raises(ValueError):
        SnakeStoragePattern(pattern).matches(name)


@pytest.mark.parametrize(
    ("data_profile", "identity", "data_expected", "resource_expected"),
    [
        ("emuera.skia.snake", reference_identity, "[ab].txt", "a.txt"),
        ("emuera.em", snake_identity, "a.txt", "[ab].txt"),
    ],
)
def test_resource_pattern_profile_comes_from_committed_bundle_not_data_flag(
    tmp_path: Path,
    data_profile: str,
    identity,
    data_expected: str,
    resource_expected: str,
) -> None:
    items = {
        name: ProjectFile(
            name, FILE_RESOURCE, variant(1, b"seed"), blake3.blake3(b"seed").digest(), 4
        )
        for name in ("a.txt", "[ab].txt")
    }
    bundle = ProjectBundle(tmp_path, 1, items, compatibility=identity())
    backend = StorageBackend(tmp_path, compatibility_profile=data_profile, resource_bundle=bundle)
    data = backend._namespace_root(3)
    data.mkdir(parents=True)
    for name in items:
        (data / name).write_bytes(b"seed")
    for namespace, expected in ((3, data_expected), (5, resource_expected)):
        result = backend.handle({0: 1, 1: namespace, 2: "", 3: variant(2, "[ab].txt", False)})[1]
        assert result[0] == 2
        assert [row[0] for row in result[1][0]] == [expected]


def test_reference_data_listing_keeps_fnmatch_case_semantics(tmp_path: Path) -> None:
    import fnmatch

    backend = StorageBackend(tmp_path)
    data = backend._namespace_root(3)
    data.mkdir()
    names = ["a.txt", "SEED.TXT"]
    for name in names:
        (data / name).write_bytes(b"seed")
    result = backend.handle({0: 1, 1: 3, 2: "", 3: variant(2, "*.txt", False)})[1]
    assert result[0] == 2
    assert [row[0] for row in result[1][0]] == sorted(
        name for name in names if fnmatch.fnmatch(name, "*.txt")
    )


def test_resource_list_requires_resolved_bundle_profile(tmp_path: Path) -> None:
    bundle = ProjectBundle(tmp_path, 1, {})
    backend = StorageBackend(
        tmp_path, compatibility_profile="emuera.skia.snake", resource_bundle=bundle
    )
    result = backend.handle({0: 1, 1: 5, 2: "", 3: variant(2, "*", False)})[1]
    assert result[0] == 4
    assert result[1][0][0] == 2
    assert "compatibility identity" in result[1][0][1]
