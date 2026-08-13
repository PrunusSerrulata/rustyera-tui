"""Project bundle model and wire projection."""

from __future__ import annotations

from .project import (
    Any,
    DEFAULT_MAXIMUM_ENVELOPE_BYTES,
    DEFAULT_MAXIMUM_PAYLOAD_BYTES,
    FILE_RESOURCE,
    MAXIMUM_PROJECT_ENVELOPE_BYTES,
    PROJECT_ENVELOPE_HEADROOM_BYTES,
    PROJECT_FILE_WIRE_OVERHEAD_BYTES,
    Path,
    ProjectFile,
    ProjectScanMetrics,
    PurePosixPath,
    _path_sort_key,
    _payload_size,
    blake3,
    dataclass,
    field,
)

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
        return cls(resolved.parent, revision, files, resolved)

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
        return {0: self.revision, 1: hasher.digest()}

    def manifest(self) -> dict[int, Any]:
        if not self.is_materialized:
            raise RuntimeError("project source payloads have not been materialized")
        ordered = sorted(self.files.values(), key=lambda item: _path_sort_key(item.relative_path))
        return {0: self.revision, 1: [item.submitted() for item in ordered]}

    def requested_wire_limits(self) -> tuple[int, int]:
        """Return a conservative one-envelope project submission budget."""

        payload_bytes = sum(
            item.content_size
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
        if item is None or item.category != FILE_RESOURCE:
            raise ValueError(f"unknown image resource {resource_id}")
        if item.payload is None:
            source_path = item.source_path
            if source_path is None:
                pure = PurePosixPath(item.relative_path)
                source_path = self.root.joinpath(*pure.parts)
            data = source_path.read_bytes()
            if item.content_hash is None or blake3.blake3(data).digest() != item.content_hash:
                raise ValueError(f"image resource {resource_id} changed after project scan")
        else:
            tag, fields = item.payload
            if tag != 1 or len(fields) != 1 or not isinstance(fields[0], bytes):
                raise ValueError(f"image resource {resource_id} has no binary payload")
            data = fields[0]
        if blake3.blake3(data).digest() != content_digest:
            raise ValueError(f"image resource {resource_id} digest does not match the project")
        return data
