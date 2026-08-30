"""Immutable SQLite revision chains backed by the project Data namespace."""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .frontend_io import IO_CONFLICT, IO_NOT_FOUND
from .wire import unwrap_variant, variant

SQLITE_VERSION = "3.53.0"
DATABASE_FORMAT_VERSION = 1
MAXIMUM_DATABASE_BYTES = 64 * 1024 * 1024

# These numeric values are part of rustyera.sql@1 and intentionally live at this
# boundary so the revision store does not depend back on the provider dispatcher.
INVALID_SOURCE = 2
DATABASE_TOO_LARGE = 15
EXECUTION_TIMEOUT = 19
REVISION_CONFLICT = 21
REVISION_MISSING = 22
STORAGE_FAILURE = 23


class StorageLike(Protocol):
    def handle(self, request: dict[int, Any]) -> dict[int, Any]: ...


class DeadlineLike(Protocol):
    def checkpoint(self) -> None: ...


@dataclass(slots=True)
class SqlChain:
    identity_hex: str
    current_database_revision: str | None
    current_storage_revision: str | None


class SqlRevisionStoreError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}


class SqlRevisionStore:
    """Own identity, immutable blobs, current CAS, and chain quota invariants."""

    def __init__(self) -> None:
        # Deliberately survives provider epoch resets so idempotency keys are never reused
        # against the same StorageBackend instance.
        self.next_storage_request = 1

    def open_resource(
        self,
        storage: StorageLike,
        resource_id: str,
        expected_seed: bytes,
        exact: bytes | None,
        deadline: DeadlineLike,
        seed_validator: Callable[[bytes], None],
    ) -> tuple[bytes, bytes, SqlChain]:
        resource_id = safe_resource_id(resource_id)
        deadline.checkpoint()
        try:
            seed, _ = self._read(storage, 5, resource_id, deadline)
        except FileNotFoundError as error:
            raise SqlRevisionStoreError(INVALID_SOURCE, "SQL Resource seed is missing") from error
        if len(seed) > MAXIMUM_DATABASE_BYTES:
            raise SqlRevisionStoreError(DATABASE_TOO_LARGE, "SQL Resource seed is too large")
        deadline.checkpoint()
        actual_seed = hashlib.sha256(seed).digest()
        if actual_seed != expected_seed:
            raise SqlRevisionStoreError(INVALID_SOURCE, "SQL Resource seed digest changed")
        seed_validator(seed)
        deadline.checkpoint()

        identity = sql_identity_digest(resource_id, expected_seed)
        current_path = current_path_for(identity)
        current_database: str | None = None
        current_storage: str | None = None
        try:
            pointer, current_storage = self._read(storage, 3, current_path, deadline)
        except FileNotFoundError:
            pointer = b""
        if pointer:
            try:
                text = pointer.decode("ascii")
            except UnicodeDecodeError as error:
                raise SqlRevisionStoreError(
                    STORAGE_FAILURE, "SQL current pointer is malformed"
                ) from error
            if not re.fullmatch(r"[0-9a-f]{64}\n", text):
                raise SqlRevisionStoreError(STORAGE_FAILURE, "SQL current pointer is malformed")
            current_database = text[:-1]

        selected = exact.hex() if exact is not None else current_database
        if selected is None:
            selected = actual_seed.hex()
            self._enforce_chain_quota(storage, identity, selected, len(seed), deadline)
            self._write_revision(storage, identity, selected, seed, deadline)
            try:
                current_storage = self._write(
                    storage,
                    current_path,
                    f"{selected}\n".encode("ascii"),
                    variant(1),
                    "initialize",
                    deadline,
                )
            except SqlRevisionStoreError as error:
                if error.code != REVISION_CONFLICT:
                    raise
                try:
                    concurrent, concurrent_storage = self._read(storage, 3, current_path, deadline)
                except FileNotFoundError as missing:
                    raise SqlRevisionStoreError(
                        REVISION_CONFLICT,
                        "SQL current revision initialization conflicted",
                    ) from missing
                if concurrent != f"{selected}\n".encode("ascii"):
                    raise SqlRevisionStoreError(
                        REVISION_CONFLICT, "SQL current revision changed during initialization"
                    ) from error
                current_storage = concurrent_storage
            current_database = selected
        elif current_database is None:
            # A verified exact orphan may republish with a Missing current precondition.
            current_database = selected

        try:
            contents, _ = self._read(storage, 3, revision_path_for(identity, selected), deadline)
        except FileNotFoundError as error:
            raise SqlRevisionStoreError(REVISION_MISSING, "SQL revision is missing") from error
        deadline.checkpoint()
        if (
            len(contents) > MAXIMUM_DATABASE_BYTES
            or hashlib.sha256(contents).hexdigest() != selected
        ):
            raise SqlRevisionStoreError(STORAGE_FAILURE, "SQL revision is corrupt")
        return (
            contents,
            bytes.fromhex(selected),
            SqlChain(identity, selected, current_storage),
        )

    def publish(
        self,
        storage: StorageLike,
        chain: SqlChain,
        expected: bytes,
        contents: bytes,
        revision: bytes,
        deadline: DeadlineLike,
    ) -> None:
        deadline.checkpoint()
        expected_hex = expected.hex()
        if chain.current_database_revision != expected_hex:
            raise SqlRevisionStoreError(REVISION_CONFLICT, "SQL current revision changed")
        revision_hex = revision.hex()
        if hashlib.sha256(contents).digest() != revision:
            raise SqlRevisionStoreError(STORAGE_FAILURE, "SQL publication digest mismatch")
        self._enforce_chain_quota(
            storage, chain.identity_hex, revision_hex, len(contents), deadline
        )
        self._write_revision(storage, chain.identity_hex, revision_hex, contents, deadline)
        deadline.checkpoint()
        precondition = (
            variant(2, chain.current_storage_revision)
            if chain.current_storage_revision is not None
            else variant(1)
        )
        storage_revision = self._write(
            storage,
            current_path_for(chain.identity_hex),
            f"{revision_hex}\n".encode("ascii"),
            precondition,
            f"publish-{revision_hex}",
            deadline,
        )
        chain.current_database_revision = revision_hex
        chain.current_storage_revision = storage_revision

    def _storage(
        self,
        storage: StorageLike,
        namespace: int,
        path: str,
        operation: list[Any],
        suffix: str,
        deadline: DeadlineLike,
        *,
        check_after: bool = True,
    ) -> list[Any]:
        deadline.checkpoint()
        request_id = self.next_storage_request
        self.next_storage_request += 1
        response = storage.handle(
            {
                0: request_id,
                1: namespace,
                2: path,
                3: operation,
                4: f"sql-v1-{request_id}-{suffix}",
                5: None,
            }
        )
        if check_after:
            deadline.checkpoint()
        if not isinstance(response, dict) or response.get(0) != request_id or 1 not in response:
            raise SqlRevisionStoreError(STORAGE_FAILURE, "invalid SQL storage response")
        result = response[1]
        if not isinstance(result, list):
            raise SqlRevisionStoreError(STORAGE_FAILURE, "invalid SQL storage result")
        return result

    def _read(
        self, storage: StorageLike, namespace: int, path: str, deadline: DeadlineLike
    ) -> tuple[bytes, str]:
        result = self._storage(storage, namespace, path, variant(0), "read", deadline)
        tag, fields = unwrap_variant(result)
        if (
            tag == 0
            and len(fields) == 2
            and isinstance(fields[0], bytes)
            and isinstance(fields[1], str)
            and fields[1]
        ):
            return fields[0], fields[1]
        if tag == 4 and _storage_error_kind(fields) == IO_NOT_FOUND:
            raise FileNotFoundError(path)
        raise SqlRevisionStoreError(STORAGE_FAILURE, "cannot read SQL storage")

    def _write(
        self,
        storage: StorageLike,
        path: str,
        contents: bytes,
        precondition: list[Any],
        suffix: str,
        deadline: DeadlineLike,
    ) -> str:
        result = self._storage(
            storage,
            3,
            path,
            variant(1, contents, True, precondition),
            suffix,
            deadline,
            check_after=False,
        )
        tag, fields = unwrap_variant(result)
        if tag == 1 and len(fields) == 1 and isinstance(fields[0], str) and fields[0]:
            return fields[0]
        if tag == 4 and _storage_error_kind(fields) == IO_CONFLICT:
            raise SqlRevisionStoreError(REVISION_CONFLICT, "SQL revision conflicted")
        # A local atomic replacement may already have committed even if its acknowledgement was
        # malformed. Read back the exact bytes so callers never roll back live SQLite state after
        # current has durably advanced.
        storage_revision = ""
        try:
            written, storage_revision = self._read(storage, 3, path, deadline)
        except FileNotFoundError:
            written = b""
        if written == contents:
            return storage_revision
        raise SqlRevisionStoreError(STORAGE_FAILURE, "cannot write SQL storage")

    def _write_revision(
        self,
        storage: StorageLike,
        identity: str,
        revision: str,
        contents: bytes,
        deadline: DeadlineLike,
    ) -> None:
        path = revision_path_for(identity, revision)
        try:
            self._write(storage, path, contents, variant(1), f"revision-{revision}", deadline)
        except SqlRevisionStoreError as error:
            if error.code != REVISION_CONFLICT:
                raise
            try:
                existing, _ = self._read(storage, 3, path, deadline)
            except FileNotFoundError as missing:
                raise SqlRevisionStoreError(
                    STORAGE_FAILURE, "SQL revision conflict disappeared"
                ) from missing
            deadline.checkpoint()
            if len(existing) != len(contents) or hashlib.sha256(existing).hexdigest() != revision:
                raise SqlRevisionStoreError(STORAGE_FAILURE, "SQL revision is corrupt")

    def _enforce_chain_quota(
        self,
        storage: StorageLike,
        identity: str,
        candidate_revision: str,
        candidate_bytes: int,
        deadline: DeadlineLike,
    ) -> None:
        result = self._storage(
            storage,
            3,
            f"sql/v1/{identity}/revisions",
            variant(2, None, False),
            "quota",
            deadline,
        )
        tag, fields = unwrap_variant(result)
        if tag == 4 and _storage_error_kind(fields) == IO_NOT_FOUND:
            entries: list[Any] = []
        elif tag == 2 and len(fields) == 1 and isinstance(fields[0], list):
            entries = fields[0]
        else:
            raise SqlRevisionStoreError(STORAGE_FAILURE, "cannot list SQL revisions")
        total = 0
        candidate_exists = False
        candidate_name = f"{candidate_revision}.sqlite3"
        expected_prefix = f"sql/v1/{identity}/revisions/"
        for raw in entries:
            deadline.checkpoint()
            entry = _map(raw, {0, 1, 2, 3}, "SQL revision entry")
            relative = _text(entry[0], "SQL revision path")
            name = relative.removeprefix(expected_prefix)
            if "/" in name or not re.fullmatch(r"[0-9a-f]{64}\.sqlite3", name):
                raise SqlRevisionStoreError(STORAGE_FAILURE, "SQL revision listing is malformed")
            byte_length = _unsigned(entry[1], "SQL revision byte length")
            total += byte_length
            candidate_exists = candidate_exists or name == candidate_name
            if total > MAXIMUM_DATABASE_BYTES:
                raise SqlRevisionStoreError(
                    DATABASE_TOO_LARGE, "SQL immutable revision chain exceeds its limit"
                )
        if not candidate_exists:
            total += candidate_bytes
        if total > MAXIMUM_DATABASE_BYTES:
            raise SqlRevisionStoreError(
                DATABASE_TOO_LARGE, "SQL immutable revision chain exceeds its limit"
            )


def _identity_preimage(resource_id: str, seed_sha256: bytes) -> bytes:
    resource = resource_id.encode("utf-8")
    return b"".join(
        (
            b"rustyera.sql.identity.v1\0",
            struct.pack(">I", len(resource)),
            resource,
            seed_sha256,
            b"3.53.0\0",
            struct.pack(">I", DATABASE_FORMAT_VERSION),
        )
    )


def sql_identity_digest(resource_id: str, seed_sha256: bytes) -> str:
    """Return the cross-client content-chain digest used by Browser, Tauri, and TUI."""

    return hashlib.sha256(_identity_preimage(resource_id, seed_sha256)).hexdigest()


def current_path_for(identity: str) -> str:
    return f"sql/v1/{identity}/current"


def revision_path_for(identity: str, revision: str) -> str:
    return f"sql/v1/{identity}/revisions/{revision}.sqlite3"


def safe_resource_id(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    if (
        not value
        or value != normalized
        or len(value.encode("utf-8")) > 4096
        or "\\" in value
        or "\0" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) is not None
        or any(part in ("", ".", "..") for part in parts)
        or any(any(ord(character) < 0x20 for character in part) for part in parts)
    ):
        raise SqlRevisionStoreError(INVALID_SOURCE, "unsafe SQL Resource seed ID")
    return value


def _map(value: Any, keys: set[int], name: str) -> dict[int, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SqlRevisionStoreError(STORAGE_FAILURE, f"{name} has an invalid shape")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise SqlRevisionStoreError(STORAGE_FAILURE, f"{name} is not text")
    return value


def _unsigned(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SqlRevisionStoreError(STORAGE_FAILURE, f"{name} is not unsigned")
    return value


def _storage_error_kind(fields: list[Any]) -> int | None:
    if len(fields) != 1 or not isinstance(fields[0], dict):
        return None
    kind = fields[0].get(0)
    return kind if type(kind) is int else None
