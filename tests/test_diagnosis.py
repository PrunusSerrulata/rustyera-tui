from __future__ import annotations

import tarfile
from datetime import datetime
from pathlib import Path

import zstandard

from rustyera_tui.diagnosis import diagnosis_default_path, write_diagnosis_archive


def test_diagnosis_archive_has_the_required_named_payloads(tmp_path: Path) -> None:
    project = tmp_path / "eraTW"
    project.mkdir()
    target = diagnosis_default_path(project, datetime(2026, 7, 26, 14, 5, 6))
    assert target.name == "eraTW-diagnosis_20260726-140506.tar.zst"

    write_diagnosis_archive(
        target,
        project_name="eraTW",
        snapshot=b"snapshot",
        logs="first\nlast\n",
        compiled_artifact=b"artifact",
        exported_at=datetime(2026, 7, 26, 14, 5, 6),
    )

    with target.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                members = {
                    member.name: archive.extractfile(member).read()
                    for member in archive
                    if member.isfile()
                }
    assert members == {
        "runtime.snapshot": b"snapshot",
        "runtime.log": b"first\nlast\n",
        "eraTW-compiled-project.bin.zst": b"artifact",
    }
