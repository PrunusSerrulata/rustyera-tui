"""Versioned global and per-project client preference persistence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import blake3

from .project import ProjectBundle

SCHEMA_VERSION = 1
PROFILE = "tui"
PREFERENCES_FILE = "preferences-v1.json"


@dataclass(frozen=True, slots=True)
class ClientPreferenceValues:
    image_scale: float | None = None
    master_volume: float | None = None
    trust_project_file_metadata: bool | None = None

    @classmethod
    def from_json(cls, value: object) -> ClientPreferenceValues:
        if not isinstance(value, dict) or set(value) - {
            "imageScale",
            "masterVolume",
            "trustProjectFileMetadata",
        }:
            raise ValueError("client 分区包含未知字段或不是对象")
        image_scale = value.get("imageScale")
        master_volume = value.get("masterVolume")
        trust = value.get("trustProjectFileMetadata")
        if image_scale is not None and (
            isinstance(image_scale, bool)
            or not isinstance(image_scale, (int, float))
            or not 0.25 <= float(image_scale) <= 4.0
        ):
            raise ValueError("imageScale 必须是 0.25 到 4.0 之间的数字")
        if master_volume is not None and (
            isinstance(master_volume, bool)
            or not isinstance(master_volume, (int, float))
            or not 0.0 <= float(master_volume) <= 1.0
        ):
            raise ValueError("masterVolume 必须是 0.0 到 1.0 之间的数字")
        if trust is not None and not isinstance(trust, bool):
            raise ValueError("trustProjectFileMetadata 必须是布尔值")
        return cls(
            None if image_scale is None else float(image_scale),
            None if master_volume is None else float(master_volume),
            trust,
        )

    def to_json(self) -> dict[str, float | bool]:
        result: dict[str, float | bool] = {}
        if self.image_scale is not None:
            result["imageScale"] = self.image_scale
        if self.master_volume is not None:
            result["masterVolume"] = self.master_volume
        if self.trust_project_file_metadata is not None:
            result["trustProjectFileMetadata"] = self.trust_project_file_metadata
        return result


@dataclass(frozen=True, slots=True)
class PreferenceValues:
    settings: dict[str, str]
    client: ClientPreferenceValues = ClientPreferenceValues()


@dataclass(frozen=True, slots=True)
class LoadedPreferences:
    path: Path
    values: PreferenceValues
    read_only: bool = False
    error: str | None = None


def _config_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RustyEra TUI"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "RustyEra TUI"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rustyera-tui"


def _data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RustyEra TUI"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RustyEra TUI"
        )
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "rustyera-tui"


def global_preferences_path() -> Path:
    return _config_root() / PREFERENCES_FILE


def project_preferences_path(bundle: ProjectBundle) -> Path:
    if bundle.project_file is None:
        return bundle.root / ".rustyera" / PREFERENCES_FILE
    canonical = bundle.project_file.expanduser().resolve(strict=False)
    normalized = (
        canonical.as_posix().casefold() if sys.platform == "win32" else canonical.as_posix()
    )
    identity = blake3.blake3(normalized.encode("utf-8")).hexdigest()
    return _data_root() / "project-preferences" / identity / PREFERENCES_FILE


def _empty_document() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "profiles": {}}


def _read_document(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_document(), None
    except OSError as error:
        return _empty_document(), f"无法读取偏好文件 {path}：{error}"
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        return _empty_document(), f"偏好文件格式无效 {path}：{error}"
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        return _empty_document(), f"偏好文件版本不受支持：{path}"
    if not isinstance(document.get("profiles"), dict):
        return _empty_document(), f"偏好文件 profiles 无效：{path}"
    return document, None


def load_preferences(path: Path) -> LoadedPreferences:
    document, error = _read_document(path)
    if error is not None:
        return LoadedPreferences(path, PreferenceValues({}), True, error)
    profile = document["profiles"].get(PROFILE, {})
    if not isinstance(profile, dict):
        return LoadedPreferences(
            path,
            PreferenceValues({}),
            True,
            f"偏好文件中的 {PROFILE} 分区无效：{path}",
        )
    if set(profile) - {"settings", "client"}:
        return LoadedPreferences(
            path,
            PreferenceValues({}),
            True,
            f"偏好文件中的 {PROFILE} 分区包含未知字段：{path}",
        )
    settings = profile.get("settings", {})
    client = profile.get("client", {})
    if not isinstance(settings, dict) or not all(
        isinstance(code, str) and isinstance(value, str) for code, value in settings.items()
    ):
        return LoadedPreferences(
            path,
            PreferenceValues({}),
            True,
            f"偏好文件中的 {PROFILE} 数据无效：{path}",
        )
    try:
        client_values = ClientPreferenceValues.from_json(client)
    except ValueError as error:
        return LoadedPreferences(
            path,
            PreferenceValues({}),
            True,
            f"偏好文件中的 {PROFILE} 数据无效：{path}：{error}",
        )
    writable_parent = path.parent
    while not writable_parent.exists() and writable_parent != writable_parent.parent:
        writable_parent = writable_parent.parent
    read_only = not os.access(writable_parent, os.W_OK)
    return LoadedPreferences(
        path,
        PreferenceValues(dict(settings), client_values),
        read_only,
        f"偏好位置不可写：{path}" if read_only else None,
    )


def save_preferences(loaded: LoadedPreferences, values: PreferenceValues) -> LoadedPreferences:
    if loaded.read_only:
        raise PermissionError(loaded.error or "偏好文件为只读")
    document, error = _read_document(loaded.path)
    if error is not None:
        raise ValueError(error)
    profiles = document["profiles"]
    profiles[PROFILE] = {
        "settings": dict(sorted(values.settings.items())),
        "client": values.client.to_json(),
    }
    parent = loaded.path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".preferences-", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, loaded.path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return LoadedPreferences(loaded.path, values)
