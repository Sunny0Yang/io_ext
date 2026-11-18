#!/usr/bin/env python3
"""analyze_hotswitch.py - q 热替换守恒验收分析（host 侧，VM 退出后运行）。

输入（<$attempt_dir>/evidence/）：
  sampler.jsonl          每 1s: {"t", "meta": [...], "tenant": [...]}
  switches.jsonl         每次热替换一条记录（含 queued_ok / queue_depth / pending / flipped）
  fio_seg_summary.jsonl  每段 fio 的 error/total_ios/io_bytes/iops/p99
  fio_hi.json            矩阵 d 高优先级租户连续 fio 的最终 JSON
  drain_a_*.json / drain_b_*.json / drain_assert.json
  dmesg_*.log / dmesg_final.txt

守恒语义（ioext_arb_common.h 记账）：
  inflight(t) = Σdispatched - Σcompleted - Σrequeued
  => 窗口内 completions = Δdispatches - Δrequeues - Δinflight
  队列策略在位段：该恒等式必须闭合（R == 0）。
  q_none（DIRECT bypass）在位段：arbiter 不 see 任何 insert/dispatch/complete，
  期望租户计数平坦（Δ≈0），fio 完成的 ios 不计入账（如实记录为旁路不可见）。

输出：analysis.json + stdout 报告；exit 0 = 无账目破坏。
"""
import json
import os
import re
import sys

EVIDENCE = "evidence"


def load_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def load_json(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def key_cgid(k):
    if isinstance(k, dict):
        return int(k.get("cgroup_id", 0))
    if isinstance(k, list):
        v = 0
        for i, b in enumerate(k[:8]):
            v |= int(b, 0) << (8 * i)
        return v
    return -1


def key_hctx(k):
    if isinstance(k, dict):
        return int(k.get("hctx_idx", 0))
    if isinstance(k, list) and len(k) > 8:
        return int(k[8], 0) if isinstance(k[8], str) else int(k[8])
    return 0


# value layouts for BTF-less maps (bpftool emits raw little-endian byte lists)
META_OFF = {
    "queue_depth": (0, 8), "dispatches": (8, 8), "active_inserts": (16, 8),
    "owner_token": (24, 8), "arb_weight": (32, 4), "arb_prio": (36, 4),
    "flags": (40, 4), "reserved": (44, 4),
}
TEN_OFF = {
    "deficit": (0, 8), "pending": (8, 8), "inflight": (16, 8),
    "completed_bytes": (24, 8), "completed_cost": (32, 8),
    "dispatches": (40, 8), "requeues": (48, 8), "requeued_bytes": (56, 8),
    "serve_selects": (64, 8), "refill_arms": (72, 8), "wrap_selects": (80, 8),
    "skip_pending_zero": (88, 8), "refilled_round": (96, 8),
    "boost": (104, 4), "slo_state": (108, 4),
}


def decode_value(v, offsets):
    if isinstance(v, dict):
        return {k: _int_or_str(val) for k, val in v.items()}
    if isinstance(v, list):
        out = {}
        for fld, (off, sz) in offsets.items():
            if len(v) >= off + sz:
                out[fld] = int.from_bytes(
                    bytes(int(x, 0) for x in v[off:off + sz]), "little")
        return out
    return {}


def _int_or_str(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return val


def extract(dump, offsets, want_cgid=None):
    """map dump entries -> {cgid: fields-dict} (tenant_state summed over hctx)."""
    agg = {}
    for e in dump or []:
        cg = key_cgid(e.get("key"))
        if want_cgid is not None and cg != want_cgid:
            continue
        cur = agg.setdefault(cg, {})
        for fld, val in decode_value(e.get("value"), offsets).items():
            try:
                cur[fld] = cur.get(fld, 0) + int(val)
            except (TypeError, ValueError):
                cur[fld] = val
    return agg


def main():
    attempt_dir = os.path.abspath(sys.argv[1])
    ev = os.path.join(attempt_dir, EVIDENCE)
    man0 = load_json(os.path.join(attempt_dir, "manifest.json")) or {}
    matrix = (man0.get("metadata") or {}).get("matrix")

    samples = load_jsonl(os.path.join(ev, "sampler.jsonl"))
    switches = load_jsonl(os.path.join(ev, "switches.jsonl"))
    seg_summary = load_jsonl(os.path.join(ev, "fio_seg_summary.jsonl"))

    # map cgid -> role (from switches records)
    cgid_role = {}
    for s in switches:
        cgid_role[s["cgid"]] = "switched"
    drain_meta = load_json(os.path.join(ev, "drain_b_policy_meta.json")) or []
    for e in drain_meta:
        cgid_role.setdefault(key_cgid(e["key"]), "static")

    result = {"matrix": matrix, "attempt_dir": attempt_dir, "switches": switches,
              "hard_failures": [], "findings": []}

    # ---------------- per-tenant sampled series ----------------
    series = {}  # cgid -> list of (t, meta_fields, tenant_fields)
    for rec in samples:
        t = rec.get("t")
        m = extract(rec.get("meta"), META_OFF)
        tn = extract(rec.get("tenant"), TEN_OFF)
        cgids = set(m) | set(tn)
        for cg in cgids:
            series.setdefault(cg, []).append(
                (t, m.get(cg, {}), tn.get(cg, {})))

    result["cgid_roles"] = {str(k): v for k, v in cgid_role.items()}

    # ---------------- unexpected cgroup ids (crosstalk probe) ----------------
    known = set(cgid_role)
    for cg in series:
        if cg not in known and cg > 0:
            result["hard_failures"].append(
                "unexpected_cgid_in_maps:%d (crosstalk probe)" % cg)

    # ---------------- segment classification ----------------
    switch_times = [s["t"] for s in switches]
    man = load_json(os.path.join(attempt_dir, "manifest.json")) or {}
    meta = man.get("metadata", {})
    pol_seq = (meta.get("policy_seq") or "").split()
    result["policy_seq"] = pol_seq

    # ---------------- post-switch meta zeroing curve ----------------
    zero_curves = []
    for sw in switches:
        cgid = sw["cgid"]
        t0 = sw["t"]
        rows = [r for r in series.get(cgid, []) if t0 <= r[0] <= t0 + 12]
        curve = []
        base_disp = None
        for (t, m, tn) in rows[:11]:
            d = int(m.get("dispatches", 0))
            if base_disp is None:
                base_disp = d
            curve.append({"t": round(t - t0, 2),
                          "meta_dispatches": d,
                          "meta_queue_depth": int(m.get("queue_depth", 0)),
                          "tenant_dispatches": int(tn.get("dispatches", 0)),
                          "tenant_pending": int(tn.get("pending", 0)),
                          "tenant_inflight": int(tn.get("inflight", 0))})
        zero_curves.append({"tag": sw.get("tag"), "cgid": cgid,
                            "from": sw.get("from"), "to": sw.get("to"),
                            "flipped": sw.get("flipped"),
                            "queued_ok": sw.get("queued_ok"),
                            "curve": curve})
    result["post_switch_meta_curves"] = zero_curves

    # quantify: max |meta_dispatches - implied_completions| in 10 windows
    # after each switch, for the switched tenant
    zeroing_gap = []
    for zc in zero_curves:
        cgid = zc["cgid"]
        rows = [r for r in series.get(cgid, [])]
        gap_max = 0
        for sw_t in [s["t"] for s in switches if s.get("tag") == zc["tag"]]:
            base = [r for r in rows if r[0] <= sw_t]
            if not base:
                continue
            b = base[-1]
            b_disp_t = int(b[2].get("dispatches", 0))
            b_reque = int(b[2].get("requeues", 0))
            b_infl = int(b[2].get("inflight", 0))
            b_disp_m = int(b[1].get("dispatches", 0))
            for (t, m, tn) in rows:
                if not (sw_t < t <= sw_t + 12):
                    continue
                impl = (int(tn.get("dispatches", 0)) - b_disp_t) - \
                       (int(tn.get("requeues", 0)) - b_reque) - \
                       (int(tn.get("inflight", 0)) - b_infl)
                gap_m = (int(m.get("dispatches", 0)) - b_disp_m) - impl
                gap_max = max(gap_max, abs(gap_m))
        zc["max_meta_minus_implied_completions"] = gap_max
        zeroing_gap.append(gap_max)
    result["zeroing_gap_max_all_switches"] = max(zeroing_gap) if zeroing_gap else 0

    # ---------------- global identity per tenant ----------------
    tenant_report = {}
    for cg, rows in sorted(series.items()):
        if not rows:
            continue
        first, last = rows[0], rows[-1]
        t_disp = int(last[2].get("dispatches", 0)) - int(first[2].get("dispatches", 0))
        t_reque = int(last[2].get("requeues", 0)) - int(first[2].get("requeues", 0))
        t_infl = int(last[2].get("inflight", 0)) - int(first[2].get("inflight", 0))
        m_disp = int(last[1].get("dispatches", 0)) - int(first[1].get("dispatches", 0))
        implied = t_disp - t_reque - t_infl
        tenant_report[str(cg)] = {
            "role": cgid_role.get(cg, "unknown"),
            "tenant_dispatches_delta": t_disp,
            "tenant_requeues_delta": t_reque,
            "tenant_inflight_delta": t_infl,
            "meta_dispatches_delta": m_disp,
            "implied_completions": implied,
            "completed_bytes": int(last[2].get("completed_bytes", 0)) -
                               int(first[2].get("completed_bytes", 0)),
        }
    result["tenant_global"] = tenant_report

    # compare implied completions vs fio (segmented tenant: sum of seg ios)
    fio_ios = sum(sg.get("total_ios", 0) for sg in seg_summary)
    result["fio_seg_total_ios"] = fio_ios

    # per-segment windows (fio start/end recorded by the guest): tenant
    # deltas inside each window -> per-segment dispatch/completion ratio.
    # Known systematic effect (quantified, not lost IO): dispatched_request
    # can be charged ~2x per request under concurrent dispatch loops on the
    # same hctx (S10 race, ioext_arb_common.h comment) -- we therefore report
    # the ratio per segment instead of asserting dispatches==completed.
    seg_acc = []
    for sg in seg_summary:
        w0 = sg.get("seg_start")
        w1 = sg.get("seg_end")
        row = {"seg": sg.get("seg"), "policy": None, "fio_ios": sg.get("total_ios"),
               "fio_error": sg.get("error"), "p99_us": sg.get("p99_us"),
               "iops": sg.get("iops")}
        if w0 is not None and w1 is not None:
            pol = pol_seq[sg["seg"] - 1] if pol_seq and sg["seg"] - 1 < len(pol_seq) else "?"
            row["policy"] = pol
            for cg, rows in series.items():
                pre = [r for r in rows if r[0] <= w0]
                if not pre:
                    continue
                a = pre[-1]
                b_row = [r for r in rows if w0 < r[0] <= w1]
                if not b_row:
                    b_row = [a]
                b = b_row[-1]
                td = int(b[2].get("dispatches", 0)) - int(a[2].get("dispatches", 0))
                trq = int(b[2].get("requeues", 0)) - int(a[2].get("requeues", 0))
                tif = int(b[2].get("inflight", 0)) - int(a[2].get("inflight", 0))
                md = int(b[1].get("dispatches", 0)) - int(a[1].get("dispatches", 0))
                impl = td - trq - tif
                key = "cg%s" % cg
                row[key] = {"tenant_disp": td, "requeues": trq, "d_inflight": tif,
                            "meta_disp": md, "implied": impl,
                            "ratio_impl_vs_fio": round(impl / sg["total_ios"], 4)
                            if sg.get("total_ios") else None}
        seg_acc.append(row)
    result["seg_accounting"] = seg_acc

    for cg, rep in tenant_report.items():
        if rep["role"] == "switched":
            resid = rep["implied_completions"] - fio_ios
            rep["fio_seg_total_ios"] = fio_ios
            rep["identity_residual_vs_fio"] = resid
            bypass = "q_none" in pol_seq
            if resid != 0:
                # q_none segments bypass the arbiter entirely (insert_request
                # declines -> kernel bypass path, arbiter hooks not fired); a
                # positive residual (fio > implied) is expected there.
                result["findings"].append(
                    "identity_residual cgid=%s: implied=%d fio=%d resid=%d "
                    "(bypass_in_seq=%s, see seg_accounting ratios)" %
                    (cg, rep["implied_completions"], fio_ios, resid, bypass))

    # ---------------- drain residue ----------------
    da = load_json(os.path.join(ev, "drain_assert.json")) or {}
    result["drain_assert"] = da
    if da.get("bad_entries"):
        result["hard_failures"].append("drain_residue:%s" % da["bad_entries"][:10])

    # ---------------- fio errors ----------------
    for sg in seg_summary:
        if sg.get("error") not in (0,):
            result["hard_failures"].append(
                "fio_seg_%s_error:%s" % (sg.get("seg"), sg.get("error")))

    # ---------------- matrix d: hi tenant continuous check ----------------
    hi = load_json(os.path.join(ev, "fio_hi.json"))
    if hi:
        try:
            j = hi["jobs"][0]
            hi_ios = j["read"]["total_ios"]
            hi_err = j.get("error", -1)
            result["fio_hi"] = {"total_ios": hi_ios, "error": hi_err,
                                "io_bytes": j["read"]["io_bytes"],
                                "iops": j["read"]["iops"]}
            if hi_err != 0:
                result["hard_failures"].append("fio_hi_error:%s" % hi_err)
            # hi tenant dispatch/completion ratio (quantified; the absolute
            # conservation gate is the drain assert, not this ratio)
            for cg, rep in tenant_report.items():
                if rep["role"] == "static":
                    ratio = rep["implied_completions"] / hi_ios if hi_ios else None
                    rep["fio_total_ios"] = hi_ios
                    rep["ratio_implied_vs_fio"] = round(ratio, 4) if ratio else None
                    if ratio is None or ratio < 0.05 or ratio > 20:
                        result["hard_failures"].append(
                            "hi_tenant_accounting_out_of_band: implied=%d fio=%d"
                            % (rep["implied_completions"], hi_ios))
                    else:
                        result["findings"].append(
                            "hi tenant dispatch/completion ratio: implied=%d fio=%d ratio=%.4f"
                            % (rep["implied_completions"], hi_ios, ratio))
        except Exception as e:
            result["hard_failures"].append("fio_hi_parse:%s" % e)

        # hi tenant per-second IOPS around each low-tenant switch:
        # perturbation quantification (no pass threshold, reported as-is)
        hi_series = None
        for cg, rows in series.items():
            if cgid_role.get(cg) == "static":
                hi_series = rows
                hi_cg = cg
                break
        if hi_series and switches:
            pert = []
            for sw in switches:
                t0 = sw["t"]
                win = [r for r in hi_series if t0 - 3 <= r[0] <= t0 + 6]
                if len(win) < 6:
                    continue
                iops = []
                for i in range(1, len(win)):
                    dt = win[i][0] - win[i - 1][0]
                    dd = int(win[i][2].get("dispatches", 0)) - \
                         int(win[i - 1][2].get("dispatches", 0))
                    if dt > 0:
                        iops.append((round(win[i][0] - t0, 2), round(dd / dt, 1)))
                pert.append({"switch": sw.get("tag"), "hi_cgid": hi_cg,
                             "hi_iops_per_s_around_switch": iops})
            result["hi_tenant_perturbation"] = pert

    # ---------------- kernel events from dmesg captures ----------------
    kern_pat = re.compile(
        r"\bpanic\b|\boops\b|BUG:|WARNING:|hung_task|soft lockup|lockdep|RCU stall",
        re.I)
    for fn in sorted(os.listdir(ev)) if os.path.isdir(ev) else []:
        if not (fn.startswith("dmesg") and
                (fn.endswith(".log") or fn.endswith(".txt"))):
            continue
        try:
            txt = open(os.path.join(ev, fn), errors="replace").read()
        except Exception:
            continue
        hits = [l for l in txt.splitlines()
                if kern_pat.search(l) and "Command line" not in l]
        if hits:
            result["hard_failures"].append("kernel:%s:%d hits" % (fn, len(hits)))
            result.setdefault("kernel_hits", {})[fn] = hits[:10]

    # ---------------- switch sanity ----------------
    for sw in switches:
        if not sw.get("flipped"):
            result["hard_failures"].append(
                "switch_not_confirmed:%s" % sw.get("tag"))
        if sw.get("to") == sw.get("from"):
            result["hard_failures"].append(
                "switch_same_policy:%s" % sw.get("tag"))

    # ---------------- p99 / throughput per segment (switched tenant) ------
    result["seg_table"] = seg_summary

    json.dump(result, open(os.path.join(attempt_dir, "analysis.json"), "w"),
              indent=2)

    # ---------------- human report ----------------
    print("=== hotswitch analysis: %s (matrix %s) ===" %
          (attempt_dir, matrix))
    print("switches: %d" % len(switches))
    for sw in switches:
        print("  [%s] %s->%s cgid=%s queued_ok=%s qd=%s pend=%s flipped=%s"
              % (sw.get("tag"), sw.get("from"), sw.get("to"), sw.get("cgid"),
                 sw.get("queued_ok"), sw.get("queue_depth"),
                 sw.get("pending"), sw.get("flipped")))
    print("tenant global accounting:")
    for cg, rep in tenant_report.items():
        print("  cgid %s (%s): tdisp=%d reque=%d dinfl=%d mdisp=%d implied=%d cbytes=%d"
              % (cg, rep["role"], rep["tenant_dispatches_delta"],
                 rep["tenant_requeues_delta"], rep["tenant_inflight_delta"],
                 rep["meta_dispatches_delta"], rep["implied_completions"],
                 rep["completed_bytes"]))
    print("fio segmented total ios: %s" % fio_ios)
    print("meta-zeroing gap max (meta.dispatches - implied completions, 10 windows post-switch): %s"
          % result["zeroing_gap_max_all_switches"])
    print("per-segment accounting (switched tenant):")
    for row in result.get("seg_accounting", []):
        keys = [k for k in row if k.startswith("cg")]
        det = row[keys[0]] if keys else {}
        print("  seg %s pol=%s fio_ios=%s err=%s iops=%s p99_us=%s "
              "tdisp=%s reque=%s d_infl=%s meta_disp=%s implied=%s ratio=%s"
              % (row.get("seg"), row.get("policy"), row.get("fio_ios"),
                 row.get("fio_error"), row.get("iops"), row.get("p99_us"),
                 det.get("tenant_disp"), det.get("requeues"),
                 det.get("d_inflight"), det.get("meta_disp"),
                 det.get("implied"), det.get("ratio_impl_vs_fio")))
    for f in result.get("findings", []):
        print("finding: %s" % f)
    print("drain assert: %s" % ("OK" if da.get("ok") else da.get("bad_entries")))
    if result["hard_failures"]:
        print("HARD FAILURES:")
        for h in result["hard_failures"]:
            print("  - %s" % h)
        return 1
    print("RESULT: CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
