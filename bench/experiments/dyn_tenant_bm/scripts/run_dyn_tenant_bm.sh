#!/usr/bin/env bash
# experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh
#
# bm (bare-metal, root disk) control-host driver for the six-phase dynamic
# multi-tenant main experiment (IoExtTimelineScene) — the kvmhost pipeline
# (scripts/run_dyn_tenant_kvmhost.sh) ported off the exclusive-disk model.
#
# HARD SAFETY RULE (bare-metal has NO exclusive experiment disk; the campaign runs on
# the ROOT device nvme0n1, root fs /dev/nvme0n1p2 ext4, 915G):
#   * NEVER write one byte to the raw block device /dev/nvme0n1.
#     - tenant fio targets PREALLOCATED REGION FILES
#       $DEPLOY_ROOT/dyn_tenant_regions/region_{a,b,c,d}.bin (30G each,
#       ~120G total), filled once by fill_once (the media-preconditioning
#       analogue of kvmhost's whole-disk fill);
#     - the tenant "filename" is a RESERVED scene key: the timeline scene
#       (20261106 fix) uses it as the SINGLE fio --filename target and REJECTS
#       /dev* targets at config time.  (It must NOT ride the generic spec
#       passthrough: fio treats multiple --filename occurrences as a
#       multi-file STRIPE -- verified fio-3.36: an absent first --filename is
#       created at --size/nfiles and the I/O range splits across both files;
#       the fio JSON "job options" echo keeps only the last string and is NOT
#       evidence of an override -- a passthrough copy would have striped
#       tenant D's randwrite over the RAW ROOT DEVICE.);
#     - the generated task YAMLs are audited BEFORE push: every timeline
#       scene's tenant spec MUST carry filename=<REGION_DIR>/region_<t>.bin
#       (a missing override would silently fall back to the RAW DEVICE);
#     - a region-file direct-IO probe re-verifies readability on bm before
#       any tenant runs, and the POST-RUN audit checks each tenant's spawn
#       argv (exactly one --filename, the region file) plus the fio JSON
#       effective filename.
#   * NO mkfs, NO mount, NO raw-device fio anywhere in this script.
#   * Loaders are the 20261106 rebuild against the ioext2 kernel tree
#     (experiments/dyn_tenant_bm/build/loaders/, MANIFEST-verified); the old
#     tree /home/anon/iosched_ext is ioext1-built and NEVER used.
#
# Stages (RUNBOOK_bm.md is the execution-order authority):
#   smoke          DYN_smoke DYN_smoke_ab                 (gen stage 1)
#   calibration    DYN_calib_{a,b,c,d} DYN_calib_t4       (gen stage 1)
#   freeze         LOCAL analyze + freeze of the bm contract
#   iocost         measure io.cost coefficients ON bm (testfile mode) -> contract
#   static_native  DYN_arm_n1..n4                         (gen stage 2)
#   c_arms         DYN_arm_c1..c4 (+ safety-ledger recording, machine bm)
#   f1             DYN_arm_f1_arbfb (frozen schedule terminal state:
#                  whole-run resident; the dynamic-resident identity)
#   validation     (optional) DYN_valid_* s101..s103      (gen stage 2)
#   heldout        DYN_held_s201_{n1..n4,c1..c4,f1}       (gen stage 3, bm arm set)
#                  trailing SEQ args extend/replace the preset (paired seeds
#                  DYN_held_s20{2..5}_{f1_arbfb,n3_native_bfq})
#
# Usage:
#   flock -w 7200 /tmp/dyn_tenant_bm.lock \
#       experiments/dyn_tenant_bm/scripts/run_dyn_tenant_bm.sh --stage smoke
#   ... [--stage NAME] [--sync-only] [--skip-sync] [--skip-run] [--no-fill]
#       [--config FILE] [SEQ ...]
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
SSH_ENV="${SSH_ENV:-$BASE_DIR/experiments/m1m2_bm/scripts/ssh_env_bm.sh}"

DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/ioext}"
TARGET_DEVICE="nvme0n1"
TARGET_KERNEL="7.3.1-ioext2"
REGION_DIR="$DEPLOY_ROOT/dyn_tenant_regions"
REGION_SIZE="${REGION_SIZE:-30G}"
REMOTE_LOADER_DIR="$DEPLOY_ROOT/io-ext/userspace"
REMOTE_CONFIG_REL="src/vm/configs/task/isol_bench/rig_dynamic_tenant/bm"
REMOTE_RESULTS_REL="results/dyn_tenant_bm"
LOCAL_BUILD="$BASE_DIR/experiments/dyn_tenant_bm/build/loaders"
LOCAL_RESULTS="$BASE_DIR/experiments/dyn_tenant_bm/results"
CONTRACT_DIR="${CONTRACT_DIR:-$BASE_DIR/experiments/dyn_tenant_bm/contract}"
HELDOUT_ARMS_bm="n1_native_none,n2_native_mqddl,n3_native_bfq,n4_native_kyber,c1_io_max,c2_bfq_weight,c3_io_latency,c4_io_cost,f1_arbfb"
LOADER_SET="loader_q_none loader_q_fifo loader_q_ddl loader_q_sp_edf loader_q_wedf loader_arb_wdrr loader_arb_feedback"
GEN_TMP=/tmp/dyn_tenant_bm_yaml
MIN_FREE_GIB=140          # 4x30G regions + results/test_results margin
IOCOST_TESTFILE_GB="${IOCOST_TESTFILE_GB:-16}"

STAGE=""
SYNC_ONLY=0
SKIP_SYNC=0
SKIP_RUN=0
NO_FILL=0
CONFIG=""
SEQS=""

log()  { echo "[dyn86 $(date +%H:%M:%S)] $*"; }
die()  { echo "BLOCKED: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --stage)        STAGE="$2"; shift 2 ;;
        --config)       CONFIG="$2"; shift 2 ;;
        --sync-only)    SYNC_ONLY=1; shift ;;
        --skip-sync)    SKIP_SYNC=1; shift ;;
        --skip-run)     SKIP_RUN=1; shift ;;
        --no-fill)      NO_FILL=1; shift ;;
        -h|--help)      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              break ;;
    esac
done
EXTRA_SEQS="${*:-}"

# ---- region size -> bytes (fill + guards share the same number) ------------
case "$REGION_SIZE" in
    *G) REGION_BYTES=$(( ${REGION_SIZE%G} * 1024 * 1024 * 1024 )) ;;
    *g) REGION_BYTES=$(( ${REGION_SIZE%g} * 1024 * 1024 * 1024 )) ;;
    *)  die "unsupported REGION_SIZE '$REGION_SIZE' (use e.g. 30G)" ;;
esac

# ---- stage presets ----------------------------------------------------------
case "$STAGE" in
    smoke)
        CONFIG="${CONFIG:-dynamic_tenant_smoke.yml}"
        SEQS="${EXTRA_SEQS:-DYN_smoke DYN_smoke_ab}" ;;
    calibration)
        CONFIG="${CONFIG:-dynamic_tenant_calibration.yml}"
        SEQS="${EXTRA_SEQS:-DYN_calib_a DYN_calib_b DYN_calib_c DYN_calib_d DYN_calib_t4}" ;;
    freeze|iocost)
        SEQS="" ;;
    static_native)
        CONFIG="${CONFIG:-dynamic_tenant_static_arms.yml}"
        SEQS="${EXTRA_SEQS:-DYN_arm_n1_native_none DYN_arm_n2_native_mqddl DYN_arm_n3_native_bfq DYN_arm_n4_native_kyber}" ;;
    c_arms)
        CONFIG="${CONFIG:-dynamic_tenant_static_arms.yml}"
        SEQS="${EXTRA_SEQS:-DYN_arm_c1_io_max DYN_arm_c2_bfq_weight DYN_arm_c3_io_latency DYN_arm_c4_io_cost}" ;;
    f1)
        CONFIG="${CONFIG:-dynamic_tenant_static_arms.yml}"
        SEQS="${EXTRA_SEQS:-DYN_arm_f1_arbfb}" ;;
    validation)
        CONFIG="${CONFIG:-dynamic_tenant_validation.yml}"
        SEQS="${EXTRA_SEQS:-DYN_valid_f1_arbfb_s101 DYN_valid_n3_native_bfq_s101 DYN_valid_f1_arbfb_s102 DYN_valid_n3_native_bfq_s102 DYN_valid_f1_arbfb_s103 DYN_valid_n3_native_bfq_s103}" ;;
    heldout)
        CONFIG="${CONFIG:-dynamic_tenant_heldout.yml}"
        SEQS="${EXTRA_SEQS:-DYN_held_s201_n1_native_none DYN_held_s201_n2_native_mqddl DYN_held_s201_n3_native_bfq DYN_held_s201_n4_native_kyber DYN_held_s201_c1_io_max DYN_held_s201_c2_bfq_weight DYN_held_s201_c3_io_latency DYN_held_s201_c4_io_cost DYN_held_s201_f1_arbfb}" ;;
    "")
        die "no --stage given (smoke|calibration|freeze|iocost|static_native|c_arms|f1|validation|heldout); see --help" ;;
    *)  die "unknown stage '$STAGE'" ;;
esac
case "$STAGE" in freeze|iocost) : ;; *) [ -n "$CONFIG" ] || die "stage $STAGE needs a CONFIG (internal error)" ;; esac

# shellcheck disable=SC1090
source "$SSH_ENV"

# ---------------------------------------------------------------------------
# local generation (control machine) + audit + push
# ---------------------------------------------------------------------------
GEN_COMMON_ENV=(
    "DYN_TENANT_TARGET_DEVICE=$TARGET_DEVICE"
    "DYN_TENANT_REGION_DIR=$REGION_DIR"
    "DYN_TENANT_REGION_SIZE=$REGION_SIZE"
    "DYN_TENANT_LOADER_DIR=$REMOTE_LOADER_DIR"
    "DYN_TENANT_LOADER_LOG_DIR=$DEPLOY_ROOT/results/dyn_tenant_bm/loader_scene_logs"
)

audit_yamls() {
    python3 "$BASE_DIR/experiments/dyn_tenant_bm/scripts/audit_dyn_tenant_bm_yamls.py" \
        --region-dir "$REGION_DIR" --files "$@" \
        || die "generated-yaml audit FAILED (tenant fio would target the RAW DEVICE); refusing to push"
}

gen_stage1() {   # pre-freeze: smoke + calibration (no contract, no C arms)
    rm -rf "$GEN_TMP" && mkdir -p "$GEN_TMP"
    log "gen stage 1 (NO_CALIB, --skip-static-arms) -> $GEN_TMP"
    env "${GEN_COMMON_ENV[@]}" DYN_TENANT_NO_CALIB=1 \
        python3 "$BASE_DIR/scripts/gen_dynamic_tenant_yamls.py" \
        --skip-static-arms --output-dir "$GEN_TMP" \
        > "$LOCAL_RESULTS/gen_stage1.log" 2>&1 \
        || { tail -5 "$LOCAL_RESULTS/gen_stage1.log" >&2; die "config generation FAILED"; }
    audit_yamls "$GEN_TMP/dynamic_tenant_smoke.yml" "$GEN_TMP/dynamic_tenant_calibration.yml"
}

gen_stage2() {   # post-freeze: static arms (bare-metal contract + bm iocost coefficients)
    local calib="$CONTRACT_DIR/calibration.json" coef="$CONTRACT_DIR/iocost_coef.json"
    [ -f "$calib" ] || die "stage 2 needs the frozen bm contract: $calib missing (run --stage freeze first)"
    [ -f "$coef" ]  || die "stage 2 needs measured io.cost coefficients: $coef missing (run --stage iocost first)"
    rm -rf "$GEN_TMP" && mkdir -p "$GEN_TMP"
    log "gen stage 2 (bare-metal contract + iocost coef) -> $GEN_TMP"
    env "${GEN_COMMON_ENV[@]}" \
        DYN_TENANT_CALIB_JSON="$calib" \
        DYN_TENANT_IOCOST_COEF_JSON="$coef" \
        python3 "$BASE_DIR/scripts/gen_dynamic_tenant_yamls.py" \
        --output-dir "$GEN_TMP" \
        > "$LOCAL_RESULTS/gen_stage2.log" 2>&1 \
        || { tail -8 "$LOCAL_RESULTS/gen_stage2.log" >&2; die "config generation FAILED"; }
    grep -E "^\[C-arm constants\]" "$LOCAL_RESULTS/gen_stage2.log" \
        || { echo "FATAL: C-arm constants derivation line missing from the gen log" >&2; die "gen stage 2 provenance check failed"; }
    audit_yamls "$GEN_TMP"/dynamic_tenant_{smoke,calibration,virtme,static_arms}.yml
    if [ "$STAGE" = validation ]; then
        env "${GEN_COMMON_ENV[@]}" \
            DYN_TENANT_STATIC_YML="$GEN_TMP/dynamic_tenant_static_arms.yml" \
            python3 "$BASE_DIR/scripts/gen_dynamic_tenant_yamls.py" \
            --emit-validation "$GEN_TMP/dynamic_tenant_validation.yml" \
            >> "$LOCAL_RESULTS/gen_stage2.log" 2>&1 \
            || die "validation config generation FAILED"
        audit_yamls "$GEN_TMP/dynamic_tenant_validation.yml"
    fi
}

gen_stage3() {   # heldout (bare-metal nine-arm set; static yml from stage 2)
    gen_stage2   # idempotent refresh; heldout copies the static C-arm constants
    log "gen stage 3 (heldout, bm arm set) -> $GEN_TMP"
    env "${GEN_COMMON_ENV[@]}" \
        DYN_TENANT_STATIC_YML="$GEN_TMP/dynamic_tenant_static_arms.yml" \
        DYN_TENANT_HELDOUT_ARMS="$HELDOUT_ARMS_bm" \
        python3 "$BASE_DIR/scripts/gen_dynamic_tenant_yamls.py" \
        --emit-heldout "$GEN_TMP/dynamic_tenant_heldout.yml" \
        >> "$LOCAL_RESULTS/gen_stage2.log" 2>&1 \
        || die "heldout config generation FAILED"
    python3 "$BASE_DIR/experiments/dyn_tenant_bm/scripts/audit_dyn_tenant_bm_yamls.py" \
        --region-dir "$REGION_DIR" --files "$GEN_TMP/dynamic_tenant_heldout.yml" \
        --expect-arms "$HELDOUT_ARMS_bm" \
        || die "heldout audit FAILED (arm set or region files); refusing to push"
}

stage_configs_to_push() {
    case "$STAGE" in
        smoke|calibration) echo "$GEN_TMP/dynamic_tenant_smoke.yml $GEN_TMP/dynamic_tenant_calibration.yml" ;;
        static_native|c_arms|f1) echo "$GEN_TMP/dynamic_tenant_static_arms.yml" ;;
        validation) echo "$GEN_TMP/dynamic_tenant_static_arms.yml $GEN_TMP/dynamic_tenant_validation.yml" ;;
        heldout) echo "$GEN_TMP/dynamic_tenant_static_arms.yml $GEN_TMP/dynamic_tenant_heldout.yml" ;;
        *) die "stage $STAGE has no configs" ;;
    esac
}

push_configs() {
    log "push task configs -> bm:$REMOTE_CONFIG_REL/"
    ssh_cmd "mkdir -p '$DEPLOY_ROOT/$REMOTE_CONFIG_REL'" \
        || die "cannot mkdir $REMOTE_CONFIG_REL on bm"
    # RAW-DEVICE SAFETY: remove the repo-default (nvme0n1 raw-device quarter)
    # top-level ymls that the repo rsync brought in -- only the audited bm/
    # variants may remain executable (same convention as run_dyn_tenant_kvmhost.sh).
    ssh_cmd "rm -f '$DEPLOY_ROOT'/src/vm/configs/task/isol_bench/rig_dynamic_tenant/*.yml" \
        || die "cannot clear raw-device default ymls on bm"
    # shellcheck disable=SC2086
    scp_cmd -q $1 "$SSH_HOST:$DEPLOY_ROOT/$REMOTE_CONFIG_REL/" \
        || die "config push failed"
}

# ---------------------------------------------------------------------------
# sync: loaders + repo, then configs
# ---------------------------------------------------------------------------
sync_loaders() {
    log "=== sync loaders (ioext2 rebuild) -> $REMOTE_LOADER_DIR ==="
    ( cd "$LOCAL_BUILD" && sha256sum -c MANIFEST.sha256 ) >/dev/null \
        || die "local loader MANIFEST.sha256 mismatch (rerun the loader rebuild)"
    ssh_cmd "mkdir -p '$REMOTE_LOADER_DIR'" || die "cannot mkdir loader dir on bm"
    rsync_cmd "$LOCAL_BUILD"/loader_* "$LOCAL_BUILD"/*.bpf.o "$LOCAL_BUILD"/*.skel.h \
        "$SSH_HOST:$REMOTE_LOADER_DIR/" || die "loader rsync failed"
    for l in $LOADER_SET; do
        ssh_cmd "'$REMOTE_LOADER_DIR/$l' --help >/dev/null 2>&1" \
            || die "$l --help probe failed on bm (static binary not runnable?)"
    done
    local expected actual
    expected=$( cd "$LOCAL_BUILD" && sha256sum $LOADER_SET )
    actual=$(ssh_cmd "cd '$REMOTE_LOADER_DIR' && sha256sum $LOADER_SET")
    [ "$expected" = "$actual" ] || die "remote loader sha256 mismatch"
    log "loaders synced + sha256 verified (7 loaders; /home/anon/iosched_ext untouched)"
}

sync_repo() {
    log "=== rsync ioext-bench repo -> bm:$DEPLOY_ROOT ==="
    # kvmhost-style whole-repo rsync (NO --delete: bm holds unrelated campaign
    # trees data/ experiments/ m2fig2/ staging/ that must survive).  Heavy or
    # irrelevant trees are excluded; the remote .venv/results are never touched.
    rsync_cmd \
        --exclude .git --exclude .venv --exclude results --exclude results.7z \
        --exclude test_results --exclude __pycache__ --exclude '*.pyc' \
        --exclude .zcode --exclude .agents --exclude KubeBurner \
        --exclude PerfKitBenchmarker --exclude ref --exclude experiments \
        "$BASE_DIR/" "$SSH_HOST:$DEPLOY_ROOT/" \
        || { echo "warn: repo rsync non-zero (continuing)" >&2; }
}

# ---------------------------------------------------------------------------
# remote guards (read-only) + fill + fio effective-filename probe
# ---------------------------------------------------------------------------
remote_guards() {
    log "=== remote guards (root-disk assertions) ==="
    # kernel check first, OUTSIDE the interpolated guard script: embedding
    # "$TARGET_KERNEL" inside the SQ-built script text is quoting hell (the
    # naive '"'"$V"'"' dance wrapped the value in literal quotes and the
    # comparison failed as "7.3.1-ioext2 != 7.3.1-ioext2" on every run).
    local remote_kernel
    remote_kernel=$(ssh_cmd 'uname -r' | tr -d '\r')
    [ "$remote_kernel" = "$TARGET_KERNEL" ] \
        || die "kernel guard FAIL: remote '$remote_kernel' != expected '$TARGET_KERNEL'"
    log "remote kernel: $remote_kernel (matches expected)"
    local script
    script='set -euo pipefail
DEPLOY="'"$DEPLOY_ROOT"'"
DEV='"$TARGET_DEVICE"'
[ -b /dev/$DEV ] || { echo "GUARD FAIL: /dev/$DEV missing"; exit 1; }
sch=$(cat /sys/block/$DEV/queue/scheduler)
case "$sch" in *scx_io*) : ;; *) echo "GUARD FAIL: scx_io not available: $sch"; exit 1;; esac
test -x $DEPLOY/.venv/bin/python || { echo "GUARD FAIL: $DEPLOY/.venv missing"; exit 1; }
test -d $DEPLOY/src/vm || { echo "GUARD FAIL: $DEPLOY/src/vm missing (sync first)"; exit 1; }
raw_mounts=$(findmnt -rn -S /dev/$DEV -o TARGET || true)
[ -z "$raw_mounts" ] || { echo "GUARD FAIL: raw device /dev/$DEV mounted at: $raw_mounts"; exit 1; }
avail_kb=$(df -Pk $DEPLOY | tail -1 | awk "{print \$4}")
need_kb='"$((MIN_FREE_GIB * 1024 * 1024))"'
[ "$avail_kb" -ge "$need_kb" ] || { echo "GUARD FAIL: $DEPLOY free ${avail_kb}KiB < need ${need_kb}KiB"; exit 1; }
pgrep -af "vm\.executor" | grep -v pgrep && { echo "GUARD FAIL: vm.executor running"; exit 1; } || true
pgrep -af "loader_(q_|arb_)" | grep -v pgrep && { echo "GUARD FAIL: leftover loader processes"; exit 1; } || true
echo "guard-snapshot kernel=$(uname -r) scheduler=$sch root=$(findmnt -rn -T / -o SOURCE) avail_kb=$avail_kb"
echo "pre-io.cost.model: $(head -c 400 /sys/fs/cgroup/io.cost.model)"
echo "pre-io.cost.qos: $(head -c 400 /sys/fs/cgroup/io.cost.qos)"
echo "GUARDS OK: /dev/$DEV root-disk mode, scx_io present, .venv present, ${avail_kb}KiB free"'
    remote_sudo "$script" || die "remote guards FAILED"
}

fill_once() {
    log "=== fill region files (one-time, $REGION_SIZE x4) ==="
    remote_sudo 'set -euo pipefail
REGION_DIR="'"$REGION_DIR"'"
MARKER="'"$DEPLOY_ROOT"'/results/.dyn_tenant_bm_regions_filled"
REGION_BYTES='"$REGION_BYTES"'
mkdir -p "$REGION_DIR" '"$DEPLOY_ROOT"'/results
if [ -f "$MARKER" ]; then
    # marker alone is NOT enough: verify all four region files still exist at
    # the exact size before honoring the one-time-fill skip
    for t in a b c d; do
        f="$REGION_DIR/region_$t.bin"
        sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
        [ "$sz" = "$REGION_BYTES" ] || { echo "GUARD FAIL: marker exists but $f is ${sz}B != $REGION_BYTES (remove the marker to refill)"; exit 1; }
    done
    echo "fill: already done ($MARKER, 4x$REGION_BYTES verified)"; exit 0; fi
for t in a b c d; do
    f="$REGION_DIR/region_$t.bin"
    if [ -f "$f" ]; then
        sz=$(stat -c%s "$f")
        [ "$sz" = "$REGION_BYTES" ] || { echo "GUARD FAIL: $f exists with size $sz != $REGION_BYTES (remove it or set the same REGION_SIZE)"; exit 1; }
        echo "fill: region_$t already full size, skipping write"
        continue
    fi
    fio --name=fill_$t --filename="$f" --ioengine=io_uring --direct=1 \
        --bs=1M --rw=write --iodepth=16 --size='"$REGION_SIZE"' --output=/dev/null
    sz=$(stat -c%s "$f")
    [ "$sz" = "$REGION_BYTES" ] || { echo "GUARD FAIL: $f size $sz != $REGION_BYTES after fill"; exit 1; }
    echo "fill: region_$t done ($sz bytes)"
done
sync
printf "{\"filled_at_utc\": \"%s\", \"region_size_bytes\": %s, \"files\": [\"a\",\"b\",\"c\",\"d\"], \"note\": \"preallocated region files; tenant fio targets only these\"}\n" "$(date -u +%FT%TZ)" "$REGION_BYTES" > '"$DEPLOY_ROOT"'/results/.dyn_tenant_bm_fill_record.json
touch "$MARKER"
echo "fill: complete (4 x $REGION_BYTES bytes)"' \
        || die "region fill FAILED"
}

region_probe() {
    # Direct-IO readability probe for the region files (O_DIRECT-on-ext4
    # feasibility on bm, R5): each region file must be randread-able with
    # direct=1 through the same engine the tenants use.  NOTE: the historical
    # duplicate-filename probe was REMOVED 20261106 -- fio treats multiple
    # --filename as a multi-file STRIPE (an absent first file is created at
    # --size/nfiles), so the tenant filename is a RESERVED scene key used as
    # the single --filename (scene rejects /dev* targets); the post-run audit
    # checks each tenant's spawn argv instead.
    log "=== region-file direct-IO probe (4 x 2s randread) ==="
    for t in a b c d; do
        remote_sudo "set -euo pipefail
fio --name=probe_$t --filename='$REGION_DIR/region_$t.bin' \\
    --ioengine=io_uring --direct=1 --bs=4k --rw=randread --size=256m \\
    --runtime=2 --time_based=1 --output-format=json --output=/tmp/dyn86_probe_$t.json
'$DEPLOY_ROOT/.venv/bin/python' - <<'PYEOF'
import json
d = json.load(open('/tmp/dyn86_probe_$t.json'))
job = d['jobs'][0]
eff = job['job options']['filename']
assert eff == '$REGION_DIR/region_$t.bin', 'effective filename is %r' % eff
assert job.get('error') == 0, 'fio error %s' % job['error']
print('probe OK: region_$t direct IO, effective filename =', eff)
PYEOF
rm -f /tmp/dyn86_probe_$t.json" \
            || die "region_$t.bin direct-IO probe FAILED — refusing to run tenants"
    done
}

# ---------------------------------------------------------------------------
# run one sequence remotely (as root), pull, audit, validate
# ---------------------------------------------------------------------------
run_sequence() {
    local SEQ="$1"
    case "$SEQ" in
        *[!A-Za-z0-9_]*|'') die "invalid sequence id '$SEQ' (refusing to interpolate into remote paths)" ;;
    esac
    local RUN_LOG="$LOCAL_RESULTS/_logs/${SEQ}.log"
    mkdir -p "$LOCAL_RESULTS/_logs"
    log "=== [$SEQ] remote run (as root, config $CONFIG) ==="
    remote_sudo "rm -rf $DEPLOY_ROOT/$REMOTE_RESULTS_REL/$SEQ $DEPLOY_ROOT/$REMOTE_RESULTS_REL/$SEQ.tar.gz" \
        || die "pre-run cleanup failed for $SEQ"
    local rc=0
    remote_sudo "cd $DEPLOY_ROOT && export PERF_ARENA_WORKSPACE=$DEPLOY_ROOT PYTHONPATH=$DEPLOY_ROOT/src SUDO_PASSWORD='$SUDO_PASS' IOEXT_DESTRUCTIVE_TEST=1 && $DEPLOY_ROOT/.venv/bin/python -m vm.executor --config $REMOTE_CONFIG_REL/$CONFIG --sequence $SEQ --output $REMOTE_RESULTS_REL/$SEQ --log-level INFO; rc=\$?; chmod -R a+rX $REMOTE_RESULTS_REL/$SEQ 2>/dev/null; exit \$rc" \
        > "$RUN_LOG" 2>&1 || rc=$?
    tail -4 "$RUN_LOG" || true
    if [ "$rc" != "0" ]; then
        echo "RUN FAILED rc=$rc (log: $RUN_LOG)" >&2
        return 1
    fi
    echo "$SEQ" >> "$LOCAL_RESULTS/_pass.log"
    return 0
}

pull_results() {
    log "=== pulling results -> $LOCAL_RESULTS ==="
    rsync_cmd "$SSH_HOST:$DEPLOY_ROOT/$REMOTE_RESULTS_REL/" "$LOCAL_RESULTS/" \
        || die "results pull failed"
}

post_audit() {
    # effective-filename audit over every pulled tenant fio JSON
    local SEQ="$1"
    python3 "$BASE_DIR/experiments/dyn_tenant_bm/scripts/audit_fio_effective_filenames.py" \
        --region-dir "$REGION_DIR" --result-dir "$LOCAL_RESULTS/$SEQ" \
        || return 1
}

validate_sequence() {
    # validate_isol_bench_result.py is device-agnostic (config structure +
    # metadata + raw fio JSONs); it resolves base_scene relative to the config
    # copy, so mirror the scene configs next to it.
    local SEQ="$1"
    local cfg_copy="$LOCAL_RESULTS/$CONFIG"
    cp "$GEN_TMP/$CONFIG" "$cfg_copy" 2>/dev/null || true
    if [ -f "$cfg_copy" ]; then
        rm -rf "$LOCAL_RESULTS/scene"
        cp -r "$BASE_DIR/src/vm/configs/scene" "$LOCAL_RESULTS/scene" 2>/dev/null || true
        python3 "$BASE_DIR/scripts/validate_isol_bench_result.py" \
            --config "$cfg_copy" --sequence "$SEQ" \
            --result-dir "$LOCAL_RESULTS/$SEQ" 2>&1 | tail -1 | sed "s/^/[$SEQ] /"
    fi
}

record_c_arm_ledger() {
    # Same-mechanism safety ledger as scripts/record_c_arm_ledger.py (campaign
    # naming convention copied; target machine bm): one ledger attempt per
    # C-arm run, evidence = pulled result dir + audited config + gen log.
    local SEQ="$1" outcome="$2"
    local cfg_arg=""
    [ -f "$GEN_TMP/$CONFIG" ] && cfg_arg="$GEN_TMP/$CONFIG"
    python3 "$BASE_DIR/experiments/dyn_tenant_bm/scripts/record_c_arm_ledger_bm.py" \
        --sequence "$SEQ" --outcome "$outcome" \
        --result-dir "$LOCAL_RESULTS/$SEQ" \
        --config "$cfg_arg" \
        || echo "warn: ledger recording failed for $SEQ (continuing)" >&2
}

# ---------------------------------------------------------------------------
# freeze (local) and iocost (on bm, testfile mode) helpers
# ---------------------------------------------------------------------------
stage_freeze() {
    log "=== freeze the bm contract (LOCAL analyze + freeze) ==="
    mkdir -p "$CONTRACT_DIR"
    python3 "$BASE_DIR/scripts/analyze_dynamic_tenant_calibration.py" \
        --results-root "$LOCAL_RESULTS" --contract-dir "$CONTRACT_DIR" \
        || die "calibration analysis FAILED (see errors above)"
    python3 "$BASE_DIR/scripts/freeze_dynamic_tenant_contract.py" \
        --contract-dir "$CONTRACT_DIR" \
        || die "contract freeze FAILED (missing frozen fields?)"
    log "contract at $CONTRACT_DIR (calibration.json + frozen_manifest.json)"
}

stage_iocost() {
    # iocost_coef_gen (kernel tools/cgroup/iocost_coef_gen.py) in TESTFILE mode.
    # kvmhost ran --testdev /dev/sdb (exclusive disk, destructive) — FORBIDDEN on
    # bm (root disk, never write raw).  Testfile mode writes
    # ./iocost-coef-fio.testfile inside a scratch dir ON the root filesystem and
    # measures the SAME nvme0n1 device through the file-backed path the campaign
    # itself uses (dir_to_dev('.') -> nvme0n1 259:0).  The tool needs `pv`
    # (absent on bm) -> a pass-through shim inside the scratch dir (removed
    # after the run; documented change+revert).  It temporarily sets
    # elevator=none + nomerges=1 on nvme0n1 and restores them (verified).
    log "=== iocost coefficients on bm (testfile mode, ${IOCOST_TESTFILE_GB} GB) ==="
    # 86-fix: the kernel tool verbatim breaks on bm (three proven failures on
    # 2026-11-06: ① upstream `pv` absent -> prefill never runs, and even the
    # shim variant misparsed "-s <size>" and cat'ed a nonexistent file,
    # silently leaving a 0-byte testfile; ② dd bs=16M oflag=direct vs nvme0n1
    # max_sectors_kb=128 -> EINVAL + 0-byte testfile -> every fio dies with
    # "you need to specify size="; ③ partially-converted option spellings).
    # We therefore ship the committed 86-patched tool (build/
    # iocost_coef_gen_bm.py: --rw= spelling, --seqio-block-kb, no pv — dd from
    # /dev/urandom directly, dd bs capped to max_sectors_kb) and record ITS
    # sha; the kernel-tree copy is only kept as the upstream provenance
    # baseline.
    local tool_src="/opt/ioext-io_ext/kernel/linux-7.3.1/tools/cgroup/iocost_coef_gen.py"
    local tool_bm="$BASE_DIR/experiments/dyn_tenant_bm/build/iocost_coef_gen_bm.py"
    local tool_local="$BASE_DIR/experiments/dyn_tenant_bm/build/iocost_coef_gen.py"
    local tool_kernel="$BASE_DIR/experiments/dyn_tenant_bm/build/iocost_coef_gen_kernel_upstream.py"
    local tool_sha
    mkdir -p "$BASE_DIR/experiments/dyn_tenant_bm/build"
    [ -f "$tool_src" ] || die "iocost_coef_gen.py not found at $tool_src (kernel tree missing?)"
    [ -f "$tool_bm" ]  || die "86-patched tool missing: $tool_bm (build/iocost_coef_gen_bm.py)"
    python3 -m py_compile "$tool_bm" || die "86-patched tool does not parse"
    cp "$tool_src" "$tool_kernel"   # upstream baseline, provenance only
    cp "$tool_bm" "$tool_local"
    tool_sha=$(sha256sum "$tool_local" | cut -d' ' -f1)
    local scratch="$DEPLOY_ROOT/dyn_tenant_regions/iocost_probe"
    # dyn_tenant_regions/iocost_probe may exist root-owned (created by an
    # earlier sudo fill) -> user scp would fail with permission denied.
    remote_sudo "mkdir -p '$scratch' && chown $(id -u):$(id -g) '$scratch'"
    scp_cmd -q "$tool_local" "$SSH_HOST:$scratch/iocost_coef_gen.py" || die "tool push failed"
    local pre_sched post_sched
    pre_sched=$(ssh_cmd "cat /sys/block/$TARGET_DEVICE/queue/scheduler")
    log "pre-probe scheduler: $pre_sched"
    local log_local="$CONTRACT_DIR/iocost_coef_gen_stdout.log"
    mkdir -p "$CONTRACT_DIR"
    remote_sudo 'set -euo pipefail
cd "'"$scratch"'"
PATH="$(pwd):$PATH" python3 iocost_coef_gen.py --testfile-size-gb '"$IOCOST_TESTFILE_GB"' --quiet
# keep iocost-coef-fio.testfile: create_testfile reuses it on a rerun when the
# size matches, saving a 16G random rewrite before every re-measurement.
echo PROBE-DONE' > "$log_local" 2>&1 \
        || { tail -10 "$log_local" >&2; die "iocost_coef_gen FAILED on bm"; }
    post_sched=$(ssh_cmd "cat /sys/block/$TARGET_DEVICE/queue/scheduler")
    log "post-probe scheduler: $post_sched"
    [ "$post_sched" = "$pre_sched" ] \
        || { echo "WARN: scheduler changed by the probe: $pre_sched -> $post_sched (restore manually)" >&2; }
    python3 "$BASE_DIR/experiments/dyn_tenant_bm/scripts/assemble_iocost_coef_bm.py" \
        --stdout-log "$log_local" --target-device "$TARGET_DEVICE" \
        --tool-sha256 "$tool_sha" --testfile-gb "$IOCOST_TESTFILE_GB" \
        --out "$CONTRACT_DIR/iocost_coef.json" \
        || die "iocost_coef.json assembly FAILED"
    log "iocost coefficients at $CONTRACT_DIR/iocost_coef.json"
}

# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------
mkdir -p "$LOCAL_RESULTS"

case "$STAGE" in
    freeze) stage_freeze; exit 0 ;;
    iocost) stage_iocost; exit 0 ;;
esac

if [ "$SKIP_SYNC" != "1" ]; then
    case "$STAGE" in
        smoke|calibration) gen_stage1 ;;
        static_native|c_arms|f1|validation) gen_stage2 ;;
        heldout) gen_stage3 ;;
    esac
    sync_loaders
    sync_repo
    # repo rsync re-brought the raw-device default ymls; push_configs removes
    # them again before pushing the audited bm/ variants (last writer wins).
    push_configs "$(stage_configs_to_push)"
fi

if [ "$SYNC_ONLY" = "1" ]; then log "sync-only done"; exit 0; fi

remote_guards
[ "$NO_FILL" = "1" ] || fill_once
region_probe

if [ "$SKIP_RUN" = "1" ]; then log "guards+fill+probe done (--skip-run)"; exit 0; fi

PASS=0; FAIL=0
for SEQ in $SEQS; do
    if run_sequence "$SEQ"; then
        pull_results
        if post_audit "$SEQ"; then
            PASS=$((PASS+1))
            [ "$STAGE" = c_arms ] && record_c_arm_ledger "$SEQ" completed
        else
            FAIL=$((FAIL+1))
            echo "[$SEQ] POST-RUN filename AUDIT FAILED (raw-device access?!)" >&2
            [ "$STAGE" = c_arms ] && record_c_arm_ledger "$SEQ" failed
        fi
        validate_sequence "$SEQ" || true
    else
        FAIL=$((FAIL+1))
        pull_results || true
        [ "$STAGE" = c_arms ] && record_c_arm_ledger "$SEQ" failed
    fi
done

echo "=== dyn_tenant_bm [$STAGE] SUMMARY: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = "0" ]
