#!/usr/bin/env python3
"""M1 1×4 timeseries + M1.2 device-bandwidth heatmap — one text-width row (1×2).

Paper geometry (M1_2_FIGURE_DESIGN): 174 mm full text width = 104.4 mm timeseries
+ 68 mm bandwidth at 44 mm row height; panels scaled to fit slots (no cross-panel
height stretch). PNG @ 300 dpi; PDF is the same raster layout (PNG → PDF), not
a separate vector merge (which stretched/cropped panel PDFs differently).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[3]
FIGS = REPO / "experiments/m1m2_bm/figs"
STEM = "m1_timeseries_m12_bw_row"
LEFT_STEM = "m1_timeseries_bm_1x4"
RIGHT_STEM = "m1_2_policy_conflict_bandwidth"

# Full text width (experiments/m1m2_bm: TEXT_W = 174 mm).
PAGE_W_MM = 174.0
LEFT_W_MM = 104.4
RIGHT_W_MM = 68.0
ROW_H_MM = 44.0
GAP_MM = PAGE_W_MM - LEFT_W_MM - RIGHT_W_MM
SAVE_DPI = 300


def _mm_px(mm: float, dpi: int = SAVE_DPI) -> int:
    return int(round(mm / 25.4 * dpi))


def fit_contain(
    img: Image.Image,
    box_w: int,
    box_h: int,
    *,
    bg: str = "white",
    valign: str = "center",
) -> Image.Image:
    """Scale image to fit inside box_w×box_h; valign bottom aligns plot baselines."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    iw, ih = img.size
    scale = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    out = Image.new("RGB", (box_w, box_h), bg)
    x0 = (box_w - nw) // 2
    if valign == "bottom":
        y0 = box_h - nh
    elif valign == "top":
        y0 = 0
    else:
        y0 = (box_h - nh) // 2
    out.paste(resized, (x0, y0))
    return out


def stitch_png(
    left_png: Path,
    right_png: Path,
    out_png: Path,
) -> dict:
    box_l_w, box_r_w = _mm_px(LEFT_W_MM), _mm_px(RIGHT_W_MM)
    box_h = _mm_px(ROW_H_MM)
    gap_px = _mm_px(GAP_MM)
    canvas_w = _mm_px(PAGE_W_MM)

    left_box = fit_contain(Image.open(left_png), box_l_w, box_h, valign="bottom")
    right_box = fit_contain(Image.open(right_png), box_r_w, box_h, valign="bottom")
    canvas = Image.new("RGB", (canvas_w, box_h), "white")
    canvas.paste(left_box, (0, 0))
    canvas.paste(right_box, (box_l_w + gap_px, 0))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, dpi=(SAVE_DPI, SAVE_DPI))

    li = Image.open(left_png)
    ri = Image.open(right_png)
    return {
        "canvas_px": [canvas_w, box_h],
        "slots_px": {
            LEFT_STEM: [box_l_w, box_h],
            RIGHT_STEM: [box_r_w, box_h],
        },
        "gap_px": gap_px,
        "native_px": {LEFT_STEM: list(li.size), RIGHT_STEM: list(ri.size)},
    }


def png_to_pdf(png: Path, pdf: Path, *, dpi: float = SAVE_DPI) -> None:
    """One-page PDF matching the PNG pixels (same approach as figure_png_to_pdf.py)."""
    img = Image.open(png)
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    img.save(pdf, "PDF", resolution=dpi)


def stitch(fig_dir: Path) -> dict:
    left_png = fig_dir / f"{LEFT_STEM}.png"
    right_png = fig_dir / f"{RIGHT_STEM}.png"
    for p in (left_png, right_png):
        if not p.is_file():
            raise SystemExit(f"missing panel PNG {p}")

    out_png = fig_dir / f"{STEM}.png"
    out_pdf = fig_dir / f"{STEM}.pdf"
    png_info = stitch_png(left_png, right_png, out_png)
    png_to_pdf(out_png, out_pdf, dpi=SAVE_DPI)

    gen_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    meta = {
        "figure": STEM,
        "generated_at": gen_at,
        "layout": "1x2",
        "panels": [LEFT_STEM, RIGHT_STEM],
        "page_width_mm": PAGE_W_MM,
        "left_width_mm": LEFT_W_MM,
        "right_width_mm": RIGHT_W_MM,
        "row_height_mm": ROW_H_MM,
        "gap_mm": round(GAP_MM, 3),
        "save_dpi": SAVE_DPI,
        "fit": "contain_per_slot",
        "pdf_from": "stitched_png",
        "inputs": {
            LEFT_STEM: {"png": str(left_png.resolve())},
            RIGHT_STEM: {"png": str(right_png.resolve())},
        },
        "canvas_mm": [PAGE_W_MM, ROW_H_MM],
        **png_info,
    }
    meta_path = fig_dir / f"{STEM}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fig-dir", type=Path, default=FIGS)
    args = ap.parse_args()
    meta = stitch(args.fig_dir)
    print(f"Wrote {args.fig_dir / STEM}.png ({meta['generated_at']})")
    print(f"Canvas: {meta['canvas_mm'][0]} × {meta['canvas_mm'][1]} mm  gap={meta['gap_mm']} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())