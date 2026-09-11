"""Validate upstream policy identity in an existing real RuntimeWorker trace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_trace(path: Path, profile: str) -> dict[str, object]:
    expected_profile, expected_version = (1, 15) if profile == "snake" else (0, 3)
    identities: list[dict[str, object]] = []
    result: dict[str, object] | None = None
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") == "error":
                raise ValueError(f"trace contains a driver error: {record}")
            if record.get("type") == "result":
                result = record
            if record.get("type") != "observation":
                continue
            for entry in record.get("rust", {}).get("project_load_reports", []):
                report = entry["report"]
                if report.get("1") is True:
                    identity = report.get("6")
                    if not isinstance(identity, dict):
                        raise ValueError("successful project report lacks compatibility identity")
                    expected = {"0": expected_profile, "1": expected_version, "2": expected_version}
                    if any(identity.get(key) != value for key, value in expected.items()):
                        raise ValueError(f"unexpected runtime compatibility identity: {identity}")
                    identities.append(identity)
    if result is None or result.get("status") != "passed":
        raise ValueError(f"trace does not finish with a passed scenario: {result}")
    if not identities:
        raise ValueError("trace contains no successful public project-load report")
    return {"profile": profile, "identity": identities[-1], "scenario_status": result["status"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--profile", choices=("original", "snake"), required=True)
    arguments = parser.parse_args()
    print(json.dumps(validate_trace(arguments.trace, arguments.profile), ensure_ascii=False))


if __name__ == "__main__":
    main()
