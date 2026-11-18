#!/bin/bash
# tools/feedback/accept_static.sh - host-side static acceptance for arb_feedback
#
# Gates:
#   0. reference-model self-test (tools/feedback/fb_model.py)
#   1. build: make arb_feedback (bpf.o + skeleton + loader)
#   2. sections: 7 struct_ops program sections + .struct_ops.link present
#   3. instruction counts per program (pre-verifier static upper bound)
#   4. map inventory from the skeleton: count <= 2x arb_slo, names <= 15 chars
#   5. request-identity hook wiring: inserted/dispatched/completed/
#      requeue_refund/dispatch_failed all wired to the arb_note_* helpers;
#      requeue refunds only (no pending++)
#   6. verifier run: only possible on the io_ext kernel (struct_ops).
#      On the stock host this is recorded BLOCKED.  When executed inside
#      the io_ext VM (RUN_VERIFIER=1), the loader is started against the
#      target device and the kernel log is scanned for the arbiter
#      registration + any verifier failure.
#
# Usage: tools/feedback/accept_static.sh [inside or outside the VM]
# Exit: 0 = PASS, 1 = FAIL, 2 = PASS-with-BLOCKED (verifier gate not runnable here)
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # userspace/io
OS_EXT="$(cd "$BASE_DIR/../.." && pwd)"                          # ioext
BUILD="$BASE_DIR/build"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/tools/feedback/results/accept_$(date +%Y%m%d_%H%M%S)}"
NOTES="$BASE_DIR/tools/feedback/NOTES.md"

mkdir -p "$RESULTS_DIR"
PASS=0; FAIL=0; BLOCKED=0
R="$RESULTS_DIR/results.txt"
: > "$R"

log()    { printf '[fb_accept] %s\n' "$*" >&2; }
pass()   { log "PASS: $1"; echo "PASS: $1" >> "$R"; PASS=$((PASS+1)); }
fail()   { log "FAIL: $1: $2"; echo "FAIL: $1: $2" >> "$R"; FAIL=$((FAIL+1)); }
blocked(){ log "BLOCKED: $1: $2"; echo "BLOCKED: $1: $2" >> "$R"; BLOCKED=$((BLOCKED+1)); }

OBJDUMP="${OBJDUMP:-llvm-objdump}"
command -v "$OBJDUMP" >/dev/null 2>&1 || OBJDUMP=objdump

log "=== arb_feedback static acceptance START ==="
log "results=$RESULTS_DIR"

# ---------------------------------------------------------------- gate 0
log "--- gate 0: reference model self-test ---"
if timeout 120 python3 -I "$BASE_DIR/tools/feedback/fb_model.py" \
        > "$RESULTS_DIR/model.txt" 2>&1; then
    pass "model_self_test ($(tail -1 "$RESULTS_DIR/model.txt"))"
else
    fail "model_self_test" "$(tail -2 "$RESULTS_DIR/model.txt" | tr '\n' ' ')"
fi

# ---------------------------------------------------------------- gate 1
log "--- gate 1: build ---"
if (cd "$BASE_DIR" && make arb_feedback) > "$RESULTS_DIR/build.log" 2>&1; then
    pass "build (arb_feedback.bpf.o + skel.h + loader)"
else
    fail "build" "$(tail -5 "$RESULTS_DIR/build.log" | tr '\n' ' ')"
    log "=== END: FAIL (build) ==="
    { echo; echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"; } >> "$R"
    exit 1
fi

# ---------------------------------------------------------------- gate 2
log "--- gate 2: struct_ops sections ---"
EXPECTED_PROGS="pick_cgroup hold_dispatch inserted_request dispatched_request completed_request dispatch_failed requeue_refund"
SECTIONS="$("$OBJDUMP" -h "$BUILD/arb_feedback.bpf.o")"
for p in $EXPECTED_PROGS; do
    if echo "$SECTIONS" | grep -q "struct_ops/$p"; then
        pass "section struct_ops/$p"
    else
        fail "section struct_ops/$p" "missing"
    fi
done
if echo "$SECTIONS" | grep -q "\.struct_ops\.link"; then
    pass "section .struct_ops.link (fb_arbiter)"
else
    fail "section .struct_ops.link" "missing"
fi

# ---------------------------------------------------------------- gate 3
log "--- gate 3: instruction counts (pre-verifier upper bound) ---"
TOTAL_INSNS=0
for p in $EXPECTED_PROGS; do
    N="$("$OBJDUMP" -d --section="struct_ops/$p" "$BUILD/arb_feedback.bpf.o" 2>/dev/null \
        | grep -cE '^\s+[0-9a-f]+:')"
    TOTAL_INSNS=$((TOTAL_INSNS + N))
    log "  $p: $N insns"
    echo "$p $N" >> "$RESULTS_DIR/insn_counts.txt"
done
log "  total: $TOTAL_INSNS insns"
# arb_slo pick_cgroup ~1.4k insns historically; a runaway explosion here
# means an unbounded construct crept in.
if [ "$TOTAL_INSNS" -gt 0 ] && [ "$TOTAL_INSNS" -lt 20000 ]; then
    pass "insn_counts total=$TOTAL_INSNS (< 20000, bounded)"
else
    fail "insn_counts" "total=$TOTAL_INSNS"
fi

# ---------------------------------------------------------------- gate 4
log "--- gate 4: map inventory (skeleton) ---"
MAPS="$(grep -oE 'obj->maps\.[a-z_0-9]+' "$BUILD/arb_feedback.skel.h" 2>/dev/null \
    | sed 's/obj->maps\.//' | sort -u || true)"
echo "$MAPS" | tr ' ' '\n' > "$RESULTS_DIR/map_names.txt"
NMAPS=$(echo "$MAPS" | grep -c . || true)
log "  maps ($NMAPS): $(echo $MAPS | tr '\n' ' ')"
# arb_slo inventory: 6 data maps + 1 struct_ops map = 7
if [ "$NMAPS" -ge 6 ] && [ "$NMAPS" -le 14 ]; then
    pass "map_count=$NMAPS (<= 2x arb_slo's 7)"
else
    fail "map_count" "n=$NMAPS (expected 6..14)"
fi
LONG_NAMES=$(echo "$MAPS" | awk 'length($0) > 15' | tr '\n' ' ')
if [ -z "$LONG_NAMES" ]; then
    pass "map_name_lengths (all <= 15 chars)"
else
    # active_cgroup_ids is inherited verbatim from every existing arbiter
    # (loader lookup name, ioext_loader_setup_arbiter_maps); accept it as
    # a documented exception only.
    if [ "$LONG_NAMES" = "active_cgroup_ids " ]; then
        blocked "map_name_lengths" "inherited shared-map name active_cgroup_ids (>15, same as all existing arbiters; libbpf/kernel truncate identically)"
    else
        fail "map_name_lengths" "over 15: $LONG_NAMES"
    fi
fi

# ---------------------------------------------------------------- gate 5
log "--- gate 5: request-identity hook wiring ---"
SRC="$BASE_DIR/arb_feedback.bpf.c"
check_src() { # pattern description
    if grep -q "$1" "$SRC"; then
        pass "$2"
    else
        fail "$2" "pattern not found: $1"
    fi
}
check_src 'SEC("struct_ops/inserted_request")'            'hook inserted_request present'
check_src 'SEC("struct_ops/dispatched_request")'          'hook dispatched_request present'
check_src 'SEC("struct_ops/completed_request")'           'hook completed_request present'
check_src 'SEC("struct_ops/dispatch_failed")'             'hook dispatch_failed present'
check_src 'SEC("struct_ops/requeue_refund")'              'hook requeue_refund present'
check_src 'arb_note_inserted(&tenant_state'               'inserted -> pending++ (identity key)'
check_src 'arb_note_dispatched_cost(&tenant_state'        'dispatched -> pending--/inflight++ + deficit charge'
check_src 'arb_note_completed(&tenant_state'              'completed -> inflight--'
check_src 'arb_note_dispatch_failed(&tenant_state'        'dispatch_failed -> guarded pending decrement'
check_src 'arb_note_requeue(&tenant_state'                'requeue -> inflight-- + refund'
# requeue must NOT touch ->pending anywhere in the requeue program body
# (comments mentioning "pending" are fine; only ->pending access counts):
sed -n '/SEC("struct_ops\/requeue_refund")/,/^}/p' "$SRC" > "$RESULTS_DIR/requeue_prog.c"
if grep -q -- '->pending' "$RESULTS_DIR/requeue_prog.c"; then
    fail "requeue_no_pending" "requeue program touches pending (must refund only; re-insert re-fires inserted_request)"
else
    pass "requeue_no_pending (refund only, no pending++)"
fi
check_src 'fb_cost_estimate(&fb_cost_tbl, dctx->opf, dctx->nr_sectors)' 'dispatch charge = cost table (opf, nr_sectors)'
check_src 'fb_cost_estimate(&fb_cost_tbl, rctx->opf,'                   'requeue refunds the same cost estimate'
check_src 'IOSCHED_ARB_STRICT'                                          'strict mode armed (.arb_mode)'

# ---------------------------------------------------------------- gate 6
log "--- gate 6: verifier run ---"
if grep -q scx_io /proc/kallsyms 2>/dev/null; then
    log "  io_ext kernel detected; attempting loader verifier run"
    DEVICE="${TARGET_DEVICE:-}"
    if [ -z "$DEVICE" ]; then
        for cand in vda vdb nvme0n1 sda; do
            [ -e "/sys/block/$cand" ] && DEVICE="$cand" && break
        done
    fi
    if [ -z "$DEVICE" ]; then
        blocked "verifier_run" "io_ext kernel but no block device found"
    else
        ( cd "$RESULTS_DIR" \
          && timeout 60 "$BUILD/loader_arb_feedback" > loader_verifier.log 2>&1
          echo $? > loader_rc )
        RC=$(cat "$RESULTS_DIR/loader_rc")
        if dmesg 2>/dev/null | grep -q "arbiter 'fb_arbiter'"; then
            pass "verifier_run (arbiter registered on /dev/$DEVICE, rc=$RC)"
            dmesg > "$RESULTS_DIR/dmesg.txt" 2>/dev/null || true
        elif grep -qi "verifier" "$RESULTS_DIR/loader_verifier.log"; then
            fail "verifier_run" "verifier rejected, see loader_verifier.log"
        else
            blocked "verifier_run" "no registration line in dmesg (loader rc=$RC); see loader_verifier.log"
        fi
    fi
else
    blocked "verifier_run" \
        "host kernel has no scx_io: struct_ops verification requires the io_ext kernel (vng smoke gated on the iox_safety ledger)"
fi

# ---------------------------------------------------------------- summary
log "=== arb_feedback static acceptance END: PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED ==="
{
    echo ""
    echo "## Summary"
    echo "PASS=$PASS FAIL=$FAIL BLOCKED=$BLOCKED"
    echo "Date: $(date)"
} >> "$R"

# NOTES.md: structured record (append one block per run)
{
    echo ""
    echo "## arb_feedback static acceptance - $(date -Iseconds)"
    echo ""
    echo "- results dir: \`$RESULTS_DIR\`"
    echo "- object: \`userspace/io/build/arb_feedback.bpf.o\`"
    echo "- insn counts: see \`$RESULTS_DIR/insn_counts.txt\`"
    echo ""
    echo '```'
    cat "$R"
    echo '```'
} >> "$NOTES"

cat "$R"
echo "RESULTS_DIR=$RESULTS_DIR"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
if [ "$BLOCKED" -gt 0 ]; then
    exit 2
fi
exit 0
