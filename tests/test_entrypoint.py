from pathlib import Path

from rustyera_tui.__main__ import build_parser


def test_resource_directory_defaults_to_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args([])

    assert args.resource_directory == tmp_path


def test_resource_directory_accepts_an_explicit_path(tmp_path: Path) -> None:
    args = build_parser().parse_args([str(tmp_path)])

    assert args.resource_directory == tmp_path
