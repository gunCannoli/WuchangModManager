"""Generate WMM app icon: dark rounded square with Material Design 'feather' icon (SVG, PNG, multi-size ICO)."""

from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

# Exact Material Design Icons 'feather' (mdi:feather, 24x24 viewBox) on a dark rounded square
WMM_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#252830"/>
      <stop offset="100%" stop-color="#16181D"/>
    </linearGradient>
    <linearGradient id="feather" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0%" stop-color="#B84925"/>
      <stop offset="60%" stop-color="#D9683F"/>
      <stop offset="100%" stop-color="#F08B63"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="240" height="240" rx="52" ry="52" fill="url(#bg)" stroke="#383D49" stroke-width="6"/>
  <g transform="translate(36, 36) scale(7.6667)">
    <path fill="url(#feather)" d="M22,2C22,2 14.36,1.63 8.34,9.88C3.72,16.21 2,22 2,22L3.94,21C5.38,18.5 6.13,17.47 7.54,16C10.07,16.74 12.71,16.65 15,14C13,13.44 11.4,13.57 9.04,13.81C11.69,12 13.5,11.6 16,12L17,10C15.2,9.66 14,9.63 12.22,10.04C14.19,8.65 15.56,7.87 18,8L19.21,6.07C17.65,5.96 16.71,6.13 14.92,6.57C16.53,5.11 18,4.45 20.14,4.32C20.14,4.32 21.19,2.43 22,2Z"/>
  </g>
</svg>
"""


def render_png_bytes(svg_bytes: bytes, size: int) -> bytes:
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(ico_path: Path, png_images: list[tuple[int, bytes]]) -> None:
    """Write a standard Windows multi-resolution PNG-in-ICO file."""
    count = len(png_images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    offset = 6 + 16 * count
    blobs = bytearray()
    for size, data in png_images:
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    ico_path.write_bytes(header + bytes(entries) + bytes(blobs))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    svg_bytes = WMM_ICON_SVG.encode("utf-8")
    (assets / "wmm.svg").write_bytes(svg_bytes)

    sizes = [16, 32, 48, 64, 128, 256]
    pngs = [(s, render_png_bytes(svg_bytes, s)) for s in sizes]
    (assets / "wmm.png").write_bytes(pngs[-1][1])
    write_ico(assets / "wmm.ico", pngs)
    print(f"Generated {assets / 'wmm.svg'}, {assets / 'wmm.png'}, {assets / 'wmm.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
