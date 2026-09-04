import json
import threading
import time
import unicodedata
from pathlib import Path

import blake3
import pytest
import rustyera_tui.project as project_module

from compatibility_test_support import reference_identity, snake_identity
from rustyera_tui.project import (
    FILE_ALS,
    FILE_ERD,
    FILE_RESOURCE,
    FILE_RESOURCE_MANIFEST,
    ProjectBundle,
    ProjectFile,
    StorageBackend,
)
from rustyera_tui.wire import decode, unwrap_variant, variant


def test_unresolved_bundle_cannot_be_serialized(tmp_path: Path) -> None:
    bundle = ProjectBundle.scan(tmp_path)
    with pytest.raises(ValueError, match="compatibility identity"):
        bundle.manifest()
    with pytest.raises(ValueError, match="compatibility identity"):
        bundle.identity()


def test_compatibility_survives_quick_materialization_and_full_export(tmp_path: Path) -> None:
    (tmp_path / "main.erb").write_text("@MAIN\nRETURN\n", encoding="utf-8")
    (tmp_path / "reraconfig.toml").write_text(
        '[meta]\nschema_version = 4\n[compatibility]\nprofile = "emuera.skia.snake"\n',
        encoding="utf-8",
    )
    ProjectBundle.scan_quick(tmp_path)
    bundle = ProjectBundle.scan_quick(tmp_path)
    configuration = bundle.root_configuration()
    assert configuration is not None and "emuera.skia.snake" in configuration[2][1][0]
    bundle.compatibility = snake_identity()
    bundle.configuration_digest = configuration[3]
    materialized = bundle.materialize()
    assert materialized.identity()[2] == snake_identity()
    assert materialized.identity()[3] == configuration[3]
    temporary, _ = bundle.write_full_manifest_temp()
    try:
        assert decode(temporary.read_bytes())[2] == snake_identity()
    finally:
        temporary.unlink()


@pytest.mark.parametrize("initial", [None, "[meta]\nschema_version = 4\n"])
def test_root_configuration_detects_changes_after_scan(tmp_path: Path, initial: str | None) -> None:
    path = tmp_path / "reraconfig.toml"
    if initial is not None:
        path.write_text(initial, encoding="utf-8")
    bundle = ProjectBundle.scan_quick(tmp_path)
    path.write_text(
        '[meta]\nschema_version = 4\n[compatibility]\nprofile = "emuera.skia.snake"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="扫描后"):
        bundle.root_configuration()


def test_snake_storage_uses_project_slots_and_keeps_private_runtime_data(tmp_path: Path) -> None:
    reference = StorageBackend(tmp_path)
    snake = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    assert reference.compiled_cache_path() != snake.compiled_cache_path()
    assert snake.data_root == tmp_path / ".rustyera" / "profiles" / "emuera.skia.snake"
    for namespace in (1, 2):
        original = reference._resolve(namespace, "save00.sav")
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"reference save")
        assert snake._resolve(namespace, "save00.sav") == original
        assert snake._resolve(namespace, "save00.sav").read_bytes() == b"reference save"
    assert snake._resolve(5, "main.erb") == reference._resolve(5, "main.erb")
    assert snake._resolve(3, "state.db") != reference._resolve(3, "state.db")
    with pytest.raises(ValueError, match="unsupported"):
        StorageBackend(tmp_path, compatibility_profile="../../outside")


def test_snake_directory_saves_ignore_configured_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    private = tmp_path / "private"
    monkeypatch.setenv("ERA_TUI_DATA_DIR", str(private))

    backend = StorageBackend(project, compatibility_profile="emuera.skia.snake")

    assert backend._namespace_root(1) == project.resolve() / "sav"
    assert backend._namespace_root(2) == project.resolve() / "sav"
    assert backend._namespace_root(3).is_relative_to(private.resolve())


def test_snake_directory_saves_ignore_explicit_data_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    private = tmp_path / "private"

    backend = StorageBackend(
        project,
        data_root=private,
        compatibility_profile="emuera.skia.snake",
    )

    assert backend._namespace_root(1) == project.resolve() / "sav"
    assert backend._namespace_root(2) == project.resolve() / "sav"
    assert backend._namespace_root(3).is_relative_to(private.resolve())


def test_snake_packaged_saves_use_the_persistent_project_copy(tmp_path: Path) -> None:
    package = tmp_path / "game.reraproj"
    package.write_bytes(b"package identity")
    persistent = tmp_path / "packaged-projects"

    backend = StorageBackend(
        tmp_path,
        data_root=persistent,
        identity_path=package,
        compatibility_profile="emuera.skia.snake",
    )

    assert backend._namespace_root(1) == backend.save_root / "sav"
    assert backend._namespace_root(2) == backend.save_root / "sav"
    assert backend.save_root.parent == persistent.resolve() / "games"
    assert backend.data_root == backend.save_root / ".rustyera/profiles/emuera.skia.snake"


def test_parallel_ordered_merges_out_of_order_results_and_reports_on_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_module, "PROJECT_IO_WORKERS", 2)
    second_finished = threading.Event()
    coordinator = threading.get_ident()
    progress_threads: list[int] = []

    def operation(value: int) -> str:
        if value == 0:
            assert second_finished.wait(timeout=1)
        else:
            second_finished.set()
        return str(value)

    result = project_module._parallel_ordered(
        [0, 1],
        operation,
        progress=lambda _completed, _total: progress_threads.append(threading.get_ident()),
    )

    assert result == ["0", "1"]
    assert progress_threads and set(progress_threads) == {coordinator}


def test_parallel_ordered_raises_the_first_input_error_not_the_first_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_module, "PROJECT_IO_WORKERS", 2)
    later_failed = threading.Event()

    def operation(value: int) -> int:
        if value == 0:
            assert later_failed.wait(timeout=1)
            raise ValueError("first input")
        later_failed.set()
        raise ValueError("second input")

    with pytest.raises(ValueError, match="first input"):
        project_module._parallel_ordered([0, 1], operation)


def test_parallel_ordered_cancellation_does_not_start_queued_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_module, "PROJECT_IO_WORKERS", 2)
    cancelled = threading.Event()
    started: list[int] = []
    lock = threading.Lock()

    def operation(value: int) -> int:
        with lock:
            started.append(value)
        if value == 1:
            cancelled.set()
        else:
            while not cancelled.is_set():
                time.sleep(0.001)
        return value

    with pytest.raises(InterruptedError, match="cancelled"):
        project_module._parallel_ordered(list(range(20)), operation, cancelled=cancelled.is_set)

    assert sorted(started) == [0, 1]


def test_project_scanner_is_utf8_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "ERB").mkdir()
    (tmp_path / "CSV").mkdir()
    (tmp_path / "ERB" / "main.erb").write_text("@EVENTFIRST\nPRINTL 你好", encoding="utf-8")
    (tmp_path / "CSV" / "Abl.csv").write_bytes(b"\xef\xbb\xbf0,test\n")
    (tmp_path / "ignored.dll").write_bytes(b"ignored")

    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    assert list(bundle.files) == ["CSV/Abl.csv", "ERB/main.erb"]
    manifest = bundle.manifest()
    assert manifest[0] == 1
    csv = manifest[1][0]
    assert csv[2] == variant(0, "0,test\n")
    assert csv[3] == blake3.blake3(b"0,test\n").digest()


def test_alias_and_erd_inputs_survive_quick_scan_transfer_and_reload(tmp_path: Path) -> None:
    contents = {
        "CSV/FLAG.ALS": (FILE_ALS, "10,ten\n11,eleven\n300,large\n"),
        "ERB/nested/MATRIX@2.ERD": (FILE_ERD, "0,first\n1,last\n"),
        "ERB/nested/MATRIX@2.als": (FILE_ALS, "1,final\n"),
    }
    for relative, (_category, text) in contents.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    (tmp_path / "ignored.als").write_text("0,outside\n", encoding="utf-8")
    (tmp_path / "ignored.erd").write_text("0,outside\n", encoding="utf-8")
    cold = ProjectBundle.scan(tmp_path)
    cold.compatibility = snake_identity()
    ProjectBundle.scan_quick(tmp_path)
    index_path = tmp_path / ".rustyera/cache/source-index-v1.json"
    source_index = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in source_index["files"].values():
        entry["category"] = {FILE_ALS: "als", FILE_ERD: "erd"}[entry["category"]]
    index_path.write_text(json.dumps(source_index), encoding="utf-8")
    warm = ProjectBundle.scan_quick(tmp_path)
    warm.compatibility = snake_identity()
    assert warm.scan_metrics.source_files_reused == len(contents)
    assert warm.materialize().manifest() == cold.manifest()
    assert warm.identity() == cold.identity()
    assert set(cold.files) == set(contents)
    for relative, (category, text) in contents.items():
        item = cold.files[relative]
        assert (item.category, item.payload) == (category, variant(0, text))
        assert item.content_hash == blake3.blake3(text.encode()).digest()
    temporary, _ = warm.write_full_manifest_temp()
    try:
        assert decode(temporary.read_bytes()) == cold.manifest()
    finally:
        temporary.unlink()
    alias = tmp_path / "CSV/FLAG.ALS"
    alias.write_text("11,ten\n", encoding="utf-8")
    changed, request = warm.reload_file(alias)
    assert changed.identity()[1] != cold.identity()[1]
    assert request[2][0] == variant(0, changed.files["CSV/FLAG.ALS"].submitted())
    erd = tmp_path / "ERB/nested/MATRIX@2.ERD"
    erd.unlink()
    renamed = tmp_path / "ERB/nested/OTHER.erd"
    renamed.write_text("0,replacement\n", encoding="utf-8")
    rescanned, request = changed.reload_folder(erd.parent)
    assert "ERB/nested/MATRIX@2.ERD" not in rescanned.files
    assert rescanned.files["ERB/nested/OTHER.erd"].category == FILE_ERD
    assert variant(1, FILE_ERD, "ERB/nested/MATRIX@2.ERD") in request[2]


@pytest.mark.parametrize("suffix,category", [("als", FILE_ALS), ("erd", FILE_ERD)])
def test_new_index_inputs_require_utf8_without_legacy_fallback(
    tmp_path: Path, suffix: str, category: int
) -> None:
    path = tmp_path / f"INDEX.{suffix}"
    path.write_bytes("0,名前\n".encode("cp932"))
    for bundle in (ProjectBundle.scan(tmp_path), ProjectBundle.scan_quick(tmp_path)):
        item = bundle.files[path.name]
        assert item.category == category
        assert item.content_hash is None
        assert unwrap_variant(item.payload)[0] == 2


@pytest.mark.parametrize("name", ["TABLE.als", "TABLE.erd", "seed.xml"])
def test_new_project_inputs_reject_root_escape_and_symbolic_link_loops(
    tmp_path: Path, name: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / name
    outside.write_text("0,outside\n", encoding="utf-8")
    linked = root / name
    linked.symlink_to(outside)
    for bundle in (ProjectBundle.scan(root), ProjectBundle.scan_quick(root)):
        item = bundle.files[name]
        assert unwrap_variant(item.payload)[0] == 2
        assert item.content_hash is None
    linked.unlink()
    linked.symlink_to(linked)
    item = ProjectBundle.scan(root).files[name]
    assert unwrap_variant(item.payload)[0] == 2


def test_new_index_inputs_detect_normalized_path_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Supply both spellings explicitly so this also covers case-insensitive filesystems.
    monkeypatch.setattr(
        "rustyera_tui.project_bundle_scan._project_paths",
        lambda root: [root / "LOOKUP.als", root / "lookup.ALS"],
    )
    with pytest.raises(ValueError, match="duplicate normalized"):
        ProjectBundle.scan(tmp_path)


def test_new_index_input_in_legacy_linked_source_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    (source / "main.erb").write_text("@MAIN\nRETURN\n", encoding="utf-8")
    (source / "INDEX.erd").write_text("0,index\n", encoding="utf-8")
    (root / "ERB").symlink_to(source, target_is_directory=True)
    bundle = ProjectBundle.scan(root)
    assert unwrap_variant(bundle.files["ERB/main.erb"].payload)[0] == 0
    assert unwrap_variant(bundle.files["ERB/INDEX.erd"].payload)[0] == 2


def test_new_project_inputs_report_directory_cycles(tmp_path: Path) -> None:
    (tmp_path / "INDEX.erd").write_text("0,index\n", encoding="utf-8")
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError, match="directory contains a loop"):
        ProjectBundle.scan(tmp_path)


def test_new_index_input_is_not_silently_skipped_by_directory_deduplication(
    tmp_path: Path,
) -> None:
    backing = tmp_path / "backing"
    backing.mkdir()
    (backing / "FLAG.als").write_text("10,ten\n", encoding="utf-8")
    (tmp_path / "CSV").symlink_to(backing, target_is_directory=True)
    with pytest.raises(ValueError, match="repeated directory link"):
        ProjectBundle.scan(tmp_path)


def test_readonly_data_resources_are_recursive_and_exclude_storage(tmp_path: Path) -> None:
    payloads = {
        "XML/nested/schema.xml": b"<schema/>\r\n",
        "plugins/seed.db": b"SQLite format 3\x00\xff",
        "plugins/sub/other.sqlite": b"sqlite seed",
        "resources/story.txt": "场景\n".encode(),
    }
    for relative, contents in payloads.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    for directory in project_module.RESOURCE_DATA_EXCLUDED_ROOTS:
        path = tmp_path / directory / "private.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private")
    (tmp_path / "plugins/unsafe.dll").write_bytes(b"not executable")
    cold = ProjectBundle.scan(tmp_path)
    cold.compatibility = reference_identity()
    assert set(cold.files) == set(payloads)
    ProjectBundle.scan_quick(tmp_path)
    warm = ProjectBundle.scan_quick(tmp_path)
    warm.compatibility = reference_identity()
    assert warm.identity() == cold.identity()
    assert warm.scan_metrics.source_files_reused == len(payloads)
    for relative, contents in payloads.items():
        item = warm.files[relative]
        assert item.category == FILE_RESOURCE
        assert item.payload == project_module.external_resource(len(contents))
        assert item.content_hash == blake3.blake3(contents).digest()
        assert warm.resource_bytes(relative, item.content_hash) == contents
    temporary, _ = warm.write_full_manifest_temp()
    try:
        manifest = decode(temporary.read_bytes())
        assert {item[0]: item[2] for item in manifest[1]} == {
            relative: variant(1, contents) for relative, contents in payloads.items()
        }
    finally:
        temporary.unlink()
    seed = tmp_path / "plugins/seed.db"
    seed.write_bytes(b"changed seed")
    with pytest.raises(ValueError, match="digest"):
        warm.resource_bytes("plugins/seed.db", warm.files["plugins/seed.db"].content_hash)
    changed = ProjectBundle.scan_quick(tmp_path)
    changed.compatibility = reference_identity()
    assert changed.identity()[1] != warm.identity()[1]


def test_data_resource_rechecks_authorized_root_after_scan(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    resource = root / "seed.xml"
    resource.write_bytes(b"same content")
    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"same content")
    bundle = ProjectBundle.scan(root)
    resource.unlink()
    resource.symlink_to(outside)
    with pytest.raises(OSError, match="authorized root"):
        bundle.resource_bytes("seed.xml", bundle.files["seed.xml"].content_hash)


def test_project_scanner_uses_lowercase_instead_of_casefold_for_path_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "ß.erb").write_text("@SHARP_S\nRETURN\n", encoding="utf-8")
    (tmp_path / "st.erb").write_text("@ST\nRETURN\n", encoding="utf-8")

    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()

    assert list(bundle.files) == ["st.erb", "ß.erb"]


def test_project_bundle_uses_embedded_project_file_resources(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"container")
    source = "@SYSTEM_TITLE\nRETURN\n"
    resource = b"image"
    manifest = {
        2: reference_identity(),
        0: 7,
        1: [
            {
                0: "main.erb",
                1: 2,
                2: variant(0, source),
                3: blake3.blake3(source.encode()).digest(),
            },
            {
                0: "resources/a.png",
                1: 4,
                2: variant(1, resource),
                3: blake3.blake3(resource).digest(),
            },
        ],
    }

    bundle = ProjectBundle.from_project_file_manifest(project_file, manifest)
    bundle.compatibility = reference_identity()

    assert bundle.project_file == project_file
    assert bundle.identity()[0] == 7
    assert bundle.resource_bytes("resources/a.png", blake3.blake3(resource).digest()) == resource
    with pytest.raises(RuntimeError, match="packaged project"):
        bundle.rescan()


def test_project_scan_reports_completed_file_counts(tmp_path: Path) -> None:
    (tmp_path / "main.erb").write_text("@SYSTEM_TITLE\nRETURN\n", encoding="utf-8")
    (tmp_path / "variables.csv").write_text("FLAG,1\n", encoding="utf-8")
    observed: list[tuple[int, int]] = []

    ProjectBundle.scan(
        tmp_path,
        progress=lambda completed, total: observed.append((completed, total)),
    )

    assert observed[:2] == [(0, 0), (0, 2)]
    assert observed[-1] == (2, 2)
    assert all(left[0] <= right[0] for left, right in zip(observed, observed[1:], strict=False))


def test_project_scanners_submit_nested_sprite_manifests_and_images(tmp_path: Path) -> None:
    (tmp_path / "CSV").mkdir()
    portraits = tmp_path / "resources" / "剧情肖像"
    portraits.mkdir(parents=True)
    (tmp_path / "CSV" / "GAMEBASE.CSV").write_text("コード,1\n", encoding="utf-8")
    manifest = "\ufeff萝乐娜_泣,Rorona-portraits.webp,1040,1182,1000,1125\n"
    image = b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\xe7\x03\x00\x64\x04\x00"
    (portraits / "Portraits.csv").write_bytes(manifest.encode("utf-8"))
    (portraits / "Rorona-portraits.webp").write_bytes(image)

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    resource_manifest = scanned.files["resources/剧情肖像/Portraits.csv"]
    resource_image = scanned.files["resources/剧情肖像/Rorona-portraits.webp"]
    assert resource_manifest.category == FILE_RESOURCE_MANIFEST
    assert resource_manifest.payload == variant(0, manifest.removeprefix("\ufeff"))
    assert resource_image.category == FILE_RESOURCE
    assert resource_image.payload == variant(
        3,
        {0: len(image), 1: {0: 1000, 1: 1125, 2: "webp", 3: False}},
    )
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_include_only_audio_from_the_sound_directory(tmp_path: Path) -> None:
    sound = tmp_path / "sound"
    sound.mkdir()
    (sound / "theme.mp3").write_bytes(b"audio")
    (sound / "cover.png").write_bytes(b"image")
    (sound / "ignored.erb").write_text("@IGNORED", encoding="utf-8")
    (sound / "ignored.csv").write_text("IGNORED,1", encoding="utf-8")
    (sound / "ignored.config").write_text("IGNORED:YES", encoding="utf-8")

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    assert list(scanned.files) == ["sound/theme.mp3"]
    assert scanned.files["sound/theme.mp3"].category == FILE_RESOURCE
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_include_supported_fonts_as_binary_resources(tmp_path: Path) -> None:
    fonts = tmp_path / "FoNt"
    fonts.mkdir()
    for name in (
        "regular.ttf",
        "display.otf",
        "collection.ttc",
        "web.woff",
        "web2.woff2",
    ):
        (fonts / name).write_bytes(name.encode())
    (fonts / "license.txt").write_text("font license", encoding="utf-8")

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    assert list(scanned.files) == [
        "FoNt/collection.ttc",
        "FoNt/display.otf",
        "FoNt/license.txt",
        "FoNt/regular.ttf",
        "FoNt/web.woff",
        "FoNt/web2.woff2",
    ]
    assert all(file.category == FILE_RESOURCE for file in scanned.files.values())
    assert quick.materialize().manifest() == scanned.manifest()


def test_project_scanners_share_the_cross_frontend_cache_contract(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    sound = tmp_path / "sound"
    fonts = tmp_path / "font"
    nested = tmp_path / "sub"
    private = tmp_path / ".RUSTYERA" / "cache"
    for directory in (resources, sound, fonts, nested, private):
        directory.mkdir(parents=True)
    decomposed = "e\u0301.png"
    (resources / decomposed).write_bytes(b"png")
    manifest = (
        f"FACE, \t{decomposed} \t\r\n"
        "ANIME, \tAnImE\t \n"
        f"NOTE,\u00a0{decomposed}\u00a0\r"
        "META,a\u0085b"
    )
    (resources / "sprites.csv").write_text(manifest, encoding="utf-8", newline="")
    (sound / "theme.MP3").write_bytes(b"audio")
    (fonts / "Project.ttf").write_bytes(b"font")
    (sound / "ignored.erb").write_text("@IGNORED", encoding="utf-8")
    (private / "ignored.erb").write_text("@PRIVATE", encoding="utf-8")
    (tmp_path / "reraconfig.toml").write_text("[display]\nfont_size = 20\n", encoding="utf-8")
    (nested / "reraconfig.toml").write_bytes(b"\x82\xa0\n")
    (tmp_path / "é.erb").write_text("@ACCENTED\nRETURN\n", encoding="utf-8")
    (tmp_path / "z.erb").write_text("@ASCII\nRETURN\n", encoding="utf-8")

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    assert list(scanned.files) == [
        "font/Project.ttf",
        "reraconfig.toml",
        "resources/sprites.csv",
        "resources/é.png",
        "sound/theme.MP3",
        "sub/reraconfig.toml",
        "z.erb",
        "é.erb",
    ]
    assert [file.category for file in scanned.files.values()] == [4, 5, 3, 4, 4, 5, 2, 2]
    assert scanned.files["resources/sprites.csv"].payload == variant(
        0,
        "FACE, \té.png \t\r\nANIME, \tAnImE\t \nNOTE,\u00a0é.png\u00a0\rMETA,a\u0085b",
    )
    assert scanned.files["sub/reraconfig.toml"].payload == variant(0, "あ\n")
    assert scanned.identity()[1].hex() == (
        "2554d3820c88d26cf3ddd33ba9896e9cc6397ce28669772cd0abd60539b2ae2b"
    )
    assert quick.identity() == scanned.identity()
    assert quick.materialize().identity() == scanned.identity()


def test_project_scanners_normalize_resource_paths_and_manifests_to_nfc(tmp_path: Path) -> None:
    resources = tmp_path / "RESOURCES"
    resources.mkdir()
    image_name = "CIOバニー巨.png"
    decomposed_name = unicodedata.normalize("NFD", image_name)
    (resources / "sprites.csv").write_bytes(f"FACE,{decomposed_name}\n".encode())
    (resources / decomposed_name).write_bytes(b"png")

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    image_path = f"RESOURCES/{image_name}"
    assert image_path in scanned.files
    assert scanned.files["RESOURCES/sprites.csv"].payload == variant(0, f"FACE,{image_name}\n")
    assert quick.materialize().identity() == scanned.identity()


def test_project_wire_limits_expand_from_scanned_content_size(tmp_path: Path) -> None:
    bundle = ProjectBundle(
        tmp_path,
        1,
        {
            "large.erb": ProjectFile(
                "large.erb",
                2,
                None,
                b"\x00" * 32,
                200 * 1024 * 1024,
            )
        },
    )
    bundle.compatibility = reference_identity()

    maximum_envelope, maximum_payload = bundle.requested_wire_limits()

    assert maximum_payload >= 200 * 1024 * 1024
    assert maximum_envelope > maximum_payload


def test_project_wire_limits_exclude_lazy_resource_bodies(tmp_path: Path) -> None:
    bundle = ProjectBundle(
        tmp_path,
        1,
        {
            "resources/large.png": ProjectFile(
                "resources/large.png",
                FILE_RESOURCE,
                variant(3, {0: 900 * 1024 * 1024, 1: None}),
                b"\x01" * 32,
                900 * 1024 * 1024,
            )
        },
    )
    bundle.compatibility = reference_identity()

    maximum_envelope, maximum_payload = bundle.requested_wire_limits()

    assert maximum_envelope == project_module.DEFAULT_MAXIMUM_ENVELOPE_BYTES
    assert maximum_payload == project_module.DEFAULT_MAXIMUM_PAYLOAD_BYTES


def test_project_scanners_normalize_cp932_sources_to_utf8(tmp_path: Path) -> None:
    source = "サブディレクトリを検索する:YES\r\n"
    path = tmp_path / "_fixed.config"
    path.write_bytes(source.encode("cp932"))

    scanned = ProjectBundle.scan(tmp_path)
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

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
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

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
    bundle.compatibility = reference_identity()

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
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(resources)
    quick.compatibility = reference_identity()

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
    scanned.compatibility = reference_identity()
    quick = ProjectBundle.scan_quick(tmp_path)
    quick.compatibility = reference_identity()

    expected = ["CSV/GAMEBASE.CSV", "emuera.config", "ERB/GUIDE/main.erb", "resources/notes.txt"]
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
    bundle.compatibility = reference_identity()
    source.write_text("@B", encoding="utf-8")

    candidate, request = bundle.reload_file(resources / "ERB" / "main.erb")

    assert candidate.files["ERB/main.erb"].payload == variant(0, "@B")
    assert unwrap_variant(request[2][0])[1][0][0] == "ERB/main.erb"
