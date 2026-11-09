"""QR codes: the payload is canonical JSON, written as SVG and PNG."""
from __future__ import annotations

from pathlib import Path

import qrcode
import qrcode.image.svg

from ..crypto import canonical


def qr_payload(**fields) -> str:
    return canonical(fields).decode()


def write_qr(payload: str, out_dir, name: str) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    svg, png = out_dir / f"{name}.svg", out_dir / f"{name}.png"
    qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage).save(str(svg))
    qrcode.make(payload).save(str(png))  # PNG via Pillow (installed with reportlab)
    return svg, png
