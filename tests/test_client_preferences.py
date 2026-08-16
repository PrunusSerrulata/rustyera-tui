from __future__ import annotations

import json
from pathlib import Path

from rustyera_tui.client_preferences import (
    ClientPreferenceValues,
    LoadedPreferences,
    PreferenceValues,
    load_preferences,
    save_preferences,
)


def test_preference_file_preserves_other_client_profiles_and_round_trips_sparse_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preferences-v1.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "profiles": {"browser": {"settings": {"UseMenu": "NO"}}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_preferences(path)
    saved = save_preferences(
        loaded,
        PreferenceValues(
            {"UseMouse": "NO"},
            ClientPreferenceValues(master_volume=0.5, trust_project_file_metadata=True),
        ),
    )

    assert saved.values.settings == {"UseMouse": "NO"}
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["profiles"]["browser"]["settings"] == {"UseMenu": "NO"}
    assert document["profiles"]["tui"] == {
        "settings": {"UseMouse": "NO"},
        "client": {"masterVolume": 0.5, "trustProjectFileMetadata": True},
    }


def test_unknown_or_malformed_preference_file_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "preferences-v1.json"
    path.write_text('{"schemaVersion": 2, "profiles": {}}', encoding="utf-8")

    loaded = load_preferences(path)

    assert loaded.read_only
    assert loaded.values == PreferenceValues({})
    assert loaded.error is not None


def test_read_only_loaded_preferences_refuse_overwrite(tmp_path: Path) -> None:
    loaded = LoadedPreferences(
        tmp_path / "preferences-v1.json",
        PreferenceValues({}),
        read_only=True,
        error="future schema",
    )

    try:
        save_preferences(loaded, PreferenceValues({"UseMouse": "NO"}))
    except PermissionError as error:
        assert "future schema" in str(error)
    else:
        raise AssertionError("read-only preferences were overwritten")


def test_invalid_active_profile_auxiliary_values_are_read_only(tmp_path: Path) -> None:
    path = tmp_path / "preferences-v1.json"
    path.write_text(
        '{"schemaVersion":1,"profiles":{"tui":{"settings":{},"client":{"masterVolume":2}}}}',
        encoding="utf-8",
    )

    loaded = load_preferences(path)

    assert loaded.read_only
    assert loaded.values == PreferenceValues({})
    assert "masterVolume" in (loaded.error or "")


def test_unknown_active_profile_fields_are_read_only_but_other_profiles_are_opaque(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preferences-v1.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "profiles": {
                    "browser": {"future": {"nested": True}},
                    "tui": {"settings": {}, "client": {}, "future": True},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_preferences(path)

    assert loaded.read_only
    assert "未知字段" in (loaded.error or "")
    original = path.read_bytes()
    try:
        save_preferences(loaded, PreferenceValues({"UseMouse": "NO"}))
    except PermissionError:
        pass
    else:
        raise AssertionError("unknown active profile was overwritten")
    assert path.read_bytes() == original
