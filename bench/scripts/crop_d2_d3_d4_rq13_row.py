#!/usr/bin/env python3
"""Tight-crop fig_d2_d3_d4_rq13_row.pdf after plot_d2_d3_d4_rq13_row.py.

Does not change the plot layout — only trims PDF media/crop box via Ghostscript bbox.

  python3 scripts/crop_d2_d3_d4_rq13_row.py
  python3 scripts/crop_d2_d3_d4_rq13_row.py --pad-pt 1.5
  python3 scripts/crop_d2_d3_d4_rq13_row.py /path/to/fig_d2_d3_d4_rq13_row.pdf --in-place
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CROP = ROOT / "scripts" / "tmp_crop_fig_pdf.py"
DEFAULT_STEM = "fig_d2_d3_d4_rq13_row"
FIG_DIRS = (
    ROOT / "experiments/d3_kvmhost/figures",
    ROOT / "results/d3/figures",
)


def _canonical_pdf(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"missing PDF {explicit}")
        return explicit.resolve()
    for base in FIG_DIRS:
        path = base / f"{DEFAULT_STEM}.pdf"
        if path.is_file():
            return path.resolve()
    raise SystemExit(
        f"missing {DEFAULT_STEM}.pdf under {[str(d) for d in FIG_DIRS]}"
    )


def _unique_targets(canonical: Path) -> list[Path]:
    """All figure-dir PDFs that resolve to the same file (symlinks)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for base in FIG_DIRS:
        path = (base / f"{DEFAULT_STEM}.pdf").resolve()
        if path in seen or not path.is_file():
            continue
        try:
            if os.path.samefile(path, canonical):
                seen.add(path)
                out.append(path)
        except OSError:
            if path == canonical and path not in seen:
                seen.add(path)
                out.append(path)
    if canonical not in seen:
        out.append(canonical)
    return out


def crop_one(pdf: Path, *, pad_pt: float, in_place: bool) -> None:
    if not _CROP.is_file():
        raise SystemExit(f"missing helper {_CROP}")
    cmd = [sys.executable, str(_CROP)]
    if in_place:
        cmd.append("--in-place")
    cmd.extend([f"--pad-pt={pad_pt}", str(pdf)])
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", type=Path, default=None, help="PDF (default: search FIG_DIRS)")
    ap.add_argument(
        "--in-place",
        action="store_true",
        help="replace PDF(s); backup to .pre_crop.pdf (default)",
    )
    ap.add_argument("--no-in-place", action="store_true", help="write <stem>_cropped.pdf beside input")
    ap.add_argument("--pad-pt", type=float, default=1.5, help="padding in pt around content bbox")
    args = ap.parse_args()
    in_place = args.in_place or not args.no_in_place

    canonical = _canonical_pdf(args.pdf)
    targets = _unique_targets(canonical) if in_place else [canonical]

    for path in targets:
        crop_one(path, pad_pt=args.pad_pt, in_place=in_place)

    print(f"cropped {len(targets)} path(s); canonical={canonical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())