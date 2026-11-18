#!/usr/bin/env python3
"""Trim empty margin below painted content (keeps page top and side boxes).

Typical use after plot_d2_d3_d4_rq13_row.py when the fixed fig height leaves
whitespace under the x-axis labels.

  python3 scripts/crop_fig_bottom_blank.py path/to/fig.pdf path/to/fig.png
  python3 scripts/crop_fig_bottom_blank.py --in-place results/d3/figures/fig_d2_d3_d4_rq13_row.pdf

PDF: Ghostscript HiResBoundingBox → raise MediaBox/CropBox bottom to content y0.
PNG: scan rows from the bottom for non-background pixels (same trim height as PDF when both given).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pypdf import PdfReader, PdfWriter


def gs_hires_bbox(pdf: Path) -> tuple[float, float, float, float]:
    proc = subprocess.run(
        [
            "gs",
            "-o",
            "-",
            "-sDEVICE=bbox",
            "-dNOPAUSE",
            "-dBATCH",
            "-dQUIET",
            str(pdf),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    text = proc.stderr + proc.stdout
    m = re.search(
        r"%%HiResBoundingBox:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
        text,
    )
    if not m:
        raise SystemExit(f"Ghostscript bbox failed for {pdf}\n{text[-2000:]}")
    return tuple(float(x) for x in m.groups())  # type: ignore[return-value]


def crop_pdf_bottom(src: Path, dst: Path, *, pad_pt: float) -> tuple[float, float]:
    _x0, y0, _x1, y1 = gs_hires_bbox(src)
    y_lo = max(0.0, y0 - pad_pt)

    reader = PdfReader(str(src))
    writer = PdfWriter()
    for page in reader.pages:
        left = float(page.mediabox.left)
        right = float(page.mediabox.right)
        top = float(page.mediabox.top)
        page.mediabox.lower_left = (left, y_lo)
        page.mediabox.upper_right = (right, top)
        page.cropbox.lower_left = (left, y_lo)
        page.cropbox.upper_right = (right, top)
        writer.add_page(page)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as stream:
        writer.write(stream)

    old_h = top / 72.0
    new_h = (top - y_lo) / 72.0
    trim_pt = y_lo
    print(
        f"PDF {dst.name}: bottom trim {trim_pt:.1f} pt "
        f"({trim_pt / 72:.3f} in), height {old_h:.2f} → {new_h:.2f} in"
    )
    return top, y_lo


def _content_bottom_row(img: Image.Image, *, bg_thresh: int) -> int:
    rgb = np.asarray(img.convert("RGB"))
    h, w, _ = rgb.shape
    ink = np.any(rgb < bg_thresh, axis=2)
    rows = np.where(ink.any(axis=1))[0]
    if len(rows) == 0:
        return h - 1
    return int(rows[-1])


def crop_png_bottom(
    src: Path,
    dst: Path,
    *,
    pad_px: int,
    bg_thresh: int,
    target_height_px: int | None = None,
) -> None:
    im = Image.open(src)
    w, h = im.size
    bottom = _content_bottom_row(im, bg_thresh=bg_thresh)
    new_h = min(h, bottom + 1 + pad_px)
    if target_height_px is not None:
        new_h = min(new_h, target_height_px)
    out = im.crop((0, 0, w, new_h))
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, dpi=im.info.get("dpi") or (400, 400))
    print(f"PNG {dst.name}: height {h} → {new_h} px (trim {h - new_h} px from bottom)")


def _paired_png(pdf: Path) -> Path:
    return pdf.with_suffix(".png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="one or more PDF and/or PNG paths")
    ap.add_argument("--in-place", action="store_true", help="replace inputs; PDF backup as *.pre_bottom_crop.pdf")
    ap.add_argument("--pad-pt", type=float, default=2.0, help="padding below content (PDF points)")
    ap.add_argument("--pad-px", type=int, default=None, help="PNG padding in pixels (default from --pad-pt @ 400 dpi)")
    ap.add_argument("--bg-thresh", type=int, default=252, help="RGB channel max treated as background on PNG")
    args = ap.parse_args()

    pad_px = args.pad_px if args.pad_px is not None else int(round(args.pad_pt * 400 / 72))

    pdfs = [p for p in args.inputs if p.suffix.lower() == ".pdf"]
    pngs = [p for p in args.inputs if p.suffix.lower() == ".png"]
    if not pdfs and not pngs:
        raise SystemExit("need at least one .pdf or .png")

    pdf_trim: dict[Path, tuple[float, float]] = {}

    for pdf in pdfs:
        if not pdf.is_file():
            raise SystemExit(f"missing {pdf}")
        if args.in_place:
            bak = pdf.with_suffix(pdf.suffix + ".pre_bottom_crop")
            shutil.copy2(pdf, bak)
            src, out = bak, pdf
            print(f"Backup {bak}")
        else:
            src, out = pdf, pdf.with_name(f"{pdf.stem}_bottom_crop.pdf")
        top, y_lo = crop_pdf_bottom(src, out, pad_pt=args.pad_pt)
        pdf_trim[pdf.resolve()] = (top, y_lo)
        pair = _paired_png(pdf)
        if pair.is_file() and pair not in pngs:
            pngs.append(pair)

    for png in pngs:
        if not png.is_file():
            raise SystemExit(f"missing {png}")
        pdf = png.with_suffix(".pdf")
        target_h = None
        if pdf.is_file():
            if pdf.resolve() in pdf_trim:
                top, y_lo = pdf_trim[pdf.resolve()]
            else:
                _x0, y_lo, _x1, top = gs_hires_bbox(pdf)
            # match PDF trim ratio on raster
            with Image.open(png) as probe:
                ph = probe.size[1]
            dpi = 400.0
            target_h = int(round(ph * (top - y_lo) / top))

        if args.in_place:
            bak = png.with_suffix(png.suffix + ".pre_bottom_crop")
            shutil.copy2(png, bak)
            src, out = bak, png
            print(f"Backup {bak}")
        else:
            src, out = png, png.with_name(f"{png.stem}_bottom_crop.png")
        crop_png_bottom(
            src, out, pad_px=pad_px, bg_thresh=args.bg_thresh, target_height_px=target_h,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())