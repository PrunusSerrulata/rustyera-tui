from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import apsw
import pytest

import rustyera_tui.sql_provider as sql_provider_module
import rustyera_tui.sql_revision_store as revision_store_module
from rustyera_tui.frontend_io import IO_CONFLICT
from rustyera_tui.project import ProjectBundle, StorageBackend
from rustyera_tui.sql_provider import LIMITS, SqlErrorCode, SqlProvider, sql_identity_digest
from rustyera_tui.sql_revision_store import SqlRevisionStoreError, safe_resource_id
from rustyera_tui.wire import decode, encode, unwrap_variant, variant


PROVIDER = {0: 7, 1: 1}
CONNECTION = {0: 7, 1: 2}


class UnusedStorage:
    def handle(self, _request: dict[int, Any]) -> dict[int, Any]:
        raise AssertionError("memory SQL must not access project storage")


def request(provider: SqlProvider, operation: list[Any], storage: Any) -> dict[int, Any]:
    return decode(provider.handle(encode({0: PROVIDER, 1: operation}), storage, None))


def result(response: dict[int, Any]) -> tuple[int, list[Any]]:
    return unwrap_variant(response[3])


def test_sql_identity_digest_matches_the_cross_client_fixed_vector() -> None:
    assert sql_identity_digest("plugins/qol_data.db", bytes(range(32))) == (
        "905e8872fc8d0cba39021c4e999f13f59a190de5bee5db5f0402e686640b3713"
    )


def test_runtime_fixture_is_a_deterministic_sqlite_353_database() -> None:
    fixture = Path(__file__).parent / "fixtures" / "snake-sql-project" / "plugins" / "qol_data.db"
    contents = fixture.read_bytes()
    assert hashlib.sha256(contents).hexdigest() == (
        "9987e229ad61fe8febaafa595cb0c90c3b9c3c171a20078a5b349588627000cd"
    )
    database = apsw.Connection(str(fixture), flags=apsw.SQLITE_OPEN_READONLY)
    assert database.execute("PRAGMA user_version").fetchone() == (1,)
    assert database.execute("SELECT version FROM seed_marker").fetchone() == (1,)
    database.close()


def test_memory_provider_preserves_reader_long_and_string_conversion() -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    opened = request(
        provider,
        variant(
            0,
            CONNECTION,
            "memory",
            {0: variant(0), 1: "3.53.0", 2: 1},
            variant(0),
            dict(LIMITS),
        ),
        storage,
    )
    assert result(opened)[0] == 0

    for sql in (
        "CREATE TABLE rows(n INTEGER, s TEXT, z)",
        "INSERT INTO rows VALUES(42, 'not-an-integer', NULL)",
    ):
        response = request(provider, variant(1, CONNECTION, 0, sql, []), storage)
        assert result(response)[0] == 1

    opened_reader = request(
        provider,
        variant(1, CONNECTION, 3, "SELECT CAST(n AS TEXT), s, n, z FROM rows", []),
        storage,
    )
    reader = result(opened_reader)[1][0]
    assert result(request(provider, variant(2, reader), storage)) == (4, [True])
    assert result(request(provider, variant(3, reader, 0, 0), storage)) == (
        5,
        [variant(1, 42)],
    )
    rejected = request(provider, variant(3, reader, 1, 0), storage)
    assert result(rejected)[0] == 10
    assert result(request(provider, variant(3, reader, 2, 1), storage)) == (
        5,
        [variant(2, "42")],
    )
    assert result(request(provider, variant(4, reader, 3), storage)) == (6, [True])
    assert result(request(provider, variant(2, reader), storage)) == (4, [False])
    assert result(request(provider, variant(2, reader), storage)) == (4, [False])


def test_resource_database_publishes_before_ack_and_reopens_current_revision(
    tmp_path: Path,
) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    seed_path = plugins / "seed.db"
    database = apsw.Connection(str(seed_path))
    database.execute("CREATE TABLE values_table(value TEXT)")
    database.close()
    seed = seed_path.read_bytes()
    seed_sha = hashlib.sha256(seed).digest()
    bundle = ProjectBundle.scan(tmp_path)
    storage = StorageBackend(tmp_path, resource_bundle=bundle)

    provider = SqlProvider()
    open_operation = variant(
        0,
        CONNECTION,
        "persistent",
        {0: variant(1, {0: "plugins/seed.db", 1: seed_sha}), 1: "3.53.0", 2: 1},
        variant(0),
        dict(LIMITS),
    )
    request(provider, open_operation, storage)
    written = request(
        provider,
        variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('published')", []),
        storage,
    )
    durable = written[1][3][0]
    assert isinstance(durable, bytes) and len(durable) == 32

    restarted = SqlProvider()
    request(restarted, open_operation, storage)
    scalar = request(
        restarted,
        variant(1, CONNECTION, 2, "SELECT value FROM values_table", []),
        storage,
    )
    assert result(scalar) == (2, [variant(2, "published")])


def test_scalar_conversions_match_numeric_text_and_type_error_semantics() -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    request(
        provider,
        variant(
            0,
            CONNECTION,
            "memory",
            {0: variant(0), 1: "3.53.0", 2: 1},
            variant(0),
            dict(LIMITS),
        ),
        storage,
    )
    rejected = request(
        provider,
        variant(1, CONNECTION, 1, "SELECT 'not-an-integer'", []),
        storage,
    )
    assert result(rejected)[0] == 10
    assert result(rejected)[1][0][0] == 10
    converted = request(provider, variant(1, CONNECTION, 1, "SELECT '42'", []), storage)
    assert result(converted) == (2, [variant(1, 42)])
    formatted = request(provider, variant(1, CONNECTION, 2, "SELECT 42", []), storage)
    assert result(formatted) == (2, [variant(2, "42")])


def open_memory(provider: SqlProvider, storage: Any) -> dict[int, Any]:
    return request(
        provider,
        variant(
            0,
            CONNECTION,
            "memory",
            {0: variant(0), 1: "3.53.0", 2: 1},
            variant(0),
            dict(LIMITS),
        ),
        storage,
    )


def persistent_fixture(tmp_path: Path) -> tuple[StorageBackend, list[Any], bytes]:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    seed_path = plugins / "seed.db"
    database = apsw.Connection(str(seed_path))
    database.execute("CREATE TABLE values_table(value TEXT)")
    database.close()
    seed_sha = hashlib.sha256(seed_path.read_bytes()).digest()
    bundle = ProjectBundle.scan(tmp_path)
    storage = StorageBackend(tmp_path, resource_bundle=bundle)
    operation = variant(
        0,
        CONNECTION,
        "persistent",
        {0: variant(1, {0: "plugins/seed.db", 1: seed_sha}), 1: "3.53.0", 2: 1},
        variant(0),
        dict(LIMITS),
    )
    return storage, operation, seed_sha


def test_reader_get_core_canonical_cbor_uses_bare_mode_index() -> None:
    reader = {0: 7, 1: 9}
    payload = encode({0: PROVIDER, 1: variant(3, reader, 2, 1)})

    assert payload == bytes.fromhex("a200a20007010101820383a2000701090201")
    assert decode(payload)[1] == variant(3, reader, 2, 1)


def test_open_reports_actual_pinned_sqlite_version(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    assert result(open_memory(provider, storage))[0] == 0
    assert apsw.sqlite_lib_version() == "3.53.0"

    mismatch = SqlProvider()
    monkeypatch.setattr(apsw, "sqlite_lib_version", lambda: "3.53.1")
    rejected = open_memory(mismatch, storage)
    assert result(rejected)[0] == 10
    assert result(rejected)[1][0][0] == SqlErrorCode.UNSUPPORTED


def test_expired_deadline_and_unknown_operation_return_decodable_errors() -> None:
    provider = SqlProvider()
    expired = decode(
        provider.handle(
            encode({0: PROVIDER, 1: variant(0)}),
            UnusedStorage(),
            0,
        )
    )
    assert result(expired)[1][0][0] == SqlErrorCode.EXECUTION_TIMEOUT
    assert result(expired)[1][0][1] == 0

    unknown = request(provider, variant(99), UnusedStorage())
    assert result(unknown)[1][0][0] == SqlErrorCode.INVALID_REQUEST
    assert result(unknown)[1][0][1] == 0


def test_transactions_publish_commit_and_leave_rollback_unchanged() -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)
    request(provider, variant(1, CONNECTION, 0, "CREATE TABLE items(value INTEGER)", []), storage)

    begun = request(provider, variant(1, CONNECTION, 0, "BEGIN", []), storage)
    assert begun[1][2] is True
    request(provider, variant(1, CONNECTION, 0, "INSERT INTO items VALUES(1)", []), storage)
    failed = request(provider, variant(1, CONNECTION, 0, "SELECT * FROM missing", []), storage)
    assert result(failed)[0] == 10
    assert failed[1][2] is True
    rolled_back = request(provider, variant(1, CONNECTION, 0, "ROLLBACK", []), storage)
    assert rolled_back[1][2] is False
    assert result(
        request(provider, variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM items", []), storage)
    ) == (
        2,
        [variant(1, 0)],
    )

    request(provider, variant(1, CONNECTION, 0, "BEGIN", []), storage)
    request(provider, variant(1, CONNECTION, 0, "INSERT INTO items VALUES(2)", []), storage)
    committed = request(provider, variant(1, CONNECTION, 0, "COMMIT", []), storage)
    assert committed[1][2] is False
    assert result(
        request(provider, variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM items", []), storage)
    ) == (
        2,
        [variant(1, 1)],
    )


def test_write_reader_publishes_on_eof_and_explicit_close(tmp_path: Path) -> None:
    storage, open_operation, _ = persistent_fixture(tmp_path)
    provider = SqlProvider()
    request(provider, open_operation, storage)

    first = request(
        provider,
        variant(1, CONNECTION, 3, "INSERT INTO values_table VALUES('eof') RETURNING value", []),
        storage,
    )
    first_reader = result(first)[1][0]
    assert result(request(provider, variant(2, first_reader), storage)) == (4, [True])
    eof = request(provider, variant(2, first_reader), storage)
    assert result(eof) == (4, [False])
    eof_revision = eof[1][3][0]
    assert result(request(provider, variant(2, first_reader), storage)) == (4, [False])

    second = request(
        provider,
        variant(1, CONNECTION, 3, "INSERT INTO values_table VALUES('close') RETURNING value", []),
        storage,
    )
    second_reader = result(second)[1][0]
    assert result(request(provider, variant(2, second_reader), storage)) == (4, [True])
    closed = request(provider, variant(5, second_reader), storage)
    assert result(closed) == (7, [])
    assert closed[2][1] == 3
    assert closed[1][3][0] != eof_revision

    restarted = SqlProvider()
    request(restarted, open_operation, storage)
    count = request(
        restarted,
        variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM values_table", []),
        storage,
    )
    assert result(count) == (2, [variant(1, 2)])


def test_missing_reader_and_row_limit_return_core_valid_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)
    missing = {0: 7, 1: 404}
    for operation in (variant(2, missing), variant(5, missing)):
        rejected = request(provider, operation, storage)
        assert result(rejected)[0] == 10
        assert result(rejected)[1][0][0] == SqlErrorCode.READER_NOT_FOUND

    monkeypatch.setattr(sql_provider_module, "MAXIMUM_READER_ROWS", 1)
    opened = request(
        provider,
        variant(1, CONNECTION, 3, "SELECT 1 UNION ALL SELECT 2", []),
        storage,
    )
    reader = result(opened)[1][0]
    assert result(request(provider, variant(2, reader), storage)) == (4, [True])
    limited = request(provider, variant(2, reader), storage)
    assert result(limited)[0] == 10
    assert limited[2][1] == 3
    assert limited[2][2] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE '/tmp/rustyera-forbidden.db' AS external",
        "VACUUM INTO '/tmp/rustyera-forbidden.db'",
        "CREATE VIRTUAL TABLE forbidden USING fts5(value)",
        "SELECT load_extension('/tmp/rustyera-forbidden')",
    ],
)
def test_untrusted_sql_cannot_access_host_files_or_extensions(sql: str) -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)

    rejected = request(provider, variant(1, CONNECTION, 0, sql, []), storage)

    assert result(rejected)[0] == 10


def test_memory_database_growth_is_bounded_by_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sql_provider_module, "MAXIMUM_DATABASE_BYTES", 16 * 1024)
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)
    request(provider, variant(1, CONNECTION, 0, "CREATE TABLE payload(value BLOB)", []), storage)

    rejected = request(
        provider,
        variant(1, CONNECTION, 0, "INSERT INTO payload VALUES(zeroblob(65536))", []),
        storage,
    )

    assert result(rejected)[0] == 10


@pytest.mark.parametrize(
    "resource_id",
    [
        "../seed.db",
        "C:/seed.db",
        "file:seed.db",
        "https://example.invalid/seed.db",
        "plugins//seed.db",
        "plugins/e\u0301.db",
    ],
)
def test_resource_identity_rejects_path_and_unicode_aliases(resource_id: str) -> None:
    with pytest.raises(SqlRevisionStoreError, match="unsafe SQL Resource"):
        safe_resource_id(resource_id)


def test_concurrent_current_cas_conflict_restores_old_durable_state(tmp_path: Path) -> None:
    storage, open_operation, _ = persistent_fixture(tmp_path)
    first = SqlProvider()
    second = SqlProvider()
    request(first, open_operation, storage)
    request(second, open_operation, storage)

    request(
        first, variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('first')", []), storage
    )
    conflicted = request(
        second,
        variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('second')", []),
        storage,
    )

    assert result(conflicted)[0] == 10
    assert result(conflicted)[1][0][0] == SqlErrorCode.REVISION_CONFLICT
    assert result(
        request(second, variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM values_table", []), storage)
    ) == (2, [variant(1, 0)])


def test_write_reader_publish_failure_reports_restored_database_and_closed_reader(
    tmp_path: Path,
) -> None:
    storage, open_operation, seed_sha = persistent_fixture(tmp_path)
    winner = SqlProvider()
    loser = SqlProvider()
    request(winner, open_operation, storage)
    request(loser, open_operation, storage)
    opened = request(
        loser,
        variant(1, CONNECTION, 3, "INSERT INTO values_table VALUES('loser') RETURNING value", []),
        storage,
    )
    reader = result(opened)[1][0]
    assert result(request(loser, variant(2, reader), storage)) == (4, [True])
    request(
        winner, variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('winner')", []), storage
    )

    conflicted = request(loser, variant(2, reader), storage)

    assert result(conflicted)[0] == 10
    assert result(conflicted)[1][0][0] == SqlErrorCode.REVISION_CONFLICT
    assert conflicted[1][3][0] == seed_sha
    assert conflicted[1][2] is False
    assert conflicted[2][1] == 3
    assert conflicted[2][2] == 1


def test_initial_pointer_failure_leaves_exact_revision_recoverable(tmp_path: Path) -> None:
    storage, open_operation, seed_sha = persistent_fixture(tmp_path)

    class FailInitialCurrent:
        failed = False

        def handle(self, storage_request: dict[int, Any]) -> dict[int, Any]:
            operation_tag, _ = storage_request[3]
            if (
                not self.failed
                and storage_request[1] == 3
                and storage_request[2].endswith("/current")
                and operation_tag == 1
            ):
                self.failed = True
                return {0: storage_request[0], 1: variant(4, {0: IO_CONFLICT, 1: "forced"})}
            return storage.handle(storage_request)

    failing = FailInitialCurrent()
    failed_open = request(SqlProvider(), open_operation, failing)
    assert result(failed_open)[0] == 10
    assert result(failed_open)[1][0][0] == SqlErrorCode.REVISION_CONFLICT

    exact_operation = list(open_operation)
    exact_fields = list(exact_operation[1])
    exact_fields[3] = variant(1, {0: seed_sha})
    exact_operation[1] = exact_fields
    recovered = SqlProvider()
    assert result(request(recovered, exact_operation, storage))[0] == 0
    published = request(
        recovered,
        variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('recovered')", []),
        storage,
    )
    assert result(published)[0] == 1


def test_conflicting_revision_blob_is_accepted_only_after_full_digest_match(tmp_path: Path) -> None:
    storage, open_operation, seed_sha = persistent_fixture(tmp_path)
    identity = sql_identity_digest("plugins/seed.db", seed_sha)
    revision_path = (
        tmp_path / "data" / "sql" / "v1" / identity / "revisions" / f"{seed_sha.hex()}.sqlite3"
    )
    revision_path.parent.mkdir(parents=True)
    revision_path.write_bytes(b"not-the-seed")

    rejected = request(SqlProvider(), open_operation, storage)

    assert result(rejected)[0] == 10
    assert result(rejected)[1][0][0] == SqlErrorCode.STORAGE_FAILURE


def test_digest_valid_non_database_resource_maps_to_invalid_source(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    seed_path = plugins / "invalid.db"
    seed_path.write_bytes(b"not sqlite")
    seed_sha = hashlib.sha256(seed_path.read_bytes()).digest()
    storage = StorageBackend(tmp_path, resource_bundle=ProjectBundle.scan(tmp_path))
    operation = variant(
        0,
        CONNECTION,
        "invalid",
        {0: variant(1, {0: "plugins/invalid.db", 1: seed_sha}), 1: "3.53.0", 2: 1},
        variant(0),
        dict(LIMITS),
    )

    rejected = request(SqlProvider(), operation, storage)

    assert result(rejected)[0] == 10
    assert result(rejected)[1][0][0] == SqlErrorCode.INVALID_SOURCE


def test_revision_chain_quota_failure_restores_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, open_operation, _ = persistent_fixture(tmp_path)
    seed_size = (tmp_path / "plugins" / "seed.db").stat().st_size
    monkeypatch.setattr(revision_store_module, "MAXIMUM_DATABASE_BYTES", seed_size + 1)
    provider = SqlProvider()
    request(provider, open_operation, storage)

    rejected = request(
        provider,
        variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('too-large-chain')", []),
        storage,
    )

    assert result(rejected)[0] == 10
    assert result(rejected)[1][0][0] == SqlErrorCode.DATABASE_TOO_LARGE
    assert result(
        request(
            provider, variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM values_table", []), storage
        )
    ) == (2, [variant(1, 0)])


def test_committed_current_with_malformed_ack_is_verified_instead_of_rolled_back(
    tmp_path: Path,
) -> None:
    storage, open_operation, _ = persistent_fixture(tmp_path)

    class MalformCurrentAck:
        def handle(self, storage_request: dict[int, Any]) -> dict[int, Any]:
            response = storage.handle(storage_request)
            operation_tag, _ = storage_request[3]
            if storage_request[2].endswith("/current") and operation_tag == 1:
                return {0: storage_request[0], 1: [99, []]}
            return response

    wrapped = MalformCurrentAck()
    provider = SqlProvider()
    assert result(request(provider, open_operation, wrapped))[0] == 0
    published = request(
        provider,
        variant(1, CONNECTION, 0, "INSERT INTO values_table VALUES('committed')", []),
        wrapped,
    )

    assert result(published)[0] == 1
    restarted = SqlProvider()
    request(restarted, open_operation, storage)
    assert result(
        request(
            restarted, variant(1, CONNECTION, 1, "SELECT COUNT(*) FROM values_table", []), storage
        )
    ) == (2, [variant(1, 1)])


def test_provider_reset_closes_connections_and_readers() -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)
    opened = request(provider, variant(1, CONNECTION, 3, "SELECT 1", []), storage)
    reader = result(opened)[1][0]
    database = provider.connections[(7, 2)].database

    provider.reset()

    assert provider.connections == {}
    assert provider.readers == {}
    with pytest.raises(apsw.ConnectionClosedError):
        database.get_autocommit()
    assert (
        result(request(provider, variant(2, reader), storage))[1][0][0]
        == SqlErrorCode.READER_NOT_FOUND
    )


def test_disconnect_reports_authoritative_closed_database_state() -> None:
    provider = SqlProvider()
    storage = UnusedStorage()
    open_memory(provider, storage)

    disconnected = request(provider, variant(7, CONNECTION), storage)

    assert result(disconnected) == (9, [])
    assert disconnected[1] == {0: CONNECTION, 1: False, 2: False, 3: None}
