from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


TUI_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = TUI_ROOT.parent / "rustyera-core"
LOCKFILE = CORE_ROOT / "Cargo.lock"
STATE = TUI_ROOT / ".rustyera" / "cargo-local"
BACKUP = STATE / "Cargo.lock.remote"
OWNER = STATE / "owner.json"


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        if os.name == "nt":
            # Windows can report invalid stale PIDs as generic WinErrors instead of
            # ProcessLookupError (for example ERROR_BAD_FORMAT for a large PID).
            return False
        raise
    return True


def restore_lockfile() -> None:
    if not BACKUP.exists():
        return
    temporary = LOCKFILE.with_name(f"Cargo.lock.cargo-local-{os.getpid()}")
    shutil.copy2(BACKUP, temporary)
    temporary.replace(LOCKFILE)


def recover_stale_state() -> None:
    if not STATE.exists():
        return
    pid: int | None = None
    try:
        value = json.loads(OWNER.read_text(encoding="utf-8")).get("pid")
        if isinstance(value, int):
            pid = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    if pid is not None and process_is_alive(pid):
        raise RuntimeError(f"另一个本地 runtime 构建正在运行（PID {pid}）")
    if pid is None and time.time() - STATE.stat().st_mtime < 5:
        raise RuntimeError("另一个本地 runtime 构建正在初始化")
    restore_lockfile()
    shutil.rmtree(STATE)
    print("已从异常中断的本地 runtime 构建恢复 rustyera-core/Cargo.lock", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the sibling runtime without changing its committed Cargo.lock"
    )
    parser.add_argument("--profile", default="release")
    parser.add_argument("cargo_args", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not LOCKFILE.is_file():
        raise RuntimeError(f"未找到兄弟 core 锁文件：{LOCKFILE}")

    recover_stale_state()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.mkdir()
    shutil.copy2(LOCKFILE, BACKUP)
    OWNER.write_text(json.dumps({"pid": os.getpid()}) + "\n", encoding="utf-8")

    cargo = os.environ.get("RUSTYERA_CARGO", "cargo")
    command = [
        cargo,
        "build",
        "--manifest-path",
        str(CORE_ROOT / "Cargo.toml"),
        "--package",
        "era-runtime-capi",
        "--profile",
        args.profile,
        *args.cargo_args,
    ]
    child: subprocess.Popen[bytes] | None = None

    def forward_signal(signum: int, _frame: object) -> None:
        if child is not None:
            child.send_signal(signum)

    previous_handlers = {
        signum: signal.signal(signum, forward_signal) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        child = subprocess.Popen(command, cwd=TUI_ROOT)
        return child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        restore_lockfile()
        shutil.rmtree(STATE)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
