# PyInstaller spec for the Flight Monitor desktop app.
#
#   python scripts/generate_icon.py          # assets/icon.ico + icon.png
#   pyinstaller --noconfirm flight-monitor.spec
#
# Output: dist/flight-monitor/ -- one folder, which Inno Setup packs into the
# Windows installer. A folder rather than one-file: the app runs all day in the
# tray, so it shouldn't unpack itself to %TEMP% on every start.

import sys

_WINDOWS = sys.platform == "win32"

a = Analysis(
    ["flight_monitor_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # The engines import their libraries inside functions (one missing library
    # disables one engine); name them so they are always bundled.
    hiddenimports=[
        "fli.models",
        "fli.search",
        "fli.search.client",
        "fast_flights",
        "fast_flights.fetcher",
        "fast_flights.parser",
        "primp",
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
    upx=False,  # UPX-packed Qt DLLs break and trip antivirus
    console=False,  # no console window on Windows
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
