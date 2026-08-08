from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "build_runtime_local.py"
REMOTE_LOCK = "remote core lock\n"


def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    tui = workspace / "rustyera-tui"
    core = workspace / "rustyera-core"
    scripts = tui / "scripts"
    scripts.mkdir(parents=True)
    core.mkdir()
    shutil.copy2(WRAPPER, scripts / WRAPPER.name)
    (core / "Cargo.lock").write_text(REMOTE_LOCK, encoding="utf-8")
    (core / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    fake_script = workspace / "fake_cargo.py"
    fake_script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "lockfile = Path(sys.argv[sys.argv.index('--manifest-path') + 1]).parent / 'Cargo.lock'\n"
        "lockfile.write_text('local core lock\\n', encoding='utf-8')\n"
        "raise SystemExit(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 0)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake_cargo = workspace / "fake_cargo.cmd"
        fake_cargo.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n', encoding="utf-8"
        )
    else:
        fake_cargo = workspace / "fake_cargo"
        fake_cargo.write_text(
            f"#!{sys.executable}\n{fake_script.read_text(encoding='utf-8')}", encoding="utf-8"
        )
        fake_cargo.chmod(0o755)
    return tui, core, fake_cargo


def run_wrapper(
    tui: Path, fake_cargo: Path, exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "RUSTYERA_CARGO": str(fake_cargo)}
    return subprocess.run(
        [sys.executable, tui / "scripts" / WRAPPER.name, "--", fake_cargo, str(exit_code)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_restores_remote_lock_after_success(tmp_path: Path) -> None:
    tui, core, fake_cargo = fixture(tmp_path)

    result = run_wrapper(tui, fake_cargo)

    assert result.returncode == 0
    assert (core / "Cargo.lock").read_text(encoding="utf-8") == REMOTE_LOCK


def test_restores_remote_lock_after_cargo_failure(tmp_path: Path) -> None:
    tui, core, fake_cargo = fixture(tmp_path)

    result = run_wrapper(tui, fake_cargo, 23)

    assert result.returncode == 23
    assert (core / "Cargo.lock").read_text(encoding="utf-8") == REMOTE_LOCK


def test_recovers_lock_from_an_interrupted_build(tmp_path: Path) -> None:
    tui, core, fake_cargo = fixture(tmp_path)
    state = tui / ".rustyera" / "cargo-local"
    state.mkdir(parents=True)
    (core / "Cargo.lock").write_text("abandoned local lock\n", encoding="utf-8")
    (state / "Cargo.lock.remote").write_text(REMOTE_LOCK, encoding="utf-8")
    (state / "owner.json").write_text('{"pid": 99999999}\n', encoding="utf-8")

    result = run_wrapper(tui, fake_cargo)

    assert result.returncode == 0
    assert "已从异常中断的本地 runtime 构建恢复" in result.stderr
    assert (core / "Cargo.lock").read_text(encoding="utf-8") == REMOTE_LOCK


def test_does_not_recover_an_initializing_build(tmp_path: Path) -> None:
    tui, core, fake_cargo = fixture(tmp_path)
    (tui / ".rustyera" / "cargo-local").mkdir(parents=True)

    result = run_wrapper(tui, fake_cargo)

    assert result.returncode == 1
    assert "另一个本地 runtime 构建正在初始化" in result.stderr
    assert (core / "Cargo.lock").read_text(encoding="utf-8") == REMOTE_LOCK
