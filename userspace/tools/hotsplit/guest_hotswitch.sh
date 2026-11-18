#!/bin/bash
# tools/hotsplit/guest_hotswitch.sh - runs INSIDE the virtme-ng VM (root).
#
# q 热替换守恒验收（arb 常驻、只切 q）：
#   - loader_arb_wdrr 常驻（strict 模式），设备切 scx_io
#   - 租户 fio 分段连续负载（每段 HS_SEG_LEN 秒，段间空隙 <1s），
#     热替换在段中（fio 运行中）触发：先起新 loader（同 cgroup attach
#     触发内核 replace：旧 policy QUIESCING -> RCU 发布 -> 异步 evacuate），
#     确认 policy_meta owner_token 翻转后再 SIGTERM 旧 loader。
#   - 采样器每 1s dump policy_meta / tenant_state 到 sampler.jsonl
#   - 每次替换前后 snapshot 全部相关 map + dmesg -c 增量
#   - 结束后 drain 5s -> dump A -> 再 5s -> dump B，断言
#     pending==inflight==0 && queue_depth==0 && active_inserts==0
#
# Evidence lands under $ATTEMPT_DIR/evidence (host shared dir, rw).
# Exit 0 = all conservation assertions passed; nonzero = first failure class.
set -uo pipefail

BASE_DIR="${HS_BASE_DIR:?}"                        # ioext
ATTEMPT_DIR="${HS_ATTEMPT_DIR:?}"                  # ledger attempt dir (rw)
DEVICE="${HS_DEVICE:-vda}"
MATRIX="${HS_MATRIX:?}"                            # a|b|c|d
SEG_LEN="${HS_SEG_LEN:-12}"
T_SWITCH="${HS_T_SWITCH:-6}"                       # switch trigger point inside a segment
BASE_SEED="${HS_BASE_SEED:-0}"
IOPS_CAP="${HS_IOPS_CAP:-6000}"                    # qemu drive throttling iops-total (host side)
POLICY_SEQ="${HS_POLICY_SEQ:?}"                    # space separated, e.g. "q_fifo q_none q_fifo ..."
HS_QD="${HS_QD:-8}"                                # single-tenant matrix fio iodepth (a/b/c use deep QD to park requests in the scheduler queue)
CAP="${HS_CAP:?}"                                  # iox_safety_capture.py (heartbeat)

LOADER_DIR="$BASE_DIR/userspace/io/build"
BT="$BASE_DIR/kernel/linux-7.3.1/tools/bpf/bpftool/bpftool"
EV="$ATTEMPT_DIR/evidence"
CG_ROOT="/sys/fs/cgroup"
mkdir -p "$EV"

PASS=0; FAIL=0
log()  { printf '[hotsplit] %s\n' "$*" >&2; }
pass() { log "PASS: $1"; PASS=$((PASS+1)); }
fail() { log "FAIL: $1: $2"; FAIL=$((FAIL+1)); }

dmesg -C 2>/dev/null || true

python3 "$CAP" heartbeat --heartbeat-path "$EV/heartbeat.jsonl" --interval-sec 1 &
HB_PID=$!

cleanup() {
    kill "$HB_PID" 2>/dev/null || true
    pkill -TERM -f 'loader_arb_wdrr|loader_q_' 2>/dev/null || true
    sleep 2
    pkill -9 -f 'loader_arb_wdrr|loader_q_' 2>/dev/null || true
    rm -rf /sys/fs/bpf/ioext_policy_meta /sys/fs/bpf/ioext_active_cgroups 2>/dev/null || true
    echo none > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null || true
}
trap cleanup EXIT

log "kernel=$(uname -r) device=$DEVICE matrix=$MATRIX seq=[$POLICY_SEQ] seg_len=$SEG_LEN seed=$BASE_SEED"
grep -q scx_io /proc/kallsyms || { log "no scx_io in guest kernel"; exit 3; }
mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf || { log "cannot mount bpffs"; exit 3; }

echo '+io' > "$CG_ROOT/cgroup.subtree_control" 2>/dev/null || true

# ------------------------------------------------------------- cgroups
if [ "$MATRIX" = "d" ]; then
    mkdir -p "$CG_ROOT/cg_lo" "$CG_ROOT/cg_hi"
    CG_LO="$CG_ROOT/cg_lo"; CG_HI="$CG_ROOT/cg_hi"
    CG_LO_ID=$(stat -c %i "$CG_LO"); CG_HI_ID=$(stat -c %i "$CG_HI")
    CGS="$CG_LO $CG_HI"
    log "dual-tenant: cg_lo(id=$CG_LO_ID) 4K QD1 gets switches; cg_hi(id=$CG_HI_ID) 64K QD8 untouched"
else
    mkdir -p "$CG_ROOT/cg_single"
    CG_S="$CG_ROOT/cg_single"
    CG_S_ID=$(stat -c %i "$CG_S")
    CGS="$CG_S"
    log "single-tenant: cg_single(id=$CG_S_ID)"
fi

dump_map() { # name outfile
    "$BT" -j map dump name "$1" > "$2" 2>"$2.err" || true
}

# policy_meta is created nameless by the loader and pinned at
# /sys/fs/bpf/ioext_policy_meta -> dump by pin path, not by name.
dump_meta_pin() { # outfile
    "$BT" -j map dump pinned /sys/fs/bpf/ioext_policy_meta > "$1" 2>"$1.err" || true
}

# full snapshot of all relevant maps (tolerates unsupported map types)
snapshot_all() { # prefix
    local p="$1"
    for m in tenant_state hctx_state state_map lane_map dispatch_map hctx_map io_queue; do
        dump_map "$m" "$EV/${p}_${m}.json"
    done
    dump_meta_pin "$EV/${p}_policy_meta.json"
}

# ------------------------------------------------------------- arb attach
log "--- attach resident arb_wdrr ---"
"$LOADER_DIR/loader_arb_wdrr" > "$EV/arb_wdrr_loader.log" 2>&1 &
ARB=$!
ARB_OK=0
for i in $(seq 1 20); do
    kill -0 "$ARB" 2>/dev/null || break
    if dmesg 2>/dev/null | grep -qE "arbiter '(wdrr_arbiter|arb_wdrr)'"; then ARB_OK=1; break; fi
    sleep 1
done
if [ "$ARB_OK" = "1" ]; then
    pass "arb_wdrr registered (dmesg)"
else
    fail "arb_wdrr attach" "no registration line in dmesg"
    dmesg > "$EV/dmesg_attach_fail.txt" 2>/dev/null || true
    exit 1
fi

if ! echo scx_io > "/sys/block/$DEVICE/queue/scheduler" 2>/dev/null; then
    fail "scx_io switch" "failed"
    exit 1
fi
ACTIVE_SCHED=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "/sys/block/$DEVICE/queue/scheduler")
[ "$ACTIVE_SCHED" = "scx_io" ] || { fail "scx_io switch" "active=$ACTIVE_SCHED"; exit 1; }
echo 1023 > "/sys/block/$DEVICE/queue/nr_requests" 2>/dev/null || true
dmesg > "$EV/dmesg_after_arb.txt" 2>/dev/null || true
pass "scx_io active, nr_requests=$(cat /sys/block/$DEVICE/queue/nr_requests 2>/dev/null)"

# ------------------------------------------------------------- sampler
python3 - "$EV/sampler.jsonl" "$BT" <<'PYEOF' &
import json, subprocess, sys, time
out_path, bt = sys.argv[1], sys.argv[2]
def dump(name):
    try:
        r = subprocess.run([bt, "-j", "map", "dump", "name", name],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout)
    except Exception:
        return []
def dump_pin(path):
    try:
        r = subprocess.run([bt, "-j", "map", "dump", "pinned", path],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout)
    except Exception:
        return []
while True:
    t = time.time()
    rec = {"t": t, "meta": dump_pin("/sys/fs/bpf/ioext_policy_meta"),
           "tenant": dump("tenant_state")}
    try:
        with open(out_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    time.sleep(1.0)
PYEOF
SAMPLER=$!

# ------------------------------------------------------------- policy mgmt
declare -A LOADER_PID      # cgroup -> current loader pid
declare -A LOADER_POL      # cgroup -> current policy name
declare -A CG_OF_ID

start_policy() { # policy cgroup prio weight -> pid in RET_PID
    "$LOADER_DIR/loader_$1" --cgroup "$2" --arb-prio "$3" --arb-weight "$4" \
        > "$EV/loader_$1_$$_$RANDOM.log" 2>&1 &
    RET_PID=$!
}

map_field() { # selector... cgid field -> summed value over matching entries
    # $1 = "pinned" (policy_meta via /sys/fs/bpf/ioext_policy_meta) or map name.
    # policy_meta is created by the loader WITHOUT BTF and nameless -> bpftool
    # emits raw little-endian byte lists; tenant_state may emit dicts.
    local sel="$1" cgid="$2" field="$3" arg spec
    if [ "$sel" = "pinned" ]; then
        spec="-j map dump pinned /sys/fs/bpf/ioext_policy_meta"
        sel="policy_meta"
    else
        spec="-j map dump name $1"
    fi
    "$BT" $spec 2>/dev/null | python3 -c "
import json,sys
map_name, cgid, field = sys.argv[1], int(sys.argv[2]), sys.argv[3]
OFF = {
 'policy_meta': {'queue_depth':(0,8),'dispatches':(8,8),'active_inserts':(16,8),
                 'owner_token':(24,8),'arb_weight':(32,4),'arb_prio':(36,4),
                 'flags':(40,4)},
 'tenant_state': {'deficit':(0,8),'pending':(8,8),'inflight':(16,8),
                  'completed_bytes':(24,8),'completed_cost':(32,8),
                  'dispatches':(40,8),'requeues':(48,8),'requeued_bytes':(56,8)},
}
try: entries=json.load(sys.stdin)
except Exception: print(-1); raise SystemExit
tot=0; found=False
for e in entries:
    k=e.get('key'); v=e.get('value')
    kid=0
    if isinstance(k,dict): kid=int(k.get('cgroup_id',0))
    elif isinstance(k,list):
        for i,b in enumerate(k[:8]): kid|=int(b,0)<<(8*i)
    if kid!=cgid: continue
    found=True
    if isinstance(v,dict): tot+=int(v.get(field,0))
    elif isinstance(v,list):
        off,sz=OFF[map_name][field]
        tot+=int.from_bytes(bytes(int(x,0) for x in v[off:off+sz]),'little')
print(tot if found else -1)
" "$sel" "$cgid" "$field"
}
meta_field()   { map_field pinned        "$1" "$2"; }
tenant_field() { map_field tenant_state  "$1" "$2"; }

# hot switch: attach new policy for the cgroup (kernel replaces old),
# confirm owner_token flip, then stop old loader.
hot_switch() { # cgroup new_policy prio weight tag
    local cg="$1" newpol="$2" prio="$3" weight="$4" tag="$5"
    local cgid; cgid=$(stat -c %i "$cg")
    local oldpol="${LOADER_POL[$cg]:-none}"
    local oldpid="${LOADER_PID[$cg]:-0}"
    local oldtok; oldtok=$(meta_field "$cgid" owner_token)

    snapshot_all "pre_${tag}"
    dmesg -c > "$EV/dmesg_${tag}.log" 2>/dev/null || true

    # queued-request confirmation: queue policy side needs queue_depth>0;
    # bypass side (q_none active) has no sched queue -- record honestly.
    # Fast polling: standing scheduler backlog may be transient at low QD.
    local queued_ok=0 qd=-1 pend=0 tryn=0
    while [ "$tryn" -lt 40 ]; do
        qd=$(meta_field "$cgid" queue_depth)
        pend=$(tenant_field "$cgid" pending)
        if [ "$qd" -gt 0 ] || [ "$pend" -gt 0 ]; then queued_ok=1; break; fi
        sleep 0.05; tryn=$((tryn+1))
    done
    local t_switch=$(date +%s.%N)
    start_policy "$newpol" "$cg" "$prio" "$weight"
    local newpid=$RET_PID
    # wait for owner_token flip (new loader upserts meta before attach)
    local flipped=0 i
    for i in $(seq 1 50); do
        local tok; tok=$(meta_field "$cgid" owner_token)
        if [ "$tok" != "$oldtok" ] && [ "$tok" != "-1" ] && [ "$tok" != "0" ]; then
            flipped=1; break
        fi
        sleep 0.1
    done
    if [ "$flipped" = "1" ]; then
        pass "switch[$tag] $oldpol->$newpol owner_token flipped (cg=$cgid queued_ok=$queued_ok qd=$qd pend=$pend)"
    else
        fail "switch[$tag] $oldpol->$newpol" "owner_token did not flip in 5s"
    fi
    sleep 2   # let replace teardown/evacuate get underway
    if [ "$oldpid" != "0" ] && kill -0 "$oldpid" 2>/dev/null; then
        kill -TERM "$oldpid" 2>/dev/null || true
        wait "$oldpid" 2>/dev/null || true
    fi
    dmesg -c > "$EV/dmesg_${tag}_post.log" 2>/dev/null || true
    snapshot_all "post_${tag}"
    printf '{"t":%s,"tag":"%s","cgroup":"%s","cgid":%s,"from":"%s","to":"%s","queued_ok":%s,"queue_depth":%s,"pending":%s,"flipped":%s}\n' \
        "$t_switch" "$tag" "$cg" "$cgid" "$oldpol" "$newpol" "$queued_ok" "$qd" "$pend" "$flipped" \
        >> "$EV/switches.jsonl"
    LOADER_PID[$cg]=$newpid
    LOADER_POL[$cg]=$newpol
}

# ------------------------------------------------------------- fio runner
run_fio_segment() { # cgroup bs qd runtime seed tag
    cat > "$EV/fio_$5.fio" <<FEOF
[job]
filename=/dev/$DEVICE
rw=randread
bs=$2
ioengine=libaio
direct=1
iodepth=$3
time_based=1
runtime=$4
randseed=$6
norandommap=1
FEOF
    SEG_START=$(date +%s.%N)
    ( echo "$BASHPID" > "$1/cgroup.procs"; exec fio "$EV/fio_$5.fio" \
        --output-format=json --output="$EV/fio_$5.json" ) >/dev/null 2>&1 &
    FIO_PID=$!
}

# ------------------------------------------------------------- matrix run
read -ra SEQ_ARR <<< "$POLICY_SEQ"
N_SEG=${#SEQ_ARR[@]}
log "--- run: $N_SEG segments x ${SEG_LEN}s (switch at +${T_SWITCH}s for seg>=2) ---"

# attach initial policy (idle device, before any load)
if [ "$MATRIX" = "d" ]; then
    start_policy "${SEQ_ARR[0]}" "$CG_LO" 0 100
    LOADER_PID[$CG_LO]=$RET_PID; LOADER_POL[$CG_LO]="${SEQ_ARR[0]}"
    start_policy "q_mq_deadline" "$CG_HI" 1 200
    LOADER_PID[$CG_HI]=$RET_PID; LOADER_POL[$CG_HI]="q_mq_deadline"
    sleep 2
    pass "initial attach: lo=${SEQ_ARR[0]}(prio0/w100) hi=q_mq_deadline(prio1/w200)"
else
    start_policy "${SEQ_ARR[0]}" "$CG_S" 0 100
    LOADER_PID[$CG_S]=$RET_PID; LOADER_POL[$CG_S]="${SEQ_ARR[0]}"
    sleep 2
    pass "initial attach: ${SEQ_ARR[0]}"
fi
snapshot_all "initial"

# matrix d: high-prio tenant runs ONE continuous fio over the whole window.
# The run's real duration is N_SEG*(SEG_LEN+20)+ slack (each hot switch costs
# ~20s of script overhead: pre/post snapshots, token-flip wait, SIGTERM, drain).
# HI_FIO runtime must cover that; sizing it to N_SEG*SEG_LEN made hi's fio
# expire mid-run (first full-d run: fio ended at t=158.6s, first switch AFTER
# that at t=162.7s) -- the "hi tenant service freeze" of NOTES 4.4 was this
# artifact: serve_selects/inflight froze because the workload was gone, not
# because the arb stopped selecting (hi implied==fio was 1:1 its whole life).
if [ "$MATRIX" = "d" ]; then
    # matches the driver's per-switch budget (SEG_LEN+20); the script blocks on
    # wait HI_PID before the drain asserts, so hi's fio ends before draining.
    TOT_RUNTIME=$(( N_SEG * (SEG_LEN + 20) ))
    cat > "$EV/fio_hi.fio" <<FEOF
[hi]
filename=/dev/$DEVICE
rw=randread
bs=64k
ioengine=libaio
direct=1
iodepth=8
time_based=1
runtime=$TOT_RUNTIME
randseed=$(( BASE_SEED + 7777 ))
norandommap=1
FEOF
    ( echo "$BASHPID" > "$CG_HI/cgroup.procs"; exec fio "$EV/fio_hi.fio" \
        --output-format=json --output="$EV/fio_hi.json" ) >/dev/null 2>&1 &
    HI_PID=$!
    log "hi tenant fio continuous: pid=$HI_PID runtime=${TOT_RUNTIME}s"
fi

for seg in $(seq 1 "$N_SEG"); do
    CUR="${SEQ_ARR[$((seg-1))]}"
    SEED=$(( BASE_SEED + seg * 10 ))
    if [ "$MATRIX" = "d" ]; then
        run_fio_segment "$CG_LO" 4k 1 "$SEG_LEN" "seg_${seg}" "$SEED"
        LO_PID=$FIO_PID
    else
        run_fio_segment "$CG_S" 4k "${HS_QD:-8}" "$SEG_LEN" "seg_${seg}" "$SEED"
        LO_PID=$FIO_PID
    fi
    sleep "$T_SWITCH"
    if [ "$seg" -gt 1 ]; then
        if [ "$MATRIX" = "d" ]; then
            hot_switch "$CG_LO" "$CUR" 0 100 "s${seg}_${LOADER_POL[$CG_LO]}_to_${CUR}"
        else
            hot_switch "$CG_S" "$CUR" 0 100 "s${seg}_${LOADER_POL[$CG_S]}_to_${CUR}"
        fi
    fi
    REMAIN=$(( SEG_LEN - T_SWITCH ))
    sleep "$REMAIN"
    wait "$LO_PID" 2>/dev/null || true
    SEG_END=$(date +%s.%N)
    # per-segment fio verdict
    python3 - "$EV/fio_seg_${seg}.json" "$seg" "$SEG_START" "$SEG_END" <<'PYEOF' >> "$EV/fio_seg_summary.jsonl"
import json, re, sys
path, seg, t0, t1 = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    raw = open(path).read()
    d = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
    j = d["jobs"][0]
    print(json.dumps({"seg": int(seg), "error": j.get("error", -1),
                      "total_ios": j["read"]["total_ios"], "io_bytes": j["read"]["io_bytes"],
                      "iops": j["read"]["iops"], "p99_us": j["read"].get("clat_ns", {}).get("percentile", {}).get("99.000000"),
                      "runtime_ms": j["read"]["runtime"],
                      "seg_start": float(t0), "seg_end": float(t1)}))
except Exception as e:
    print(json.dumps({"seg": int(seg), "error": -2, "parse_error": str(e)}))
PYEOF
    SEGERR=$(python3 -c "
import json
try:
    r=json.loads(open('$EV/fio_seg_summary.jsonl').readlines()[-1])
    print(r.get('error',-2))
except Exception: print(-3)")
    [ "$SEGERR" = "0" ] && pass "seg $seg fio error=0" || fail "seg $seg fio" "error=$SEGERR"
done

if [ "$MATRIX" = "d" ]; then
    wait "$HI_PID" 2>/dev/null || true
fi

# ------------------------------------------------------------- drain + assert
log "--- drain and conservation assert ---"
sleep 5
snapshot_all "drain_a"
sleep 5
snapshot_all "drain_b"

python3 - "$EV" "$MATRIX" <<'PYEOF'
import json, os, sys
ev, matrix = sys.argv[1], sys.argv[2]
OFF = {
 "policy_meta": {"queue_depth":(0,8),"dispatches":(8,8),"active_inserts":(16,8),
                 "owner_token":(24,8),"arb_weight":(32,4),"arb_prio":(36,4),"flags":(40,4)},
 "tenant_state": {"deficit":(0,8),"pending":(8,8),"inflight":(16,8),
                  "completed_bytes":(24,8),"completed_cost":(32,8),
                  "dispatches":(40,8),"requeues":(48,8),"requeued_bytes":(56,8)},
}
def load(p):
    try: return json.load(open(os.path.join(ev, p)))
    except Exception: return []
def key_cgid(k):
    if isinstance(k, dict): return int(k.get("cgroup_id", 0))
    if isinstance(k, list):
        v = 0
        for i, b in enumerate(k[:8]): v |= int(b, 0) << (8 * i)
        return v
    return 0
def fields(map_name, e):
    v = e.get("value")
    out = {}
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        for fld, (off, sz) in OFF[map_name].items():
            out[fld] = int.from_bytes(bytes(int(x,0) for x in v[off:off+sz]), "little")
    return out
bad = []
stale = []
def scan(fname, map_name, flds, sink):
    for e in load(fname):
        f = fields(map_name, e)
        for fld in flds:
            if int(f.get(fld, 0)) != 0:
                sink.append((fname, key_cgid(e["key"]), fld, f.get(fld)))
scan("drain_a_policy_meta.json", "policy_meta", ("queue_depth", "active_inserts"), stale)
scan("drain_a_tenant_state.json", "tenant_state", ("pending", "inflight"), stale)
scan("drain_b_policy_meta.json", "policy_meta", ("queue_depth", "active_inserts"), bad)
scan("drain_b_tenant_state.json", "tenant_state", ("pending", "inflight"), bad)
res = {"bad_entries": bad, "stale_entries_at_5s": stale, "ok": not bad}
json.dump(res, open(os.path.join(ev, "drain_assert.json"), "w"), indent=2)
print("DRAIN_ASSERT_OK" if not bad else "DRAIN_ASSERT_BAD: %s" % bad[:20])
PYEOF
DRAIN_OUT=$(python3 -c "
import json
d=json.load(open('$EV/drain_assert.json'))
print('OK' if d['ok'] else 'BAD:%s' % d['bad_entries'][:10])")
[ "$DRAIN_OUT" = "OK" ] && pass "drained (pending=inflight=0, queue_depth=0, active_inserts=0, +10s recheck)" \
    || fail "drain conservation" "$DRAIN_OUT"

dmesg > "$EV/dmesg_final.txt" 2>/dev/null || true
if dmesg 2>/dev/null | grep -iE '\bpanic\b|\boops\b|BUG:|WARNING:|hung_task|soft lockup|lockdep|RCU stall' | grep -vq 'Command line'; then
    fail "kernel_stable" "dmesg kernel errors (see dmesg_final.txt)"
else
    pass "kernel_stable"
fi

kill "$SAMPLER" 2>/dev/null || true
log "=== guest hotswitch END: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
