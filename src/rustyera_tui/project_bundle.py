"""Project bundle model and wire projection."""

from __future__ import annotations

import os
import tempfile

from .compatibility import compatibility_identity, compatibility_profile
from .project import (
    Any,
    Callable,
    DEFAULT_MAXIMUM_ENVELOPE_BYTES,
    DEFAULT_MAXIMUM_PAYLOAD_BYTES,
    FILE_RESOURCE,
    MAXIMUM_PROJECT_ENVELOPE_BYTES,
    PROJECT_ENVELOPE_HEADROOM_BYTES,
    PROJECT_FILE_WIRE_OVERHEAD_BYTES,
    Path,
    ProjectFile,
    ProjectScanProgress,
    ProjectScanMetrics,
    PurePosixPath,
    _path_sort_key,
    _payload_size,
    _source_signature,
    _validate_new_project_file,
    blake3,
    dataclass,
    field,
)
from .wire import encode

from .project_bundle_reload import _ProjectBundleReloadMixin
from .project_bundle_scan import _ProjectBundleScanMixin


@dataclass(slots=True)
class ProjectBundle(_ProjectBundleScanMixin, _ProjectBundleReloadMixin):
    root: Path
    revision: int
    files: dict[str, ProjectFile]
    project_file: Path | None = None
    quick_scan_pending: bool = False
    reload_baseline_pending: bool = False
    scan_metrics: ProjectScanMetrics = field(default_factory=ProjectScanMetrics)
    compatibility: dict[int, Any] | None = None
    configuration_digest: bytes | None = None

    def require_compatibility(self) -> dict[int, Any]:
        return compatibility_identity(self.compatibility)

    @property
    def compatibility_profile(self) -> str:
        return compatibility_profile(self.require_compatibility())

    def root_configuration(self) -> dict[int, Any] | None:
        """Read the one configuration payload even when the source index is trusted."""

        from . import project as project_facade

        for relative, item in self.files.items():
            if relative.replace("\\", "/").lower() != "reraconfig.toml":
                continue
            if self.project_file is None:
                fresh = project_facade._stable_read_project_file(
                    self.root, self.root / PurePosixPath(relative), item.category
                )
                if fresh.content_hash != item.content_hash:
                    raise ValueError("项目根配置在扫描后发生变化，请重新打开项目")
                item = fresh
                self.files[relative] = item
            return item.submitted()
        if self.project_file is None and any(
            path.name.casefold() == "reraconfig.toml" for path in self.root.iterdir()
        ):
            raise ValueError("项目根配置在扫描后新增，请重新打开项目")
        return None

    @classmethod
    def from_project_file_manifest(
        cls, project_file: Path, manifest: dict[int, Any]
    ) -> ProjectBundle:
        resolved = project_file.expanduser().resolve(strict=True)
        if resolved.suffix.lower() != ".reraproj":
            raise ValueError("project file must use the .reraproj extension")
        revision = manifest.get(0)
        submitted = manifest.get(1)
        if not isinstance(revision, int) or not isinstance(submitted, list):
            raise ValueError("project file contains an invalid manifest")
        files: dict[str, ProjectFile] = {}
        for value in submitted:
            if not isinstance(value, dict):
                raise ValueError("project file contains an invalid file entry")
            relative = value.get(0)
            category = value.get(1)
            payload = value.get(2)
            content_hash = value.get(3)
            if (
                not isinstance(relative, str)
                or not isinstance(category, int)
                or not isinstance(payload, list)
                or (content_hash is not None and not isinstance(content_hash, bytes))
            ):
                raise ValueError("project file contains an invalid file entry")
            files[relative] = ProjectFile(
                relative_path=relative,
                category=category,
                payload=payload,
                content_hash=content_hash,
                content_size=_payload_size(payload),
            )
        return cls(
            resolved.parent,
            revision,
            files,
            resolved,
            compatibility=compatibility_identity(manifest.get(2)),
            configuration_digest=next(
                (
                    item.content_hash
                    for path, item in files.items()
                    if path.casefold() == "reraconfig.toml"
                ),
                None,
            ),
        )

    @property
    def is_materialized(self) -> bool:
        return not self.quick_scan_pending and all(
            item.payload is not None for item in self.files.values()
        )

    def identity(self) -> dict[int, Any]:
        hasher = blake3.blake3(derive_key_context="rustyera.project-source-identity.v1")
        ordered = sorted(self.files.values(), key=lambda item: _path_sort_key(item.relative_path))
        for item in ordered:
            digest = item.content_hash
            if digest is None and item.payload is not None and item.payload[0] == 2:
                digest = blake3.blake3(str(item.payload[1][0][1]).encode("utf-8")).digest()
            if digest is None:
                raise RuntimeError(f"project file {item.relative_path} has no content hash")
            path = item.relative_path.encode("utf-8")
            hasher.update(len(path).to_bytes(8, "little"))
            hasher.update(path)
            hasher.update(bytes([item.category]))
            hasher.update(digest)
        return {
            0: self.revision,
            1: hasher.digest(),
            2: self.require_compatibility(),
            3: self.configuration_digest,
        }

    def manifest(self) -> dict[int, Any]:
        if not self.is_materialized:
            raise RuntimeError("project source payloads have not been materialized")
        ordered = sorted(self.files.values(), key=lambda item: _path_sort_key(item.relative_path))
        return {
            0: self.revision,
            1: [item.submitted() for item in ordered],
            2: self.require_compatibility(),
        }

    def write_full_manifest_temp(
        self,
        progress: ProjectScanProgress | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[Path, int]:
        """Spool a canonical full manifest while retaining at most one 4 MiB resource chunk."""

        bundle = self.materialize(progress, cancelled)
        ordered = sorted(bundle.files.values(), key=lambda item: _path_sort_key(item.relative_path))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="rustyera-full-manifest-", suffix=".cbor"
        )
        temporary = Path(temporary_name)
        written = 0

        def write(stream: Any, data: bytes) -> None:
            nonlocal written
            if written + len(data) > MAXIMUM_PROJECT_ENVELOPE_BYTES:
                raise ValueError("full project manifest exceeds the 1 GiB transfer limit")
            if stream.write(data) != len(data):
                raise OSError("full project manifest was not written completely")
            written += len(data)

        try:
            with os.fdopen(descriptor, "wb") as stream:
                write(stream, b"\xa3\x00")
                write(stream, encode(bundle.revision))
                write(stream, b"\x01" + _cbor_container_header(4, len(ordered)))
                for item in ordered:
                    if cancelled is not None and cancelled():
                        raise InterruptedError("project export was cancelled")
                    external = (
                        item.category == FILE_RESOURCE
                        and item.payload is not None
                        and item.payload[0] == 3
                    )
                    if not external:
                        write(stream, encode(item.submitted()))
                        continue
                    write(
                        stream,
                        _cbor_container_header(5, 3 + (item.content_hash is not None))
                        + b"\x00"
                        + encode(item.relative_path)
                        + b"\x01"
                        + encode(item.category)
                        + b"\x02\x82\x01\x81"
                        + _cbor_container_header(2, item.content_size),
                    )
                    source_path = item.source_path or self.root / PurePosixPath(item.relative_path)
                    _validate_new_project_file(self.root, source_path, item.category)
                    before = _source_signature(source_path)
                    if item.source_signature is not None and before != item.source_signature:
                        raise ValueError(
                            f"image resource {item.relative_path} changed after project scan"
                        )
                    hasher = blake3.blake3()
                    resource_bytes = 0
                    with source_path.open("rb") as resource:
                        while chunk := resource.read(4 * 1024 * 1024):
                            if cancelled is not None and cancelled():
                                raise InterruptedError("project export was cancelled")
                            hasher.update(chunk)
                            resource_bytes += len(chunk)
                            write(stream, chunk)
                    if (
                        resource_bytes != item.content_size
                        or hasher.digest() != item.content_hash
                        or _source_signature(source_path) != before
                    ):
                        raise ValueError(
                            f"image resource {item.relative_path} changed after project scan"
                        )
                    if item.content_hash is not None:
                        write(stream, b"\x03" + encode(item.content_hash))
                write(stream, b"\x02" + encode(bundle.require_compatibility()))
                stream.flush()
                os.fsync(stream.fileno())
            return temporary, written
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def requested_wire_limits(self) -> tuple[int, int]:
        """Return a conservative one-envelope project submission budget."""

        payload_bytes = sum(
            (0 if item.category == FILE_RESOURCE else item.content_size)
            + len(item.relative_path.encode("utf-8"))
            + PROJECT_FILE_WIRE_OVERHEAD_BYTES
            for item in self.files.values()
        )
        envelope_bytes = payload_bytes + PROJECT_ENVELOPE_HEADROOM_BYTES
        requested_payload = max(DEFAULT_MAXIMUM_PAYLOAD_BYTES, payload_bytes)
        requested_envelope = max(DEFAULT_MAXIMUM_ENVELOPE_BYTES, envelope_bytes)
        if requested_envelope > MAXIMUM_PROJECT_ENVELOPE_BYTES:
            raise ValueError(
                "project submission exceeds the frontend's 1 GiB envelope safety limit"
            )
        return requested_envelope, requested_payload

    def resource_bytes(self, resource_id: str, content_digest: bytes) -> bytes:
        item = self._resource_file(resource_id)
        if item is None:
            raise ValueError(f"unknown image resource {resource_id}")
        tag = item.payload[0] if item.payload is not None and len(item.payload) == 2 else None
        if item.payload is None:
            source_path = item.source_path
            if source_path is None:
                pure = PurePosixPath(item.relative_path)
                source_path = self.root.joinpath(*pure.parts)
            _validate_new_project_file(self.root, source_path, item.category)
            data = source_path.read_bytes()
            if item.content_hash is None or blake3.blake3(data).digest() != item.content_hash:
                raise ValueError(f"image resource {resource_id} changed after project scan")
        elif tag == 1:
            _, fields = item.payload
            if len(fields) != 1 or not isinstance(fields[0], bytes):
                raise ValueError(f"image resource {resource_id} has no binary payload")
            data = fields[0]
        else:
            source_path = item.source_path
            if source_path is None:
                pure = PurePosixPath(item.relative_path)
                source_path = self.root.joinpath(*pure.parts)
            _validate_new_project_file(self.root, source_path, item.category)
            data = source_path.read_bytes()
        digest = blake3.blake3(data).digest()
        if item.content_hash != digest or digest != content_digest:
            raise ValueError(f"image resource {resource_id} digest does not match the project")
        return data

    def resource_prefix(self, resource_id: str, content_digest: bytes, maximum_bytes: int) -> bytes:
        item = self._resource_file(resource_id)
        if item is None or item.content_hash != content_digest:
            raise ValueError(f"unknown or stale image resource {resource_id}")
        if item.payload is not None and item.payload[0] == 1:
            fields = item.payload[1]
            if len(fields) != 1 or not isinstance(fields[0], bytes):
                raise ValueError(f"image resource {resource_id} has no binary payload")
            return fields[0][:maximum_bytes]
        source_path = item.source_path or self.root / PurePosixPath(item.relative_path)
        _validate_new_project_file(self.root, source_path, item.category)
        signature = _source_signature(source_path)
        if item.source_signature != signature:
            raise ValueError(f"image resource {resource_id} changed after project scan")
        with source_path.open("rb") as stream:
            data = stream.read(maximum_bytes)
        if _source_signature(source_path) != signature:
            raise ValueError(f"image resource {resource_id} changed after project scan")
        return data

    def _resource_file(self, resource_id: str) -> ProjectFile | None:
        item = self.files.get(resource_id)
        if item is None:
            item = next(
                (
                    candidate
                    for path, candidate in self.files.items()
                    if path.lower() == resource_id.lower()
                ),
                None,
            )
        return item if item is not None and item.category == FILE_RESOURCE else None


def _cbor_container_header(major: int, length: int) -> bytes:
    if length < 24:
        return bytes([(major << 5) | length])
    if length <= 0xFF:
        return bytes([(major << 5) | 24, length])
    if length <= 0xFFFF:
        return bytes([(major << 5) | 25]) + length.to_bytes(2, "big")
    if length <= 0xFFFF_FFFF:
        return bytes([(major << 5) | 26]) + length.to_bytes(4, "big")
    return bytes([(major << 5) | 27]) + length.to_bytes(8, "big")
