from __future__ import annotations

import tarfile
from datetime import datetime
from pathlib import Path

import zstandard

from rustyera_tui.diagnosis import diagnosis_default_path, write_diagnosis_archive


def test_diagnosis_archive_has_the_required_named_payloads(tmp_path: Path) -> None:
    project = tmp_path / "eraTW"
    project.mkdir()
    target = diagnosis_default_path(
        project,
        datetime(2026, 7, 26, 14, 5, 6),
        project_name="eraThe World",
    )
    assert target.name == "eraThe World-diagnosis_20260726-140506.tar.zst"

    write_diagnosis_archive(
        target,
        project_name="eraThe World",
        snapshot=b"snapshot",
        input_replay=b'{"record":"header"}\n',
        logs="first\nlast\n",
        project_file=b"RERAPROJartifact",
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
        "input-replay.jsonl": b'{"record":"header"}\n',
        "eraThe World.reraproj": b"RERAPROJartifact",
    }


def test_diagnosis_name_uses_a_safe_project_title_instead_of_the_folder_name(
    tmp_path: Path,
) -> None:
    project = tmp_path / "renamed-folder"
    project.mkdir()

    target = diagnosis_default_path(
        project,
        datetime(2026, 7, 26, 14, 5, 6),
        project_name="era/The World",
    )

    assert target.name == "era_The World-diagnosis_20260726-140506.tar.zst"
