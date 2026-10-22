#!/usr/bin/env python3
"""Generate assets/icon.png and assets/icon.ico for PyInstaller and the installer.

Run before building (headless: set QT_QPA_PLATFORM=offscreen):
    python scripts/generate_icon.py

Draws the same icon the app renders at runtime (desktop.render_icon).
Produces:
    assets/icon.png   256x256 RGBA
    assets/icon.ico   multi-size ICO (16-256), for the .exe and the installer
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _build_ico(pngs: dict[int, bytes]) -> bytes:
    """Pack PNG frames into an ICO file (PNG-compressed entries, Vista and later)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in sorted(pngs.items()):
        dim = 0 if size >= 256 else size  # 0 means 256 in ICO
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication

    from desktop import render_icon

    app = QApplication.instance() or QApplication(sys.argv[:1])

    def png(size: int) -> bytes:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render_icon(size).save(buf, "PNG")
        buf.close()
        return bytes(buf.data())

    ASSETS.mkdir(exist_ok=True)
    pngs = {size: png(size) for size in SIZES}
    (ASSETS / "icon.png").write_bytes(pngs[256])
    (ASSETS / "icon.ico").write_bytes(_build_ico(pngs))
    print(f"Written {ASSETS / 'icon.png'} and {ASSETS / 'icon.ico'}")
    del app


if __name__ == "__main__":
    main()
