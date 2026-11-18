#!/usr/bin/env python3
# common.py — shared helpers for the io_ext reproduction campaign analysis.
# Read-only over results/campaign/**; writes only under RESULTS/.
import gzip
import io
import json
import math
import os
import re

CAMPAIGN = "/opt/ioext/experiments/reproduction/results/campaign"
RESULTS = "/opt/ioext/experiments/reproduction/RESULTS"

ALGOS = ["mq_deadline", "kyber", "io_max", "io_weight",
         "io_cost", "io_latency", "d2fq", "linnos"]
REPS = [1, 2, 3, 4, 5]
ARMS_ALL = ["none", "native", "iox"]

# fixed palette per arm (consistent across all figures)
ARM_COLOR = {"none": "#7f7f7f", "native": "#1f77b4", "iox": "#d62728"}
ARM_LS = {"none": ":", "native": "-", "iox": "-"}
ARM_LABEL = {"none": "none", "native": "native", "iox": "IOX"}

IO_MAX_WBPS = 33554432          # 32 MiB/s
IOLAT_TARGET_US = 12000         # 12 ms


def run_dir(algo, rep, arm):
    return os.path.join(CAMPAIGN, algo, "rep%d" % rep, arm)


def read_text_maybe_gz(path):
    """Read a possibly-gzipped text file entirely in memory (no disk unpack)."""
    if not os.path.exists(path):
        return None
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as f:
        return f.read()


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_jsonl(path):
    """Parse a JSON-lines loader stats file (plain or gz). Returns list of dicts."""
    txt = read_text_maybe_gz(path)
    if txt is None:
        return None
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # tolerate torn trailing lines
    return out


# ---------------------------------------------------------------------------
# fio JSON helpers
# ---------------------------------------------------------------------------

def fio_p99_ns(j, direction):
    """p99 clat in ns from a fio JSON result dict, for 'read'/'write'; None if absent."""
    if j is None:
        return None
    for jo in j.get("jobs", []):
        d = jo.get(direction, {})
        v = d.get("clat_ns", {}).get("percentile", {}).get("99.000000")
        if v is not None:
            return v
    return None


def fio_pct_ns(j, direction, pct="99.000000"):
    if j is None:
        return None
    for jo in j.get("jobs", []):
        d = jo.get(direction, {})
        v = d.get("clat_ns", {}).get("percentile", {}).get(pct)
        if v is not None:
            return v
    return None


def fio_iops(j, direction):
    if j is None:
        return None
    for jo in j.get("jobs", []):
        d = jo.get(direction, {})
        if d:
            return d.get("iops")
    return None


def fio_bw_kib(j, direction):
    if j is None:
        return None
    for jo in j.get("jobs", []):
        d = jo.get(direction, {})
        if d:
            return d.get("bw")
    return None


def fio_bytes(j, direction):
    if j is None:
        return None
    for jo in j.get("jobs", []):
        d = jo.get(direction, {})
        if d:
            return d.get("io_bytes")
    return None


def fio_runtime_ms(j):
    if j is None:
        return None
    for jo in j.get("jobs", []):
        for d in ("read", "write"):
            if jo.get(d, {}).get("runtime"):
                return jo[d]["runtime"]
    return None


# ---------------------------------------------------------------------------
# fio window logs (1 s bw / iops) and per-IO latency logs
# ---------------------------------------------------------------------------
# fio *_bw.1.log line:  time_ms, value(KiB/s), dir(0=read,1=write), bs, offset
# fio *_iops.1.log line: time_ms, value(iops count per window), dir, bs, offset
# fio *_lat.1.log (write_lat_log, per-IO): time_ms, latency_us, dir, bs, offset

_LOG_RE = re.compile(r"^\s*(\d+)\s*,\s*([-\d.]+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")


def parse_fio_log(path):
    """Parse a fio log (plain or gz) -> list of (time_ms, value, dir, bs, offset).
    Returns None if the file is missing."""
    txt = read_text_maybe_gz(path)
    if txt is None:
        return None
    rows = []
    for line in txt.splitlines():
        m = _LOG_RE.match(line)
        if m:
            t = int(m.group(1))
            v = float(m.group(2))
            d = int(m.group(3))
            bs = int(m.group(4))
            off = int(m.group(5))
            rows.append((t, v, d, bs, off))
    return rows


def find_log(run, prefix, suffix):
    """Find a fio log file '<prefix>_<suffix>.1.log' (or .gz) in a run dir."""
    for ext in (".log", ".log.gz"):
        p = os.path.join(run, prefix + "_" + suffix + ".1" + ext)
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# sentinels
# ---------------------------------------------------------------------------

def sentinel_iops(run, tag):
    """IOPS of the 3 sentinel runs (before/after); list of 3 floats (None if missing)."""
    out = []
    for i in (1, 2, 3):
        j = load_json(os.path.join(run, "sentinel_%s_%d.json" % (tag, i)))
        v = None
        if j:
            for jo in j.get("jobs", []):
                if jo.get("read", {}).get("iops"):
                    v = jo["read"]["iops"]
                    break
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# loader stdout helpers
# ---------------------------------------------------------------------------

def last_stats(recs, pol=None):
    """Last record of a loader JSON-lines list (optionally filtered by 'pol')."""
    if not recs:
        return None
    it = recs
    if pol is not None:
        it = [r for r in recs if r.get("pol") == pol]
    return it[-1] if it else None


def armed_unix(run):
    """Parse the '== armed <unix>' line from state_snapshot(.gz) -> float or None."""
    txt = read_text_maybe_gz(os.path.join(run, "state_snapshot.log.gz"))
    if txt is None:
        txt = read_text_maybe_gz(os.path.join(run, "state_snapshot.log"))
    if not txt:
        return None
    m = re.search(r"^== armed ([\d.]+)", txt, re.M)
    return float(m.group(1)) if m else None


def sanity_report():
    """Basic data-completeness inventory across the whole campaign."""
    lines = []
    for algo in ALGOS:
        for rep in REPS:
            arms = ARMS_ALL if algo not in ("d2fq", "linnos") else ["none", "iox"]
            for arm in arms:
                d = run_dir(algo, rep, arm)
                if not os.path.isdir(d):
                    lines.append("MISSING RUN DIR %s" % d)
                    continue
                st = load_json(os.path.join(d, "RESULT.json"))
                if not st or st.get("status") != "OK":
                    lines.append("RESULT not OK: %s -> %s" % (d, st))
    return lines


if __name__ == "__main__":
    probs = sanity_report()
    print("sanity problems:", len(probs))
    for p in probs[:20]:
        print(" ", p)
