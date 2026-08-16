import json
import unicodedata
import threading
import time
from pathlib import Path

import blake3
import pytest
import rustyera_tui.project as project_module

from rustyera_tui.project import (
    FILE_ERB,
    FILE_RESOURCE,
    FILE_RESOURCE_MANIFEST,
    IO_CONFLICT,
    ProjectBundle,
    ProjectFile,
    SOURCE_INDEX_VERSION,
    StorageBackend,
)
from rustyera_tui.wire import decode, unwrap_variant, variant


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


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
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")

    bundle = ProjectBundle.scan(tmp_path)
    assert list(bundle.files) == ["CSV/Abl.csv", "ERB/main.erb"]
    manifest = bundle.manifest()
    assert manifest[0] == 1
    csv = manifest[1][0]
    assert csv[2] == variant(0, "0,test\n")
    assert csv[3] == blake3.blake3(b"0,test\n").digest()


def test_project_scanner_uses_lowercase_instead_of_casefold_for_path_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "ß.erb").write_text("@SHARP_S\nRETURN\n", encoding="utf-8")
    (tmp_path / "st.erb").write_text("@ST\nRETURN\n", encoding="utf-8")

    bundle = ProjectBundle.scan(tmp_path)

    assert list(bundle.files) == ["st.erb", "ß.erb"]


def test_project_bundle_uses_embedded_project_file_resources(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"container")
    source = "@SYSTEM_TITLE\nRETURN\n"
    resource = b"image"
    manifest = {
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
    quick = ProjectBundle.scan_quick(tmp_path)

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
    quick = ProjectBundle.scan_quick(tmp_path)

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
    (fonts / "license.txt").write_text("not packaged", encoding="utf-8")

    scanned = ProjectBundle.scan(tmp_path)
    quick = ProjectBundle.scan_quick(tmp_path)

    assert list(scanned.files) == [
        "FoNt/collection.ttc",
        "FoNt/display.otf",
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
    quick = ProjectBundle.scan_quick(tmp_path)

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
    quick = ProjectBundle.scan_quick(tmp_path)

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

    maximum_envelope, maximum_payload = bundle.requested_wire_limits()

    assert maximum_envelope == project_module.DEFAULT_MAXIMUM_ENVELOPE_BYTES
    assert maximum_payload == project_module.DEFAULT_MAXIMUM_PAYLOAD_BYTES


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


def test_quick_scan_reloads_only_the_selected_file(tmp_path: Path) -> None:
    (tmp_path / "ERB").mkdir()
    selected = tmp_path / "ERB" / "selected.erb"
    unselected = tmp_path / "ERB" / "unselected.erb"
    selected.write_text("@SELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    unselected.write_text("@UNSELECTED\nPRINTL v1\nRETURN\n", encoding="utf-8")
    ProjectBundle.scan_quick(tmp_path)
    bundle = ProjectBundle.scan_quick(tmp_path)
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

    assert migrated.scan_metrics.source_files_reused == 1
    assert migrated.scan_metrics.source_files_hashed == 0
    canonical = json.loads(index_path.read_text(encoding="utf-8"))
    assert canonical["version"] == SOURCE_INDEX_VERSION
    assert canonical["files"]["main.erb"]["category"] == FILE_ERB
    assert isinstance(canonical["files"]["main.erb"]["signature"], str)

    source.write_text("@SYSTEM_TITLE\nPRINTL CHANGED\nRETURN\n", encoding="utf-8")
    updated = ProjectBundle.scan_quick(tmp_path)
    repeated = ProjectBundle.scan_quick(tmp_path)

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
        {0: 7, 1: [{0: "main.erb", 1: FILE_ERB, 2: variant(0, ""), 3: digest}]},
    )
    full = ProjectBundle(
        tmp_path,
        7,
        {
            "main.erb": ProjectFile(
                "main.erb", FILE_ERB, variant(0, "@SYSTEM_TITLE\nRETURN\n"), digest
            )
        },
    )

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


def test_configuration_write_is_atomic_and_detects_external_changes(tmp_path: Path) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[display]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None

    bundle.write_configuration(digest, "[display]\nfont_size = 20\n")
    assert config.read_text(encoding="utf-8") == "[display]\nfont_size = 20\n"

    with pytest.raises(RuntimeError, match="其他程序修改"):
        bundle.write_configuration(digest, "[display]\nfont_size = 22\n")


def test_configuration_creation_is_idempotent_and_requires_utf8(tmp_path: Path) -> None:
    bundle = ProjectBundle.scan(tmp_path)
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
    captured: list[tuple[bytes, bytes, str]] = []

    def prepare(project: bytes, expected: bytes, contents: str) -> tuple[int, bytes]:
        captured.append((project, expected, contents))
        return 4, b"journal"

    bundle.write_configuration(b"digest", "[display]\nfont_size = 20\n", prepare)

    assert captured == [(b"base-incomplete", b"digest", "[display]\nfont_size = 20\n")]
    assert project_file.read_bytes() == b"basejournal"
