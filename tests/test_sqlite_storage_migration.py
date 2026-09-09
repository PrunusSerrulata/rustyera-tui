from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

import apsw

from test_sql_provider import CONNECTION, LIMITS, request, result, snake_sql_contract
from rustyera_tui.frontend_io import IO_CONFLICT
from rustyera_tui.sql_provider import SqlErrorCode, SqlProvider
from rustyera_tui.sql_revision_store import (
    SQLITE_VERSION,
    SQL_STORAGE_IDENTITY_ANCHOR_V1,
    SqlRevisionStore,
)
from rustyera_tui.wire import unwrap_variant, variant


class NoDeadline:
    def checkpoint(self) -> None:
        pass


class LegacyStorage:
    def __init__(self, files: dict[tuple[int, str], bytes]) -> None:
        self.files = files
        self.requests: list[dict[int, Any]] = []

    def handle(self, request: dict[int, Any]) -> dict[int, Any]:
        self.requests.append(request)
        namespace, path = request[1], request[2]
        tag, fields = unwrap_variant(request[3])
        key = (namespace, path)
        if tag == 0:
            data = self.files[key]
            result = variant(0, data, hashlib.sha256(data).hexdigest())
        elif tag == 2:
            entries = [
                {0: name, 1: len(data), 2: None, 3: None}
                for (space, name), data in self.files.items()
                if space == namespace and name.startswith(path + "/")
            ]
            result = variant(2, entries)
        else:
            assert tag == 1
            data, atomic, precondition = fields
            assert atomic is True
            kind, expected = unwrap_variant(precondition)
            if kind == 1:
                assert key not in self.files
            else:
                assert kind == 2
                assert expected == [hashlib.sha256(self.files[key]).hexdigest()]
            self.files[key] = data
            result = variant(1, hashlib.sha256(data).hexdigest())
        return {0: request[0], 1: result}


def test_legacy_resource_current_exact_and_cas_preserve_bytes_and_paths() -> None:
    assert SQLITE_VERSION == "3.53.4"
    assert SQL_STORAGE_IDENTITY_ANCHOR_V1 == "3.53.0"
    resource = "plugins/seed.db"
    seed = b"immutable old seed"
    current = b"old current database"
    exact = b"older exact database"
    seed_sha = hashlib.sha256(seed).digest()
    encoded = resource.encode("utf-8")
    # Independent literal old preimage: changing the production anchor must fail here.
    identity = hashlib.sha256(
        b"rustyera.sql.identity.v1\0"
        + struct.pack(">I", len(encoded))
        + encoded
        + seed_sha
        + b"3.53.0\0"
        + struct.pack(">I", 1)
    ).hexdigest()
    current_sha = hashlib.sha256(current).digest()
    exact_sha = hashlib.sha256(exact).digest()
    pointer_path = f"sql/v1/{identity}/current"
    current_path = f"sql/v1/{identity}/revisions/{current_sha.hex()}.sqlite3"
    exact_path = f"sql/v1/{identity}/revisions/{exact_sha.hex()}.sqlite3"
    pointer = (current_sha.hex() + "\n").encode("ascii")
    storage = LegacyStorage(
        {
            (5, resource): seed,
            (3, pointer_path): pointer,
            (3, current_path): current,
            (3, exact_path): exact,
        }
    )
    store = SqlRevisionStore()
    deadline = NoDeadline()
    opened, revision, chain = store.open_resource(
        storage, resource, seed_sha, None, deadline, lambda _: None
    )
    restored, restored_revision, exact_chain = store.open_resource(
        storage, resource, seed_sha, exact_sha, deadline, lambda _: None
    )
    assert (opened, revision) == (current, current_sha)
    assert (restored, restored_revision) == (exact, exact_sha)
    assert chain.identity_hex == exact_chain.identity_hex == identity
    assert all(unwrap_variant(request[3])[0] == 0 for request in storage.requests)
    next_bytes = b"new database"
    next_sha = hashlib.sha256(next_bytes).digest()
    store.publish(storage, chain, current_sha, next_bytes, next_sha, deadline)
    last = storage.requests[-1]
    assert last[2] == pointer_path
    assert unwrap_variant(last[3])[1][2] == variant(2, hashlib.sha256(pointer).hexdigest())
    assert storage.files[(3, current_path)] == current
    assert storage.files[(3, exact_path)] == exact
    assert storage.files[(5, resource)] == seed
    assert storage.files[(3, pointer_path)] == (next_sha.hex() + "\n").encode("ascii")


class CasLegacyStorage(LegacyStorage):
    """Model storage CAS failures without replacing the real SQLite provider."""

    def handle(self, request: dict[int, Any]) -> dict[int, Any]:
        tag, fields = unwrap_variant(request[3])
        if tag == 1:
            key = (request[1], request[2])
            kind, expected = unwrap_variant(fields[2])
            existing = self.files.get(key)
            conflict = (kind == 1 and existing is not None) or (
                kind == 2
                and (existing is None or expected != [hashlib.sha256(existing).hexdigest()])
            )
            if conflict:
                self.requests.append(request)
                return {0: request[0], 1: variant(4, {0: IO_CONFLICT})}
        return super().handle(request)


def test_historical_sqlite_seed_survives_upgraded_provider_revision_lifecycle() -> None:
    # Read the committed historical database, never regenerate it using the new engine.
    resource = "plugins/qol_data.db"
    seed = (Path(__file__).parent / "fixtures" / "snake-sql-project" / resource).read_bytes()
    seed_hex = "3eefa4c1f5e8eb01010ad3c3200364da0e506a639258062c0f7c52163eb0acd2"
    contract = snake_sql_contract()
    assert contract["sqliteVersion"] == "3.53.0"
    assert contract["seedSha256"] == contract["files"][resource] == seed_hex
    assert hashlib.sha256(seed).hexdigest() == seed_hex
    assert apsw.sqlitelibversion() == SQLITE_VERSION == "3.53.4"
    seed_sha = bytes.fromhex(seed_hex)
    encoded = resource.encode("utf-8")
    identity = hashlib.sha256(
        b"rustyera.sql.identity.v1\0"
        + struct.pack(">I", len(encoded))
        + encoded
        + seed_sha
        + b"3.53.0\0"
        + struct.pack(">I", 1)
    ).hexdigest()
    prefix = f"sql/v1/{identity}"
    pointer_key = (3, f"{prefix}/current")
    seed_key = (3, f"{prefix}/revisions/{seed_hex}.sqlite3")
    original_pointer = (seed_hex + "\n").encode("ascii")
    storage = CasLegacyStorage(
        {
            (5, resource): seed,
            seed_key: seed,
            pointer_key: original_pointer,
        }
    )
    providers: list[SqlProvider] = []

    def open_database(exact: bytes | None = None) -> SqlProvider:
        provider = SqlProvider()
        providers.append(provider)
        opened = request(
            provider,
            variant(
                0,
                CONNECTION,
                "migration",
                {0: variant(1, {0: resource, 1: seed_sha}), 1: SQLITE_VERSION, 2: 1},
                variant(0) if exact is None else variant(1, {0: exact}),
                dict(LIMITS),
            ),
            storage,
        )
        assert result(opened) == (0, ["3.53.4", dict(LIMITS)])
        expected = (
            exact
            if exact is not None
            else bytes.fromhex(storage.files[pointer_key].decode("ascii").strip())
        )
        assert opened[1][3][0] == expected
        return provider

    def execute(provider: SqlProvider, sql: str, mode: int = 0) -> dict[int, Any]:
        response = request(provider, variant(1, CONNECTION, mode, sql, []), storage)
        assert result(response)[0] == (1 if mode == 0 else 2), response
        return response

    def marker(provider: SqlProvider, expected: int) -> None:
        assert result(execute(provider, "SELECT version FROM seed_marker", 1)) == (
            2,
            [variant(1, expected)],
        )

    try:
        current = open_database()
        marker(current, 1)
        exact = open_database(seed_sha)
        marker(exact, 1)
        assert result(execute(current, "SELECT sqlite_version()", 2)) == (2, [variant(2, "3.53.4")])
        assert all(unwrap_variant(item[3])[0] == 0 for item in storage.requests)

        assert execute(current, "BEGIN")[1][2] is True
        execute(current, "UPDATE seed_marker SET version = 2")
        marker(current, 2)
        marker(exact, 1)
        assert storage.files[pointer_key] == original_pointer
        committed = execute(current, "COMMIT")
        assert committed[1][2] is False
        committed_sha = committed[1][3][0]
        assert committed_sha != seed_sha
        committed_key = (3, f"{prefix}/revisions/{committed_sha.hex()}.sqlite3")
        committed_bytes = storage.files[committed_key]
        assert hashlib.sha256(committed_bytes).digest() == committed_sha
        assert storage.files[pointer_key] == (committed_sha.hex() + "\n").encode("ascii")
        marker(open_database(), 2)

        # Exact may intentionally branch from an older revision. It must still lose CAS if
        # another connection advances current after Exact captured the pointer token.
        files_before_exact = dict(storage.files)
        stale_exact = open_database(seed_sha)
        marker(stale_exact, 1)
        assert storage.files == files_before_exact
        winner = execute(current, "UPDATE seed_marker SET version = 3")
        winner_sha = winner[1][3][0]
        winner_pointer = storage.files[pointer_key]
        assert winner_sha != committed_sha
        assert winner_pointer == (winner_sha.hex() + "\n").encode("ascii")
        conflicted = request(
            stale_exact,
            variant(1, CONNECTION, 0, "UPDATE seed_marker SET version = 99", []),
            storage,
        )
        assert result(conflicted)[0] == 10
        assert result(conflicted)[1][0][0] == SqlErrorCode.REVISION_CONFLICT
        assert conflicted[1][3][0] == seed_sha
        assert conflicted[1][2] is False
        pointer_writes = [
            item
            for item in storage.requests
            if (item[1], item[2]) == pointer_key and unwrap_variant(item[3])[0] == 1
        ]
        assert unwrap_variant(pointer_writes[-1][3])[1][2] == variant(
            2, hashlib.sha256(files_before_exact[pointer_key]).hexdigest()
        )
        marker(stale_exact, 1)
        assert storage.files[pointer_key] == winner_pointer
        marker(open_database(), 3)

        files_before_rollback = dict(storage.files)
        assert execute(current, "BEGIN")[1][2] is True
        execute(current, "UPDATE seed_marker SET version = 4")
        marker(current, 4)
        rolled_back = execute(current, "ROLLBACK")
        assert rolled_back[1][2] is False
        assert rolled_back[1][3][0] == winner_sha
        marker(current, 3)
        assert storage.files == files_before_rollback
        marker(open_database(committed_sha), 2)
        marker(open_database(seed_sha), 1)
        assert storage.files[seed_key] == storage.files[(5, resource)] == seed
        assert storage.files[committed_key] == committed_bytes
        assert hashlib.sha256(storage.files[seed_key]).hexdigest() == seed_hex
        assert all(
            item[1] == 5
            and item[2] == resource
            or item[1] == 3
            and item[2].startswith(prefix + "/")
            for item in storage.requests
        )
    finally:
        for provider in providers:
            provider.reset()
