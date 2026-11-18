#!/usr/bin/env python3
"""Crop excess white canvas from figure PNG/PDF (content bbox + padding).

Does not re-render; only trims margins. Intended for M1.2 bandwidth heatmaps
and other fixed-canvas exports from matplotlib.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore


def _content_bbox_rgb(arr: np.ndarray, thresh: int) -> tuple[int, int, int, int]:
    """Return (x0, y0, x1, y1) pixel bbox of non-background pixels (x1/y1 exclusive)."""
    if arr.ndim == 2:
        mask = arr < thresh
    else:
        rgb = arr[..., :3]
        if arr.shape[-1] == 4:
            alpha = arr[..., 3]
            mask = (alpha > 8) & np.any(rgb < thresh, axis=2)
        else:
            mask = np.any(rgb < thresh, axis=2)
    if not mask.any():
        raise ValueError("no non-background pixels found (try lowering --thresh)")
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def crop_png(path: Path, out: Path, *, pad_px: int, thresh: int) -> tuple[int, int, int, int]:
    img = Image.open(path)
    arr = np.asarray(img)
    x0, y0, x1, y1 = _content_bbox_rgb(arr, thresh)
    x0 = max(0, x0 - pad_px)
    y0 = max(0, y0 - pad_px)
    x1 = min(arr.shape[1], x1 + pad_px)
    y1 = min(arr.shape[0], y1 + pad_px)
    cropped = img.crop((x0, y0, x1, y1))
    out.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out)
    return x0, y0, x1, y1


def crop_pdf(path: Path, out: Path, *, pad_pt: float, thresh: int, render_scale: float) -> tuple[float, float, float, float]:
    doc = fitz.open(path)
    if doc.page_count != 1:
        raise ValueError(f"expected single-page PDF, got {doc.page_count} pages: {path}")
    page = doc[0]
    mat = fitz.Matrix(render_scale, render_scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    h, w = pix.height, pix.width
    n = pix.n
    data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, n)
    x0, y0, x1, y1 = _content_bbox_rgb(data, thresh)
    inv = 1.0 / render_scale
    pt_x0 = x0 * inv - pad_pt
    pt_y0 = y0 * inv - pad_pt
    pt_x1 = x1 * inv + pad_pt
    pt_y1 = y1 * inv + pad_pt
    media = page.mediabox
    pt_x0 = max(media.x0, pt_x0)
    pt_y0 = max(media.y0, pt_y0)
    pt_x1 = min(media.x1, pt_x1)
    pt_y1 = min(media.y1, pt_y1)
    crop = fitz.Rect(pt_x0, pt_y0, pt_x1, pt_y1)
    page.set_cropbox(crop)
    page.set_mediabox(crop)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    tmp.replace(out)
    return pt_x0, pt_y0, pt_x1, pt_y1


def resolve_pair(stem: Path) -> tuple[Path, Path]:
    png = stem.with_suffix(".png") if stem.suffix else Path(f"{stem}.png")
    pdf = stem.with_suffix(".pdf") if stem.suffix else Path(f"{stem}.pdf")
    return png, pdf


def main() -> int:
    default_stem = (
        Path(__file__).resolve().parents[1] / "figs" / "m1_2_policy_conflict_bandwidth"
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "stem",
        type=Path,
        nargs="?",
        default=default_stem,
        help="figure basename or path without extension (default: M1.2 bandwidth fig)",
    )
    ap.add_argument("--png", type=Path, default=None, help="override PNG path")
    ap.add_argument("--pdf", type=Path, default=None, help="override PDF path")
    ap.add_argument("--pad-px", type=int, default=4, help="PNG padding after crop (pixels)")
    ap.add_argument("--pad-pt", type=float, default=1.5, help="PDF padding after crop (points)")
    ap.add_argument(
        "--thresh",
        type=int,
        default=250,
        help="per-channel cutoff: pixels darker than this are content (0–255)",
    )
    ap.add_argument("--pdf-scale", type=float, default=3.0, help="raster scale for PDF bbox probe")
    ap.add_argument(
        "--suffix",
        default="",
        help="output suffix before extension (default: '' = overwrite when --in-place)",
    )
    ap.add_argument(
        "--in-place",
        action="store_true",
        help="replace original files (default unless --suffix is set)",
    )
    ap.add_argument("--backup", action="store_true", help="with --in-place, keep .bak copies")
    args = ap.parse_args()

    if args.png or args.pdf:
        png = args.png or resolve_pair(args.stem)[0]
        pdf = args.pdf or resolve_pair(args.stem)[1]
    else:
        png, pdf = resolve_pair(args.stem)

    in_place = args.in_place or (args.suffix == "")
    suffix = args.suffix

    for path in (png, pdf):
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 1

    out_png = png if in_place else png.with_name(f"{png.stem}{suffix}{png.suffix}")
    out_pdf = pdf if in_place else pdf.with_name(f"{pdf.stem}{suffix}{pdf.suffix}")

    if in_place and args.backup:
        for src in (png, pdf):
            bak = src.with_suffix(src.suffix + ".bak")
            shutil.copy2(src, bak)
            print(f"backup {bak}")

    px_box = crop_png(png, out_png, pad_px=args.pad_px, thresh=args.thresh)
    pt_box = crop_pdf(
        pdf, out_pdf, pad_pt=args.pad_pt, thresh=args.thresh, render_scale=args.pdf_scale,
    )

    print(f"PNG {png} -> {out_png}  crop_px={px_box}")
    print(f"PDF {pdf} -> {out_pdf}  crop_pt={pt_box}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())