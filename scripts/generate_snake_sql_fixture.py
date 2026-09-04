#!/usr/bin/env python3
"""Regenerate the shared three-client SQLite seed fixture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import apsw


PROJECT = Path(__file__).parents[1] / "tests" / "fixtures" / "snake-sql-project"
SEED = PROJECT / "plugins" / "qol_data.seed.sql"
DATABASE = PROJECT / "plugins" / "qol_data.db"
CONTRACT = PROJECT / "contract.json"


def main() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if contract["schemaVersion"] != 1:
        raise RuntimeError(f"unsupported contract schema {contract['schemaVersion']}")
    if apsw.sqlitelibversion() != contract["sqliteVersion"]:
        raise RuntimeError(
            f"expected SQLite {contract['sqliteVersion']}, received {apsw.sqlitelibversion()}"
        )

    connection = apsw.Connection(":memory:")
    try:
        connection.execute(
            "PRAGMA page_size=4096; PRAGMA auto_vacuum=NONE; PRAGMA encoding='UTF-8'"
        )
        connection.execute(SEED.read_text(encoding="utf-8"))
        connection.execute("VACUUM")
        contents = connection.serialize("main")
    finally:
        connection.close()

    digest = hashlib.sha256(contents).hexdigest()
    if digest != contract["seedSha256"]:
        raise RuntimeError(
            f"generated fixture SHA-256 {digest} differs from {contract['seedSha256']}"
        )
    DATABASE.write_bytes(contents)
    print(json.dumps({"path": str(DATABASE), "bytes": len(contents), "sha256": digest}))


if __name__ == "__main__":
    main()
