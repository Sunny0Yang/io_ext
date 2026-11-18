#!/usr/bin/env python3
"""Write a one-page PDF that embeds the PNG at native resolution (300 dpi).

Use when PDF margin-crop is wrong but the PNG export is correct.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image

FIGS = Path(__file__).resolve().parents[1] / "figs"
DEFAULT_STEM = FIGS / "m1_timeseries_bm_1x4"


def png_to_pdf(png: Path, pdf: Path, *, dpi: float = 300.0) -> None:
    img = Image.open(png)
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    img.save(pdf, "PDF", resolution=dpi)


def resolve_pair(stem: Path) -> tuple[Path, Path]:
    if stem.suffix.lower() == ".png":
        return stem, stem.with_suffix(".pdf")
    return stem.with_suffix(".png"), stem.with_suffix(".pdf")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stem", type=Path, nargs="?", default=DEFAULT_STEM)
    ap.add_argument("--dpi", type=float, default=300.0)
    ap.add_argument("--backup", action="store_true", help="keep .pdf.bak before overwrite")
    args = ap.parse_args()
    png, pdf = resolve_pair(args.stem)
    if not png.is_file():
        print(f"missing PNG: {png}", file=sys.stderr)
        return 1
    if pdf.is_file() and args.backup:
        bak = pdf.with_suffix(pdf.suffix + ".bak")
        shutil.copy2(pdf, bak)
        print(f"backup {bak}")
    png_to_pdf(png, pdf, dpi=args.dpi)
    w, h = Image.open(png).size
    print(f"wrote {pdf} from {png} ({w}x{h} px @ {args.dpi} dpi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())