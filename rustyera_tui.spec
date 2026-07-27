# rustyera_tui.spec

import platform
from pathlib import Path

project_root = Path(SPEC).resolve().parent
package_dir = project_root / "src" / "rustyera_tui"

system = platform.system()
machine = platform.machine().lower()

if system == "Windows":
    if machine == "amd64":
        library = package_dir / "era_runtime_capi.dll"
    else:
        raise Exception(f"Unsupported architecture: {machine}")
elif system == "Linux":
    library = package_dir / "libera_runtime_capi.so"
elif system == "Darwin":
    library = package_dir / "libera_runtime_capi.dylib"
else:
    raise Exception(f"Unsupported operating system: {system}")

if not library.exists():
    raise FileNotFoundError(f"Native library does not exist: {library}")

a = Analysis(
    ["entry.py"],
    pathex=["src"],
    binaries=[(str(library), "rustyera_tui")],
    datas=[("src/rustyera_tui/app.tcss", "rustyera_tui")],
    hiddenimports = [
        "rich",
        "rich.console",
        "rich.panel",
        "rich.table",
        "rich.text",
        "rich.theme",
        "rich.style",
        "rich.box",
        "rich.progress",
        "rich.spinner",
        "rich.traceback",
        "rich.prompt",
        "rich.markdown",
        "rich.syntax",
    ],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rustyera_tui",
    debug=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="rustyera_tui",
)
