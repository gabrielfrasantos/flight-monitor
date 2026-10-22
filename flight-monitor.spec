
import sys

_WINDOWS = sys.platform == "win32"

a = Analysis(
    ["flight_monitor_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "fli.models",
        "fli.search",
        "fli.search.client",
        "fast_flights",
        "fast_flights.fetcher",
        "fast_flights.parser",
        "primp",
        "keyring.backends.Windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "fli.cli", "fli.mcp"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="flight-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico" if _WINDOWS else "assets/icon.png",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="flight-monitor",
)
