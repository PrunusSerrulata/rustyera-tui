"""Project-scoped SQLite 3.53 provider for the versioned SQL service."""

from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import apsw

from .sql_revision_store import (
    DATABASE_FORMAT_VERSION,
    MAXIMUM_DATABASE_BYTES,
    SQLITE_VERSION,
    SqlChain,
    SqlRevisionStore,
    SqlRevisionStoreError,
    StorageLike,
    safe_resource_id,
    sql_identity_digest,
)
from .wire import decode, encode, unwrap_variant, variant

__all__ = ["LIMITS", "SqlErrorCode", "SqlProvider", "sql_identity_digest"]

SQL_OPERATION = "rustyera.sql"
MAXIMUM_CONNECTIONS = 8
MAXIMUM_READERS = 32
MAXIMUM_SQL_BYTES = 256 * 1024
MAXIMUM_PARAMETERS = 64
MAXIMUM_PARAMETER_BYTES = 8 * 1024 * 1024
MAXIMUM_CELL_BYTES = 1024 * 1024
MAXIMUM_MAP_ROWS = 100_000
MAXIMUM_MAP_BYTES = 8 * 1024 * 1024
MAXIMUM_READER_ROWS = 1_000_000
EXECUTION_BUDGET_NS = 5_000_000_000

LIMITS = {
    0: MAXIMUM_CONNECTIONS,
    1: MAXIMUM_READERS,
    2: MAXIMUM_SQL_BYTES,
    3: MAXIMUM_PARAMETERS,
    4: MAXIMUM_PARAMETER_BYTES,
    5: MAXIMUM_CELL_BYTES,
    6: MAXIMUM_DATABASE_BYTES,
    7: MAXIMUM_MAP_ROWS,
    8: MAXIMUM_MAP_BYTES,
    9: MAXIMUM_READER_ROWS,
    10: EXECUTION_BUDGET_NS // 1_000_000,
}


class SqlErrorCode:
    INVALID_REQUEST = 0
    INVALID_NAME = 1
    INVALID_SOURCE = 2
    INVALID_CONNECTION_STRING = 3
    CONNECTION_LIMIT = 4
    CONNECTION_CONFLICT = 5
    CONNECTION_NOT_FOUND = 6
    READER_LIMIT = 7
    READER_NOT_FOUND = 8
    COLUMN_OUT_OF_RANGE = 9
    TYPE_MISMATCH = 10
    SQL_TOO_LARGE = 11
    PARAMETER_LIMIT = 12
    PARAMETER_BYTES_LIMIT = 13
    CELL_TOO_LARGE = 14
    DATABASE_TOO_LARGE = 15
    MAP_ROW_LIMIT = 16
    MAP_BYTES_LIMIT = 17
    READER_ROW_LIMIT = 18
    EXECUTION_TIMEOUT = 19
    TRANSACTION_ACTIVE = 20
    REVISION_CONFLICT = 21
    REVISION_MISSING = 22
    STORAGE_FAILURE = 23
    SQLITE = 24
    CANCELLED = 25
    STALE_EPOCH = 26
    INVALID_TABLE_NAME = 27
    INVALID_STATE = 28
    UNSUPPORTED = 29


@dataclass(slots=True)
class SqlConnection:
    handle: tuple[int, int]
    database: apsw.Connection
    durable_revision: bytes | None = None
    durable_bytes: bytes | None = None
    chain: SqlChain | None = None
    active_write_probe: list[bool] | None = None


@dataclass(slots=True)
class SqlReader:
    handle: tuple[int, int]
    connection: SqlConnection
    cursor: apsw.Cursor | None
    write_detected: bool
    status: int = 0
    rows_read: int = 0
    row: tuple[Any, ...] | None = None


class SqlProviderFault(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        context: dict[str, str] | None = None,
        sqlite_code: int | None = None,
        connection: SqlConnection | None = None,
        reader: SqlReader | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}
        self.sqlite_code = sqlite_code
        self.connection = connection
        self.reader = reader


class SqlProvider:
    """Own all native SQLite state on the RuntimeWorker thread."""

    def __init__(self) -> None:
        self.provider: tuple[int, int] | None = None
        self.connections: dict[tuple[int, int], SqlConnection] = {}
        self.readers: dict[tuple[int, int], SqlReader] = {}
        self.next_reader_id = 1
        self.revisions = SqlRevisionStore()

    def reset(self) -> None:
        for connection in tuple(self.connections.values()):
            self._close_connection(connection)
        self.connections.clear()
        self.readers.clear()
        self.provider = None
        self.next_reader_id = 1

    def handle(
        self,
        payload: bytes,
        storage: StorageLike,
        deadline_ns: int | None,
    ) -> bytes:
        operation_kind = 0
        provider: tuple[int, int] | None = None
        operation: tuple[int, list[Any]] | None = None
        deadline = _OperationDeadline(deadline_ns)
        try:
            deadline.checkpoint()
            request = _map(decode(payload), {0, 1}, "SQL request")
            provider = _handle(request[0], "SQL provider")
            self._enter_provider(provider)
            operation_kind, fields = unwrap_variant(request[1])
            operation = operation_kind, fields
            response = self._execute(provider, operation_kind, fields, storage, deadline)
        except SqlRevisionStoreError as error:
            fault = SqlProviderFault(error.code, str(error), context=error.context)
            response = self._error_response(
                provider or self.provider or (0, 0), operation_kind, operation, fault
            )
        except SqlProviderFault as error:
            response = self._error_response(
                provider or self.provider or (0, 0), operation_kind, operation, error
            )
        except apsw.InterruptError as error:
            fault = SqlProviderFault(
                SqlErrorCode.EXECUTION_TIMEOUT,
                "SQL execution budget exceeded",
                sqlite_code=getattr(error, "extendedresult", None),
            )
            response = self._error_response(
                provider or self.provider or (0, 0), operation_kind, operation, fault
            )
        except apsw.Error as error:
            fault = SqlProviderFault(
                SqlErrorCode.SQLITE,
                str(error),
                sqlite_code=getattr(error, "extendedresult", None),
            )
            response = self._error_response(
                provider or self.provider or (0, 0), operation_kind, operation, fault
            )
        except (KeyError, TypeError, ValueError) as error:
            fault = SqlProviderFault(SqlErrorCode.INVALID_REQUEST, str(error))
            response = self._error_response(
                provider or self.provider or (0, 0), operation_kind, operation, fault
            )
        return encode(response)

    def _enter_provider(self, provider: tuple[int, int]) -> None:
        if self.provider == provider:
            return
        self.reset()
        self.provider = provider

    def _execute(
        self,
        provider: tuple[int, int],
        kind: int,
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        if kind == 0:
            return self._open(provider, fields, storage, deadline)
        if kind == 1:
            return self._execute_sql(provider, fields, storage, deadline)
        if kind == 2:
            return self._reader_read(provider, fields, storage, deadline)
        if kind == 3:
            return self._reader_get(provider, fields)
        if kind == 4:
            return self._reader_is_null(provider, fields)
        if kind == 5:
            return self._reader_close(provider, fields, storage, deadline)
        if kind == 6:
            return self._import_map(provider, fields, storage, deadline)
        if kind == 7:
            return self._disconnect(provider, fields)
        raise SqlProviderFault(SqlErrorCode.INVALID_REQUEST, "unknown SQL operation")

    def _open(
        self,
        provider: tuple[int, int],
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        _length(fields, 5, "SQL open")
        handle = _handle(fields[0], "SQL connection")
        self._same_epoch(provider, handle)
        logical_name = _text(fields[1], "SQL logical name")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", logical_name):
            raise SqlProviderFault(SqlErrorCode.INVALID_NAME, "invalid SQL logical connection name")
        if handle in self.connections:
            raise SqlProviderFault(
                SqlErrorCode.CONNECTION_CONFLICT, "SQL connection already exists"
            )
        if len(self.connections) >= MAXIMUM_CONNECTIONS:
            raise SqlProviderFault(SqlErrorCode.CONNECTION_LIMIT, "SQL connection limit exceeded")
        identity = _map(fields[2], {0, 1, 2}, "SQL identity")
        if identity[1] != SQLITE_VERSION or identity[2] != DATABASE_FORMAT_VERSION:
            raise SqlProviderFault(SqlErrorCode.UNSUPPORTED, "unsupported SQL database identity")
        if _map(fields[4], set(LIMITS), "SQL limits") != LIMITS:
            raise SqlProviderFault(SqlErrorCode.INVALID_REQUEST, "SQL limits do not match v1")
        revision_tag, revision_fields = unwrap_variant(fields[3])
        if apsw.sqlite_lib_version() != SQLITE_VERSION:
            raise SqlProviderFault(SqlErrorCode.UNSUPPORTED, "SQLite provider version mismatch")
        database = apsw.Connection(":memory:")
        connection = SqlConnection(handle, database)
        try:
            source_tag, source_fields = unwrap_variant(identity[0])
            if source_tag == 0:
                _length(source_fields, 0, "memory SQL source")
                if revision_tag != 0 or revision_fields:
                    raise SqlProviderFault(
                        SqlErrorCode.INVALID_REQUEST, "memory SQL cannot open an exact revision"
                    )
            elif source_tag == 1:
                _length(source_fields, 1, "resource SQL source")
                seed = _map(source_fields[0], {0, 1}, "SQL resource seed")
                resource_id = safe_resource_id(_text(seed[0], "SQL resource ID"))
                seed_sha = _bytes32(seed[1], "SQL seed SHA-256")
                exact = None
                if revision_tag == 1:
                    _length(revision_fields, 1, "exact SQL revision")
                    exact = _bytes32(
                        _map(revision_fields[0], {0}, "SQL revision")[0], "SQL revision SHA-256"
                    )
                elif revision_tag != 0 or revision_fields:
                    raise SqlProviderFault(
                        SqlErrorCode.INVALID_REQUEST, "invalid SQL open revision"
                    )
                material, durable, chain = self.revisions.open_resource(
                    storage,
                    resource_id,
                    seed_sha,
                    exact,
                    deadline,
                    self._validate_resource_seed,
                )
                try:
                    database.deserialize("main", material)
                except apsw.Error as error:
                    raise SqlProviderFault(
                        SqlErrorCode.STORAGE_FAILURE,
                        "SQL stored revision is not a valid SQLite database",
                    ) from error
                connection.durable_revision = durable
                connection.durable_bytes = material
                connection.chain = chain
            else:
                raise SqlProviderFault(SqlErrorCode.INVALID_SOURCE, "unsupported SQL source")
            self._configure_database(connection)
            deadline.checkpoint()
        except BaseException:
            database.close(force=True)
            raise
        self.connections[handle] = connection
        return self._response(provider, variant(0, SQLITE_VERSION, dict(LIMITS)), connection)

    def _execute_sql(
        self,
        provider: tuple[int, int],
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        _length(fields, 4, "SQL execute")
        connection = self._connection(provider, fields[0])
        mode = _unsigned(fields[1], "SQL execute mode", 3)
        sql = _text(fields[2], "SQL text")
        if len(sql.encode("utf-8")) > MAXIMUM_SQL_BYTES:
            raise SqlProviderFault(SqlErrorCode.SQL_TOO_LARGE, "SQL text exceeds its limit")
        parameters = _values(fields[3])
        self._validate_parameters(parameters)
        bindings = {str(index): value for index, value in enumerate(parameters)}
        probe = [False]
        try:
            with self._write_probe(connection, probe), self._budget(connection.database, deadline):
                cursor = (
                    connection.database.execute(sql, bindings)
                    if parameters
                    else connection.database.execute(sql)
                )
                if mode == 3:
                    if len(self.readers) >= MAXIMUM_READERS:
                        cursor.close(force=True)
                        raise SqlProviderFault(
                            SqlErrorCode.READER_LIMIT, "SQL reader limit exceeded"
                        )
                    reader_handle = self._new_reader_handle(provider[0])
                    reader = SqlReader(reader_handle, connection, cursor, probe[0])
                    self.readers[reader_handle] = reader
                    return self._response(
                        provider, variant(3, _handle_map(reader_handle)), connection, reader
                    )
                try:
                    row = cursor.fetchone()
                    if mode == 0:
                        while row is not None:
                            row = cursor.fetchone()
                        result = variant(1, connection.database.changes())
                    else:
                        raw = None if row is None or not row else _sql_value(row[0])
                        value = _scalar_value(raw, mode)
                        result = variant(2, _value_variant(value))
                finally:
                    cursor.close(force=True)
        except BaseException:
            if probe[0] and connection.chain is not None and connection.database.get_autocommit():
                self._restore_durable(connection)
            raise
        if probe[0]:
            self._publish_if_autocommit(connection, storage, deadline)
        return self._response(provider, result, connection)

    def _reader_read(
        self,
        provider: tuple[int, int],
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        _length(fields, 1, "SQL reader read")
        handle = _handle(fields[0], "SQL reader")
        self._same_epoch(provider, handle)
        reader = self.readers.get(handle)
        if reader is None:
            raise SqlProviderFault(SqlErrorCode.READER_NOT_FOUND, "SQL reader is not open")
        if reader.status == 2:
            return self._response(provider, variant(4, False), reader.connection, reader)
        if reader.cursor is None:
            raise SqlProviderFault(SqlErrorCode.INVALID_STATE, "SQL reader cursor is unavailable")
        probe = [False]
        with (
            self._write_probe(reader.connection, probe),
            self._budget(reader.connection.database, deadline),
        ):
            row = reader.cursor.fetchone()
        reader.write_detected = reader.write_detected or probe[0]
        if row is None:
            reader.row = None
            reader.status = 2
            if reader.write_detected:
                self._finalize_reader_write(reader, storage, deadline)
            return self._response(provider, variant(4, False), reader.connection, reader)
        if reader.rows_read >= MAXIMUM_READER_ROWS:
            self._close_reader(reader)
            raise SqlProviderFault(
                SqlErrorCode.READER_ROW_LIMIT,
                "SQL reader row limit exceeded",
                connection=reader.connection,
                reader=reader,
            )
        reader.rows_read += 1
        reader.row = tuple(row)
        reader.status = 1
        return self._response(provider, variant(4, True), reader.connection, reader)

    def _reader_get(self, provider: tuple[int, int], fields: list[Any]) -> dict[int, Any]:
        _length(fields, 3, "SQL reader column")
        handle = _handle(fields[0], "SQL reader")
        self._same_epoch(provider, handle)
        reader = self.readers.get(handle)
        if reader is None or reader.status != 1 or reader.row is None:
            raise SqlProviderFault(SqlErrorCode.READER_NOT_FOUND, "SQL reader has no current row")
        column = _unsigned(fields[1], "SQL reader column", 0xFFFF_FFFF)
        mode = _unsigned(fields[2], "SQL reader value mode", 1)
        if column >= len(reader.row):
            raise SqlProviderFault(SqlErrorCode.COLUMN_OUT_OF_RANGE, "SQL reader column is invalid")
        value = _reader_value(_sql_value(reader.row[column]), mode, reader.connection.database)
        return self._response(
            provider, variant(5, _value_variant(value)), reader.connection, reader
        )

    def _reader_is_null(self, provider: tuple[int, int], fields: list[Any]) -> dict[int, Any]:
        _length(fields, 2, "SQL reader column")
        handle = _handle(fields[0], "SQL reader")
        self._same_epoch(provider, handle)
        reader = self.readers.get(handle)
        if reader is None or reader.status != 1 or reader.row is None:
            raise SqlProviderFault(SqlErrorCode.READER_NOT_FOUND, "SQL reader has no current row")
        column = _unsigned(fields[1], "SQL reader column", 0xFFFF_FFFF)
        if column >= len(reader.row):
            raise SqlProviderFault(SqlErrorCode.COLUMN_OUT_OF_RANGE, "SQL reader column is invalid")
        return self._response(
            provider,
            variant(6, reader.row[column] is None),
            reader.connection,
            reader,
        )

    def _reader_close(
        self,
        provider: tuple[int, int],
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        _length(fields, 1, "SQL reader close")
        handle = _handle(fields[0], "SQL reader")
        self._same_epoch(provider, handle)
        reader = self.readers.get(handle)
        if reader is None:
            raise SqlProviderFault(SqlErrorCode.READER_NOT_FOUND, "SQL reader is not open")
        if reader.write_detected:
            self._finalize_reader_write(reader, storage, deadline)
        self._close_reader(reader)
        return self._response(provider, variant(7), reader.connection, reader)

    def _import_map(
        self,
        provider: tuple[int, int],
        fields: list[Any],
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> dict[int, Any]:
        _length(fields, 3, "SQL MAP import")
        connection = self._connection(provider, fields[0])
        table = _text(fields[1], "SQL MAP table")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise SqlProviderFault(SqlErrorCode.INVALID_TABLE_NAME, "invalid SQL MAP table name")
        raw_rows = fields[2]
        if not isinstance(raw_rows, list):
            raise SqlProviderFault(SqlErrorCode.INVALID_REQUEST, "SQL MAP rows are not a list")
        if len(raw_rows) > MAXIMUM_MAP_ROWS:
            raise SqlProviderFault(SqlErrorCode.MAP_ROW_LIMIT, "SQL MAP row limit exceeded")
        rows: list[tuple[str, str]] = []
        byte_length = 0
        for raw in raw_rows:
            deadline.checkpoint()
            row = _map(raw, {0, 1}, "SQL MAP row")
            key = _text(row[0], "SQL MAP key")
            value = _text(row[1], "SQL MAP value")
            byte_length += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            rows.append((key, value))
        if byte_length > MAXIMUM_MAP_BYTES:
            raise SqlProviderFault(SqlErrorCode.MAP_BYTES_LIMIT, "SQL MAP bytes exceed their limit")
        quoted = f'"{table}"'
        with self._budget(connection.database, deadline):
            connection.database.execute("SAVEPOINT rustyera_map_import")
            try:
                connection.database.execute(
                    f"CREATE TABLE IF NOT EXISTS {quoted} (k TEXT PRIMARY KEY, v TEXT)"
                )
                connection.database.execute(f"DELETE FROM {quoted}")
                connection.database.executemany(
                    f"INSERT OR REPLACE INTO {quoted}(k, v) VALUES(?1, ?2)", rows
                )
                connection.database.execute("RELEASE rustyera_map_import")
            except BaseException:
                connection.database.execute(
                    "ROLLBACK TO rustyera_map_import; RELEASE rustyera_map_import"
                )
                raise
        self._publish_if_autocommit(connection, storage, deadline)
        return self._response(provider, variant(8, len(rows)), connection)

    def _disconnect(self, provider: tuple[int, int], fields: list[Any]) -> dict[int, Any]:
        _length(fields, 1, "SQL disconnect")
        handle = _handle(fields[0], "SQL connection")
        self._same_epoch(provider, handle)
        connection = self.connections.get(handle)
        durable_revision = connection.durable_revision if connection is not None else None
        if connection is not None:
            self._close_connection(connection)
        return {
            0: _handle_map(provider),
            1: {
                0: _handle_map(handle),
                1: False,
                2: False,
                3: {0: durable_revision} if durable_revision is not None else None,
            },
            2: None,
            3: variant(9),
        }

    def _connection(self, provider: tuple[int, int], raw: Any) -> SqlConnection:
        handle = _handle(raw, "SQL connection")
        self._same_epoch(provider, handle)
        connection = self.connections.get(handle)
        if connection is None:
            raise SqlProviderFault(SqlErrorCode.CONNECTION_NOT_FOUND, "SQL connection is not open")
        return connection

    def _same_epoch(self, provider: tuple[int, int], handle: tuple[int, int]) -> None:
        if provider[0] != handle[0]:
            raise SqlProviderFault(SqlErrorCode.STALE_EPOCH, "SQL handle belongs to another epoch")

    def _new_reader_handle(self, epoch: int) -> tuple[int, int]:
        for _ in range(MAXIMUM_READERS + 1):
            candidate = (epoch, self.next_reader_id)
            self.next_reader_id = (
                1 if self.next_reader_id >= 0xFFFF_FFFF_FFFF_FFFF else self.next_reader_id + 1
            )
            if candidate not in self.readers:
                return candidate
        raise SqlProviderFault(SqlErrorCode.READER_LIMIT, "SQL reader limit exceeded")

    def _validate_parameters(self, parameters: list[Any]) -> None:
        if len(parameters) > MAXIMUM_PARAMETERS:
            raise SqlProviderFault(SqlErrorCode.PARAMETER_LIMIT, "SQL parameter limit exceeded")
        byte_length = 0
        for value in parameters:
            value_bytes = (
                len(value.encode("utf-8")) if isinstance(value, str) else 0 if value is None else 8
            )
            if value_bytes > MAXIMUM_CELL_BYTES:
                raise SqlProviderFault(SqlErrorCode.CELL_TOO_LARGE, "SQL parameter is too large")
            byte_length += value_bytes
        if byte_length > MAXIMUM_PARAMETER_BYTES:
            raise SqlProviderFault(
                SqlErrorCode.PARAMETER_BYTES_LIMIT, "SQL parameter bytes exceed their limit"
            )

    def _budget(self, database: apsw.Connection, deadline: _OperationDeadline):
        return _SqlBudget(database, deadline)

    @contextmanager
    def _write_probe(self, connection: SqlConnection, probe: list[bool]) -> Iterator[None]:
        previous = connection.active_write_probe
        connection.active_write_probe = probe
        try:
            yield
        finally:
            connection.active_write_probe = previous

    def _configure_database(self, connection: SqlConnection) -> None:
        database = connection.database
        database.enable_load_extension(False)
        database.set_authorizer(lambda *args: self._authorize(connection, *args))
        database.limit(apsw.SQLITE_LIMIT_SQL_LENGTH, MAXIMUM_SQL_BYTES)
        database.limit(apsw.SQLITE_LIMIT_VARIABLE_NUMBER, MAXIMUM_PARAMETERS)
        database.limit(apsw.SQLITE_LIMIT_LENGTH, MAXIMUM_DATABASE_BYTES)
        database.execute("PRAGMA trusted_schema=OFF").fetchall()
        database.execute("PRAGMA temp_store=MEMORY").fetchall()
        database.execute("PRAGMA journal_mode=MEMORY").fetchall()
        page_size_row = database.execute("PRAGMA page_size").fetchone()
        page_size = int(page_size_row[0]) if page_size_row else 4096
        maximum_pages = max(1, MAXIMUM_DATABASE_BYTES // page_size)
        database.execute(f"PRAGMA max_page_count={maximum_pages}").fetchall()

    @staticmethod
    def _validate_resource_seed(contents: bytes) -> None:
        database = apsw.Connection(":memory:")
        try:
            database.deserialize("main", contents)
            database.execute("PRAGMA schema_version").fetchone()
        except apsw.Error as error:
            raise SqlProviderFault(
                SqlErrorCode.INVALID_SOURCE,
                "SQL Resource seed is not a valid SQLite database",
            ) from error
        finally:
            database.close(force=True)

    @staticmethod
    def _authorize(
        connection: SqlConnection,
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        denied = {
            apsw.SQLITE_ATTACH,
            apsw.SQLITE_DETACH,
            apsw.SQLITE_CREATE_VTABLE,
            apsw.SQLITE_DROP_VTABLE,
        }
        if action in denied:
            return apsw.SQLITE_DENY
        if action == apsw.SQLITE_FUNCTION and (argument_two or argument_one) == "load_extension":
            return apsw.SQLITE_DENY
        if action == apsw.SQLITE_PRAGMA and (argument_one or "").lower() in {
            "data_store_directory",
            "temp_store_directory",
        }:
            return apsw.SQLITE_DENY
        write_actions = {
            apsw.SQLITE_INSERT,
            apsw.SQLITE_UPDATE,
            apsw.SQLITE_DELETE,
            apsw.SQLITE_CREATE_INDEX,
            apsw.SQLITE_CREATE_TABLE,
            apsw.SQLITE_CREATE_TEMP_INDEX,
            apsw.SQLITE_CREATE_TEMP_TABLE,
            apsw.SQLITE_CREATE_TEMP_TRIGGER,
            apsw.SQLITE_CREATE_TEMP_VIEW,
            apsw.SQLITE_CREATE_TRIGGER,
            apsw.SQLITE_CREATE_VIEW,
            apsw.SQLITE_DROP_INDEX,
            apsw.SQLITE_DROP_TABLE,
            apsw.SQLITE_DROP_TEMP_INDEX,
            apsw.SQLITE_DROP_TEMP_TABLE,
            apsw.SQLITE_DROP_TEMP_TRIGGER,
            apsw.SQLITE_DROP_TEMP_VIEW,
            apsw.SQLITE_DROP_TRIGGER,
            apsw.SQLITE_DROP_VIEW,
            apsw.SQLITE_ALTER_TABLE,
            apsw.SQLITE_REINDEX,
            apsw.SQLITE_ANALYZE,
            apsw.SQLITE_TRANSACTION,
            apsw.SQLITE_SAVEPOINT,
        }
        if action in write_actions and connection.active_write_probe is not None:
            connection.active_write_probe[0] = True
        return apsw.SQLITE_OK

    def _finalize_reader_write(
        self,
        reader: SqlReader,
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> None:
        if reader.cursor is not None:
            reader.cursor.close(force=True)
            reader.cursor = None
        if reader.connection.database.get_autocommit():
            try:
                self._publish_if_autocommit(reader.connection, storage, deadline)
            except SqlProviderFault as error:
                reader.status = 3
                reader.row = None
                error.connection = reader.connection
                error.reader = reader
                raise

    def _publish_if_autocommit(
        self,
        connection: SqlConnection,
        storage: StorageLike,
        deadline: _OperationDeadline,
    ) -> None:
        if connection.chain is None or not connection.database.get_autocommit():
            return
        deadline.checkpoint()
        contents = bytes(connection.database.serialize("main"))
        deadline.checkpoint()
        if len(contents) > MAXIMUM_DATABASE_BYTES:
            self._restore_durable(connection)
            raise SqlProviderFault(
                SqlErrorCode.DATABASE_TOO_LARGE,
                "SQL database exceeds its limit",
                connection=connection,
            )
        revision = hashlib.sha256(contents).digest()
        if connection.durable_revision == revision:
            return
        chain = connection.chain
        if chain is None or connection.durable_revision is None:
            self._restore_durable(connection)
            raise SqlProviderFault(
                SqlErrorCode.INVALID_STATE,
                "SQL publication has no durable base",
                connection=connection,
            )
        try:
            deadline.checkpoint()
            self.revisions.publish(
                storage,
                chain,
                connection.durable_revision,
                contents,
                revision,
                deadline,
            )
        except SqlRevisionStoreError as error:
            self._restore_durable(connection)
            raise SqlProviderFault(
                error.code,
                str(error),
                context=error.context,
                connection=connection,
            ) from error
        except BaseException:
            self._restore_durable(connection)
            raise
        connection.durable_bytes = contents
        connection.durable_revision = revision

    def _restore_durable(self, connection: SqlConnection) -> None:
        for reader in tuple(self.readers.values()):
            if reader.connection is connection:
                self._close_reader(reader)
        if connection.durable_bytes is None:
            return
        previous = connection.database
        replacement = apsw.Connection(":memory:")
        try:
            replacement.deserialize("main", connection.durable_bytes)
            connection.database = replacement
            self._configure_database(connection)
        except BaseException:
            connection.database = previous
            replacement.close(force=True)
            raise
        previous.close(force=True)

    def _close_reader(self, reader: SqlReader) -> None:
        if reader.cursor is not None:
            reader.cursor.close(force=True)
            reader.cursor = None
        reader.status = 3
        reader.row = None
        self.readers.pop(reader.handle, None)

    def _close_connection(self, connection: SqlConnection) -> None:
        for reader in tuple(self.readers.values()):
            if reader.connection is connection:
                self._close_reader(reader)
        if not connection.database.get_autocommit():
            try:
                connection.database.execute("ROLLBACK")
            except apsw.Error:
                pass
        connection.database.close(force=True)
        self.connections.pop(connection.handle, None)

    def _response(
        self,
        provider: tuple[int, int],
        result: list[Any],
        connection: SqlConnection | None = None,
        reader: SqlReader | None = None,
    ) -> dict[int, Any]:
        return {
            0: _handle_map(provider),
            1: self._database_state(connection) if connection is not None else None,
            2: self._reader_state(reader) if reader is not None else None,
            3: result,
        }

    def _error_response(
        self,
        provider: tuple[int, int],
        operation_kind: int,
        operation: tuple[int, list[Any]] | None,
        error: SqlProviderFault,
    ) -> dict[int, Any]:
        connection, reader = self._operation_state(operation)
        connection = error.connection or connection
        reader = error.reader or reader
        contexts = [{0: key, 1: value} for key, value in sorted(error.context.items())]
        detail = {
            0: error.code,
            1: operation_kind if 0 <= operation_kind <= 7 else 0,
            2: contexts,
            3: error.sqlite_code,
            4: str(error),
        }
        return self._response(provider, variant(10, detail), connection, reader)

    def _operation_state(
        self, operation: tuple[int, list[Any]] | None
    ) -> tuple[SqlConnection | None, SqlReader | None]:
        if operation is None:
            return None, None
        kind, fields = operation
        try:
            if kind in (1, 6, 7) and fields:
                connection = self.connections.get(_handle(fields[0], "SQL connection"))
                return connection, None
            if kind in (2, 3, 4, 5) and fields:
                reader = self.readers.get(_handle(fields[0], "SQL reader"))
                return (reader.connection if reader else None), reader
        except (TypeError, ValueError):
            pass
        return None, None

    @staticmethod
    def _database_state(connection: SqlConnection) -> dict[int, Any]:
        return {
            0: _handle_map(connection.handle),
            1: True,
            2: not connection.database.get_autocommit(),
            3: {0: connection.durable_revision}
            if connection.durable_revision is not None
            else None,
        }

    @staticmethod
    def _reader_state(reader: SqlReader) -> dict[int, Any]:
        return {0: _handle_map(reader.handle), 1: reader.status, 2: reader.rows_read}


class _OperationDeadline:
    def __init__(self, requested_deadline_ns: int | None) -> None:
        now = time.monotonic_ns()
        local_deadline = now + EXECUTION_BUDGET_NS
        self.deadline_ns = (
            min(local_deadline, requested_deadline_ns)
            if requested_deadline_ns is not None
            else local_deadline
        )

    def expired(self) -> bool:
        return time.monotonic_ns() >= self.deadline_ns

    def checkpoint(self) -> None:
        if self.expired():
            raise SqlProviderFault(SqlErrorCode.EXECUTION_TIMEOUT, "SQL execution budget exceeded")


class _SqlBudget:
    def __init__(self, database: apsw.Connection, deadline: _OperationDeadline) -> None:
        self.database = database
        self.deadline = deadline
        self.identity = object()

    def __enter__(self) -> _SqlBudget:
        self.database.set_progress_handler(lambda: self.deadline.expired(), 1_000, id=self.identity)
        return self

    def __exit__(self, _kind: Any, _error: Any, _traceback: Any) -> None:
        self.database.set_progress_handler(None, id=self.identity)


def _map(value: Any, keys: set[int], name: str) -> dict[int, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} has an invalid CBOR map shape")
    return value


def _length(value: list[Any], expected: int, name: str) -> None:
    if len(value) != expected:
        raise ValueError(f"{name} has an invalid CBOR field count")


def _handle(value: Any, name: str) -> tuple[int, int]:
    fields = _map(value, {0, 1}, name)
    return _unsigned(fields[0], f"{name} epoch"), _unsigned(fields[1], f"{name} ID")


def _handle_map(value: tuple[int, int]) -> dict[int, int]:
    return {0: value[0], 1: value[1]}


def _unsigned(value: Any, name: str, maximum: int = 0xFFFF_FFFF_FFFF_FFFF) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} is not an unsigned integer")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is not text")
    return value


def _bytes32(value: Any, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} is not 32 bytes")
    return value


def _values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("SQL parameters are not a list")
    return [_decode_value(item) for item in value]


def _decode_value(value: Any) -> Any:
    tag, fields = unwrap_variant(value)
    if tag == 0 and not fields:
        return None
    if tag == 1 and len(fields) == 1 and type(fields[0]) is int:
        return fields[0]
    if tag == 2 and len(fields) == 1 and isinstance(fields[0], str):
        return fields[0]
    raise ValueError("invalid SQL value")


def _sql_value(value: Any) -> Any:
    if value is None or type(value) is int:
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAXIMUM_CELL_BYTES:
            raise SqlProviderFault(SqlErrorCode.CELL_TOO_LARGE, "SQL cell exceeds its limit")
        return value
    raise SqlProviderFault(SqlErrorCode.TYPE_MISMATCH, "SQL value type is unsupported by v1")


def _scalar_value(value: Any, mode: int) -> Any:
    if value is None:
        return None
    if mode == 1:
        if type(value) is int:
            return value
        if not re.fullmatch(r"[+-]?\d+", value.strip()):
            raise SqlProviderFault(SqlErrorCode.TYPE_MISMATCH, "SQL scalar is not an integer")
        integer = int(value.strip())
        if not -(1 << 63) <= integer < 1 << 63:
            raise SqlProviderFault(
                SqlErrorCode.TYPE_MISMATCH,
                "SQL scalar integer is out of range",
            )
        return integer
    if mode == 2:
        return str(value)
    raise SqlProviderFault(SqlErrorCode.INVALID_REQUEST, "invalid SQL scalar mode")


def _reader_value(value: Any, mode: int, _database: apsw.Connection) -> Any:
    if value is None:
        return None
    if mode == 0:
        return _scalar_value(value, 1)
    return str(value)


def _value_variant(value: Any) -> list[Any]:
    if value is None:
        return variant(0)
    if type(value) is int:
        if not -(1 << 63) <= value < 1 << 63:
            raise SqlProviderFault(SqlErrorCode.TYPE_MISMATCH, "SQL integer exceeds i64")
        return variant(1, value)
    return variant(2, value)
