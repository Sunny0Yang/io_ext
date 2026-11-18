#!/usr/bin/env python3
"""TEMP: trim excess white canvas on m1_timeseries_bm_1x4.{png,pdf}.

Uses the same bbox logic as crop_figure_canvas.py (sibling import). Safe to
delete after figures are checked into paper assets.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from crop_figure_canvas import crop_pdf, crop_png, resolve_pair

FIGS = Path(__file__).resolve().parents[1] / "figs"
DEFAULT_STEM = FIGS / "m1_timeseries_bm_1x4"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "stem",
        type=Path,
        nargs="?",
        default=DEFAULT_STEM,
        help="figure basename (default: m1_timeseries_bm_1x4)",
    )
    ap.add_argument("--pad-px", type=int, default=6, help="PNG margin after trim (px)")
    ap.add_argument("--pad-pt", type=float, default=2.0, help="PDF margin after trim (pt)")
    ap.add_argument(
        "--thresh",
        type=int,
        default=248,
        help="per-channel cutoff; lower keeps more near-white (grid lines)",
    )
    ap.add_argument("--pdf-scale", type=float, default=3.0)
    ap.add_argument("--suffix", default="", help="e.g. _trim (default: overwrite)")
    ap.add_argument("--in-place", action="store_true", help="replace originals")
    ap.add_argument("--backup", action="store_true", help="write .bak before overwrite")
    args = ap.parse_args()

    png, pdf = resolve_pair(args.stem)
    for path in (png, pdf):
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 1

    in_place = args.in_place or args.suffix == ""
    out_png = png if in_place else png.with_name(f"{png.stem}{args.suffix}{png.suffix}")
    out_pdf = pdf if in_place else pdf.with_name(f"{pdf.stem}{args.suffix}{pdf.suffix}")

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