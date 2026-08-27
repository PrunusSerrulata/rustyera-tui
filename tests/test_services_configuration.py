from __future__ import annotations

from compatibility_test_support import reference_identity

from services_test_support import (
    Any,
    ConfigurationChange,
    ConfigurationSnapshot,
    FrontendEvent,
    Path,
    PendingConfigurationPrepare,
    ProjectBundle,
    SimpleNamespace,
    blake3,
    client_with_capture,
    variant,
)
from rustyera_tui.client_preferences import LoadedPreferences, PreferenceValues


def test_configuration_write_does_not_authorize_uncommitted_resource_changes(tmp_path: Path) -> None:
    from rustyera_tui.project import StorageBackend

    (tmp_path / "seed.xml").write_bytes(b"old")
    bundle = ProjectBundle.scan(tmp_path)
    bundle.compatibility = reference_identity()
    client, captured = client_with_capture()
    client.bundle = bundle
    client.storage = StorageBackend(tmp_path, resource_bundle=bundle)
    (tmp_path / "seed.xml").write_bytes(b"new")
    (tmp_path / "added.xml").write_bytes(b"added")
    client.bundle = client._write_configuration(bundle, b"", "[meta]\nschema_version = 4\n")
    assert "added.xml" not in client.bundle.files
    assert client.bundle.files["seed.xml"].content_hash == bundle.files["seed.xml"].content_hash
    client._handle_storage({0: 1, 1: 5, 2: "seed.xml", 3: variant(0)}, None)
    assert captured[-1][1][1][1][0][0] == 7
    client._handle_storage({0: 2, 1: 5, 2: "added.xml", 3: variant(0)}, None)
    assert captured[-1][1][1][1][0][0] == 1


def test_configuration_update_uses_authoritative_snapshot_and_open_effect_is_supported() -> None:
    client, captured = client_with_capture()
    client.bundle = SimpleNamespace(project_file=None)
    client.pending_configuration = None
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: b"digest", 2: [], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("FontSize", "20")])
    assert captured.pop() == (
        24,
        {0: 7, 1: b"digest", 2: [{0: "FontSize", 1: "20"}]},
    )
    assert isinstance(client.pending_configuration, PendingConfigurationPrepare)
    assert client.pending_configuration.message_id == 1

    client._acknowledge_effects({0: [{0: 41, 1: variant(4)}]})
    assert client.events.get_nowait() == FrontendEvent("open_configuration")
    assert captured.pop() == (43, {0: [{0: 41, 1: 0}]})


def test_configuration_update_requires_the_negotiated_tui_profile() -> None:
    client, _captured = client_with_capture()
    client.bundle = SimpleNamespace(project_file=None)
    client.configuration_profile_supported = False
    client.pending_configuration = None
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: b"digest", 2: [], 3: False}
    )

    try:
        client.prepare_configuration_update([])
    except RuntimeError as error:
        assert "不支持 TUI" in str(error)
    else:
        raise AssertionError("an unsupported configuration profile was accepted")


def test_global_preferences_save_without_an_open_project(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.global_preferences = LoadedPreferences(
        tmp_path / "global" / "preferences-v1.json", PreferenceValues({})
    )
    client.project_preferences = None
    client.configuration_snapshot = None
    client.pending_client_preferences = None
    client.pending_client_preferences_save = False

    client.save_client_preferences("global", PreferenceValues({"UseMouse": "NO"}))

    assert captured == []
    assert client.global_preferences.values.settings == {"UseMouse": "NO"}
    assert client.global_preferences.path.is_file()
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait().kind)
    assert events == ["client_preferences_loaded", "client_preferences_applied"]


def test_preference_apply_correlation_survives_interleaved_game_state(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.global_preferences = LoadedPreferences(
        tmp_path / "global" / "preferences-v1.json", PreferenceValues({})
    )
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {
            0: 7,
            1: b"digest",
            2: [
                {
                    0: "UseMouse",
                    1: "UseMouse",
                    2: "UseMouse",
                    3: "YES",
                    4: 0,
                    5: [],
                    6: False,
                    7: 2,
                    8: "YES",
                    9: "YES",
                    10: 0,
                    11: True,
                    12: "YES",
                }
            ],
            3: False,
        }
    )
    client.startup_active = False
    wait = {0: {0: 7, 1: 3}}
    client.active_wait = wait

    client.save_client_preferences("global", PreferenceValues({"UseMouse": "NO"}))
    assert captured[-1][0] == 28
    client._handle_client_preferences_applied({0: {0: 7, 1: b"digest", 2: [], 3: False}}, 99)
    assert client.pending_client_preferences == 1

    client._handle_runtime(21, {0: 5, 2: 9}, None)
    client._handle_client_preferences_applied({0: {0: 7, 1: b"digest", 2: [], 3: False}}, 1)

    assert (client.phase, client.epoch, client.active_wait) == (5, 9, wait)
    assert client.pending_client_preferences is None
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait().kind)
    assert "client_preferences_applied" in events


def test_startup_preference_rejection_falls_back_without_aborting_the_game() -> None:
    client, captured = client_with_capture()
    client.pending_client_preferences = 7
    client.pending_client_preferences_save = False
    client.pending_start_after_preferences = False
    client.startup_active = False

    client._handle_command_rejection({0: 1, 1: "invalid preference"}, 7)

    assert client.pending_client_preferences is None
    assert client.pending_start_after_preferences is None
    assert captured[-1] == (20, {0: variant(0, None)})
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert any(event.kind == "log" and "客户端偏好未应用" in str(event.value) for event in events)
    assert not any(event.kind == "runtime_error" for event in events)


def test_saved_preference_rejection_preserves_interleaved_game_state() -> None:
    client, _captured = client_with_capture()
    client.pending_client_preferences = 8
    client.pending_client_preferences_save = True
    client.pending_start_after_preferences = None
    client.phase = 5
    client.epoch = 12
    client.active_wait = {0: {0: 7, 1: 4}}

    client._handle_command_rejection({0: 1, 1: "save denied"}, 8)

    assert (client.phase, client.epoch, client.active_wait) == (5, 12, {0: {0: 7, 1: 4}})
    assert client.pending_client_preferences is None
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert any(
        event.kind == "client_preferences_save_failed" and event.value == "save denied"
        for event in events
    )
    assert not any(event.kind == "runtime_error" for event in events)


def test_packaged_configuration_commits_through_the_append_update(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"base")
    bundle = ProjectBundle(tmp_path, 7, {}, project_file, compatibility=reference_identity())
    client.bundle = bundle
    client.pending_configuration = None
    digest = b"package-digest"
    hot_entry = {
        0: "UseMouse",
        1: "UseMouse",
        2: "UseMouse",
        3: "YES",
        4: 0,
        5: [],
        6: False,
        7: 2,
        8: "YES",
        9: "YES",
        10: 0,
        11: True,
        12: "YES",
    }
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 7, 1: digest, 2: [hot_entry], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("UseMouse", "NO")])
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    contents = "UseMouse:NO\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client.abi.prepare_project_configuration_update = lambda *_args: (4, b"journal")
    client.abi.project_file_manifest = lambda _bytes: {
        0: 7,
        2: reference_identity(),
        1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, contents), 3: prepared_digest}],
    }
    client._handle_configuration_prepared(
        {0: 7, 1: digest, 2: contents, 3: False, 4: prepared_digest}, pending.message_id
    )
    assert captured[-1] == (26, {0: pending.message_id, 1: 1})
    client._handle_configuration_committed(
        {
            0: {
                0: 7,
                1: prepared_digest,
                2: [{**hot_entry, 3: "NO", 9: "NO"}],
                3: False,
            }
        },
        1,
    )

    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert FrontendEvent("configuration_saved", (False, False)) in events
    assert project_file.read_bytes() == b"basejournal"
    assert client.bundle is not bundle
    assert client.bundle.identity() != bundle.identity()


def test_packaged_configuration_restarts_with_the_updated_identity(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    project_file = tmp_path / "game.reraproj"
    project_file.write_bytes(b"base")
    old_source = "[save]\nauto_save = true\n"
    old_digest = blake3.blake3(old_source.encode()).digest()
    bundle = ProjectBundle.from_project_file_manifest(
        project_file,
        {
            0: 8,
            2: reference_identity(),
            1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, old_source), 3: old_digest}],
        },
    )
    client.bundle = bundle
    client.pending_configuration = None
    hot_entry = {
        0: "UseMouse",
        1: "UseMouse",
        2: "UseMouse",
        3: "YES",
        4: 0,
        5: [],
        6: False,
        7: 2,
        8: "YES",
        9: "YES",
        10: 0,
        11: True,
        12: "YES",
    }
    restart_entry = {**hot_entry, 0: "AutoSave", 1: "AutoSave", 2: "AutoSave", 10: 1}
    fixed_entry = {**hot_entry, 0: "BackColor", 1: "BackColor", 2: "BackColor", 6: True}
    client.configuration_snapshot = ConfigurationSnapshot.from_wire(
        {0: 8, 1: old_digest, 2: [hot_entry, restart_entry, fixed_entry], 3: False}
    )

    client.prepare_configuration_update([ConfigurationChange("AutoSave", "NO")], True)
    assert captured[0][0] == 24
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    contents = "[save]\nauto_save = false\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client.abi.prepare_project_configuration_update = lambda *_args: (4, b"journal")
    client.abi.project_file_manifest = lambda _bytes: {
        0: 8,
        2: reference_identity(),
        1: [{0: "reraconfig.toml", 1: 5, 2: variant(0, contents), 3: prepared_digest}],
    }
    recreated: list[tuple[ProjectBundle, bytes | None]] = []

    def recreate(candidate: ProjectBundle, **options: Any) -> None:
        recreated.append((candidate, options.get("project_file_bytes")))

    client.recreate = recreate  # type: ignore[method-assign]
    client._handle_configuration_prepared(
        {0: 8, 1: old_digest, 2: contents, 3: True, 4: prepared_digest}, pending.message_id
    )
    client._handle_configuration_committed(
        {0: {0: 8, 1: prepared_digest, 2: [restart_entry], 3: False}}, 1
    )

    assert len(recreated) == 1
    candidate, submitted_project = recreated[0]
    assert candidate.identity() != bundle.identity()
    assert submitted_project is None


def test_prepared_configuration_writes_and_restarts_without_exposing_wire_maps(
    tmp_path: Path,
) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[text]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, True)
    recreated: list[ProjectBundle] = []
    client.recreate = recreated.append  # type: ignore[method-assign]

    contents = "[text]\nfont_size = 20\n"
    prepared_digest = blake3.blake3(contents.encode()).digest()
    client._handle_configuration_prepared(
        {0: 1, 1: digest, 2: contents, 3: False, 4: prepared_digest}, 7
    )

    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 20\n"
    client._handle_configuration_committed({0: {0: 1, 1: prepared_digest, 2: [], 3: True}}, 1)
    assert len(recreated) == 1
    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert FrontendEvent("configuration_saved", (True, True)) in events


def test_generated_reraconfig_is_persisted_idempotently(tmp_path: Path) -> None:
    client, captured = client_with_capture()
    client.bundle = ProjectBundle.scan(tmp_path)
    client.configuration_snapshot = None
    client.pending_configuration = None
    generated = "[meta]\nschema_version = 2\n"
    wire = {0: 1, 1: b"", 2: [], 3: False, 4: generated}

    assert client._publish_configuration(wire) is not None
    assert client._publish_configuration(wire) is not None
    assert (tmp_path / "reraconfig.toml").read_text(encoding="utf-8") == generated
    assert [command for command in captured if command[0] == 24] == [(24, {0: 1, 1: b"", 2: []})]


def test_upgraded_reraconfig_uses_the_original_source_digest(tmp_path: Path) -> None:
    original = "[meta]\nschema_version = 1\n[text]\nfont_size = 20\n"
    generated = "[meta]\nschema_version = 2\n[text]\nfont_size = 20\n"
    (tmp_path / "reraconfig.toml").write_text(original, encoding="utf-8")
    client, captured = client_with_capture()
    client.bundle = ProjectBundle.scan(tmp_path)
    client.pending_configuration = None
    wire = {
        0: 1,
        1: blake3.blake3(original.encode()).digest(),
        2: [],
        3: False,
        4: generated,
    }

    assert client._publish_configuration(wire) is not None
    assert (tmp_path / "reraconfig.toml").read_text(encoding="utf-8") == generated
    pending = client.pending_configuration
    assert isinstance(pending, PendingConfigurationPrepare)
    assert pending.automatic
    generated_digest = blake3.blake3(generated.encode()).digest()
    client._handle_configuration_prepared(
        {0: 1, 1: pending.source_digest, 2: generated, 3: False, 4: generated_digest},
        pending.message_id,
    )
    client.pending_start_after_configuration = False
    client._handle_configuration_committed(
        {0: {0: 1, 1: generated_digest, 2: [], 3: False, 4: None}},
        1,
    )
    assert captured[-1][0] == 28
    client._handle_client_preferences_applied(
        {0: {0: 1, 1: generated_digest, 2: [], 3: False, 4: None}},
        client.pending_client_preferences,
    )
    assert captured[-1] == (20, {0: variant(0, None)})

    client.prepare_configuration_update([ConfigurationChange("FontSize", "22")])
    assert captured[-1] == (
        24,
        {0: 1, 1: generated_digest, 2: [{0: "FontSize", 1: "22"}]},
    )


def test_invalid_or_conflicting_prepared_configuration_keeps_session_alive(
    tmp_path: Path,
) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[text]\nfont_size = 18\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, False)

    client._handle_configuration_prepared({0: "bad"}, 7)
    failure = client.events.get_nowait()
    assert failure.kind == "configuration_save_failed"
    assert "保存偏好选项失败" in failure.value
    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 18\n"
    client._handle_configuration_committed({0: {0: 1, 1: digest, 2: [], 3: False}}, 1)
    assert client.pending_configuration is None
    assert client.events.get_nowait().kind == "configuration"

    config.write_text("[text]\nfont_size = 19\n", encoding="utf-8")
    client.pending_configuration = PendingConfigurationPrepare(8, 1, digest, False)
    contents = "[text]\nfont_size = 20\n"
    client._handle_configuration_prepared(
        {
            0: 1,
            1: digest,
            2: contents,
            3: True,
            4: blake3.blake3(contents.encode()).digest(),
        },
        8,
    )
    conflict = client.events.get_nowait()
    assert conflict.kind == "configuration_save_failed"
    assert "其他程序修改" in conflict.value
    assert config.read_text(encoding="utf-8") == "[text]\nfont_size = 19\n"


def test_invalid_committed_configuration_stops_the_success_path(tmp_path: Path) -> None:
    config = tmp_path / "reraconfig.toml"
    config.write_text("[interaction]\nuse_mouse = true\n", encoding="utf-8")
    bundle = ProjectBundle.scan(tmp_path)
    digest = bundle.files["reraconfig.toml"].content_hash
    assert digest is not None
    client, _captured = client_with_capture()
    client.bundle = bundle
    client.pending_configuration = PendingConfigurationPrepare(7, 1, digest, True)
    recreated: list[ProjectBundle] = []
    client.recreate = recreated.append  # type: ignore[method-assign]
    contents = "[interaction]\nuse_mouse = false\n"
    client._handle_configuration_prepared(
        {
            0: 1,
            1: digest,
            2: contents,
            3: False,
            4: blake3.blake3(contents.encode()).digest(),
        },
        7,
    )

    client._handle_configuration_committed({0: {0: "bad"}}, 1)

    events = []
    while not client.events.empty():
        events.append(client.events.get_nowait())
    assert any(event.kind == "configuration_save_failed" for event in events)
    assert not any(event.kind == "configuration_saved" for event in events)
    assert not recreated


def test_snake_data_names_share_resource_identity_and_preserve_existing_spelling(tmp_path: Path) -> None:
    from rustyera_tui.storage import StorageBackend
    from rustyera_tui.storage_path import normalized_data_path

    source = tmp_path / "plugins" / "café"
    source.mkdir(parents=True)
    (source / "seed.txt").write_bytes(b"source")
    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake", resource_bundle=ProjectBundle.scan(tmp_path))
    data = backend.data_root / "data"
    actual = data / "PlUgIns" / "Cafe\u0301" / "SEED.TXT"
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"overlay")

    def call(path: str, operation: list[Any], namespace: int = 3) -> list[Any]:
        return backend.handle({0: 1, 1: namespace, 2: path, 3: operation})[1]

    for path in ("plugins/CAFÉ/seed.txt", "PLUGINS/cafe\u0301/SEED.TXT"):
        assert call(path, variant(0))[1][0] == b"overlay"
        assert call(path, variant(4))[1][0][0] == 7
        assert call(path, variant(5, 1, 3, None))[1][0] == b"ver"
    listed = call("pLuGiNs", variant(2, "*", True))
    assert [entry[0] for entry in listed[1][0]] == ["PlUgIns/Café/SEED.TXT"]
    assert call(listed[1][0][0][0], variant(0))[1][0] == b"overlay"
    assert call("PLUGINS/café/seed.txt", variant(0), 5)[1][0] == b"source"

    assert call("plugins/CAFÉ/Seed.Txt", variant(1, b"changed", True, variant(0)))[0] == 1
    assert actual.read_bytes() == b"changed"
    assert len(list(actual.parent.iterdir())) == 1
    assert normalized_data_path(data, "New/e\u0301.txt").parts[-1] == "é.txt"
    assert call("New/e\u0301.txt", variant(1, b"new", False, variant(0)))[0] == 1
    assert call("new/É.TXT", variant(0))[1][0] == b"new"
    assert call("PLUGINS/café/seed.txt", variant(3, variant(0)))[0] == 3
    assert not actual.exists()
    assert call("plugins/café/seed.txt", variant(0))[1][0][0] == 0
    assert (source / "seed.txt").read_bytes() == b"source"


def test_snake_data_collision_rejects_every_operation_before_mutation(tmp_path: Path, monkeypatch: Any) -> None:
    from rustyera_tui.storage import StorageBackend

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend.data_root / "data"
    root.mkdir(parents=True)
    actual = root / "seed.txt"
    actual.write_bytes(b"unchanged")
    duplicate = root / "SEED.TXT"
    from rustyera_tui import storage_listing, storage_path

    original_entries = storage_path.directory_entries
    original_resolve = Path.resolve

    # A synthetic case-sensitive directory makes the collision portable to macOS APFS.
    def entries(path: Path):
        if path == root:
            return iter([actual, duplicate])
        return original_entries(path)

    def resolve(path: Path, strict: bool = False):
        return original_resolve(actual if path == duplicate else path, strict=strict)

    monkeypatch.setattr(storage_path, "directory_entries", entries)
    monkeypatch.setattr(storage_listing, "directory_entries", entries)
    monkeypatch.setattr(Path, "resolve", resolve)
    for operation in (
        variant(0), variant(4), variant(5, 0, 1, None),
        variant(1, b"overwrite", True, variant(0)), variant(3, variant(0)),
        variant(2, "*", True),
    ):
        relative = "" if operation[0] == 2 else "seed.txt"
        result = backend.handle({0: 1, 1: 3, 2: relative, 3: operation})[1]
        assert result[0] == 4
        assert result[1][0][0] == 2
        assert actual.read_bytes() == b"unchanged"
    rejected = backend.handle({0: 2, 1: 3, 2: "new/different.txt", 3: variant(1, b"new", False, variant(0))})[1]
    assert rejected[0] == 4
    assert rejected[1][0][0] == 2
    assert not (root / "new").exists()


def test_snake_data_rejects_escaped_links_cycles_and_excessive_paths(tmp_path: Path) -> None:
    from rustyera_tui.storage import StorageBackend

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend.data_root / "data"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "seed.txt").write_bytes(b"private")
    (root / "Escape").symlink_to(outside, target_is_directory=True)
    (root / "Loop").symlink_to(root, target_is_directory=True)
    for relative, kind in (("escape/seed.txt", 1), ("loop/seed.txt", 2), ("a" * 4097, 2), ("/".join(["a"] * 257), 2)):
        for operation in (variant(0), variant(4), variant(1, b"bad", False, variant(0)), variant(3, variant(0))):
            result = backend.handle({0: 1, 1: 3, 2: relative, 3: operation})[1]
            assert result[0] == 4
            assert result[1][0][0] == kind
    assert (outside / "seed.txt").read_bytes() == b"private"


def test_snake_data_lookup_preserves_permission_error_and_scan_budget(tmp_path: Path, monkeypatch: Any) -> None:
    from rustyera_tui.storage import StorageBackend
    from rustyera_tui import storage_path

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    root = backend.data_root / "data"
    root.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"a")
    original_entries = storage_path.directory_entries

    def denied(path: Path):
        if path == root:
            raise PermissionError("denied")
        return original_entries(path)

    monkeypatch.setattr(storage_path, "directory_entries", denied)
    result = backend.handle({0: 1, 1: 3, 2: "a.txt", 3: variant(0)})[1]
    assert result[1][0][0] == 1
    monkeypatch.setattr(storage_path, "directory_entries", original_entries)
    monkeypatch.setattr(storage_path, "MAXIMUM_LOOKUP_ENTRIES", 0)
    result = backend.handle({0: 1, 1: 3, 2: "new.txt", 3: variant(1, b"new", False, variant(0))})[1]
    assert result[1][0][0] == 2
    assert not (root / "new.txt").exists()


def test_reference_data_uses_literal_lookup_without_snake_directory_scans(tmp_path: Path, monkeypatch: Any) -> None:
    from rustyera_tui.storage import StorageBackend

    root = tmp_path / "data"
    root.mkdir()
    original = root / "Literal.TXT"
    original.write_bytes(b"old")
    backend = StorageBackend(tmp_path)

    def no_scan(_path: Path):
        raise AssertionError("reference lookup must not scan normalized directory names")

    from rustyera_tui import storage_listing, storage_path

    monkeypatch.setattr(storage_path, "directory_entries", no_scan)
    monkeypatch.setattr(storage_listing, "directory_entries", no_scan)
    assert backend.handle({0: 1, 1: 3, 2: "Literal.TXT", 3: variant(0)})[1][1][0] == b"old"
    assert backend.handle({0: 2, 1: 3, 2: "Literal.TXT", 3: variant(1, b"new", True, variant(0))})[1][0] == 1
    assert original.read_bytes() == b"new"


def test_snake_data_namespace_link_cannot_reauthorize_an_outside_directory(tmp_path: Path) -> None:
    from rustyera_tui.storage import StorageBackend

    backend = StorageBackend(tmp_path, compatibility_profile="emuera.skia.snake")
    backend.data_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "seed.txt").write_bytes(b"safe")
    (backend.data_root / "data").symlink_to(outside, target_is_directory=True)
    for operation in (variant(0), variant(1, b"bad", False, variant(0)), variant(2, "*", True)):
        result = backend.handle({0: 1, 1: 3, 2: "" if operation[0] == 2 else "seed.txt", 3: operation})[1]
        assert result[0] == 4
        assert result[1][0][0] == 1
    assert (outside / "seed.txt").read_bytes() == b"safe"
