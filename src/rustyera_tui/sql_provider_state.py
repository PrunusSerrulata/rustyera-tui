"""Connection state and execution budgets for the SQL provider."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import apsw

from .sql_revision_store import SqlChain

EXECUTION_BUDGET_NS = 5_000_000_000


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
    allow_bare_vacuum: bool = False


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
