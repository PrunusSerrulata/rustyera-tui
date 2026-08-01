from pathlib import Path

from rustyera_tui.__main__ import build_parser


def test_resource_directory_is_omitted_when_not_specified() -> None:
    args = build_parser().parse_args([])

    assert args.resource_directory is None
    assert args.project_file is None


def test_resource_directory_accepts_an_explicit_path(tmp_path: Path) -> None:
    args = build_parser().parse_args([str(tmp_path)])

    assert args.resource_directory == tmp_path


def test_project_file_option_coexists_with_higher_priority_directory(tmp_path: Path) -> None:
    project_file = tmp_path / "game.reraproj"
    args = build_parser().parse_args([str(tmp_path), "--project-file", str(project_file)])

    assert args.resource_directory == tmp_path
    assert args.project_file == project_file
