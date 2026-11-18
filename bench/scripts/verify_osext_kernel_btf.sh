#!/usr/bin/env bash
# Verify running (or file) kernel BTF exposes iosched_arbiter lifecycle hooks.
# Required for arb_wdrr / arb_slo / arb_cost loaders (struct_ops inserted_request).
#
# Usage:
#   ./scripts/verify_osext_kernel_btf.sh                 # local /sys/kernel/btf/vmlinux
#   ./scripts/verify_osext_kernel_btf.sh /path/vmlinux   # offline vmlinux
#   SSH_ENV=archives/.../ssh_env.sh ./scripts/verify_osext_kernel_btf.sh --remote
set -euo pipefail

REMOTE=0
BTF_SOURCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --remote) REMOTE=1; shift ;;
        -h|--help)
            sed -n '2,8p' "$0"
            exit 0
            ;;
        *)
            BTF_SOURCE="$1"
            shift
            ;;
    esac
done

dump_btf() {
    if [ "$REMOTE" -eq 1 ]; then
        : "${SSH_ENV:?set SSH_ENV for --remote}"
        # shellcheck source=/dev/null
        source "$SSH_ENV"
        remote_sudo "bpftool btf dump file /sys/kernel/btf/vmlinux format c 2>/dev/null"
    elif [ -n "$BTF_SOURCE" ]; then
        bpftool btf dump file "$BTF_SOURCE" format c 2>/dev/null
    else
        bpftool btf dump file /sys/kernel/btf/vmlinux format c 2>/dev/null
    fi
}

check_member() {
    local member="$1"
    dump_btf | sed -n '/struct iosched_arbiter_ops {/,/^};/p' | grep -q "${member}"
}

fail() {
    echo "ERROR: $*" >&2
    echo "--- iosched_arbiter_ops (from BTF) ---" >&2
    dump_btf | sed -n '/struct iosched_arbiter_ops {/,/^};/p' >&2 || true
    exit 1
}

command -v bpftool >/dev/null || fail "bpftool not found"

if [ "$REMOTE" -eq 1 ]; then
    KREL="$(source "${SSH_ENV}" && remote 'uname -r')"
    echo "$KREL" | grep -q osext || fail "guest not on osext kernel: $KREL"
    echo "guest kernel: $KREL"
fi

for hook in inserted_request dispatched_request dispatch_failed requeue_refund; do
    check_member "(*${hook})" || fail "missing iosched_arbiter_ops.${hook} in kernel BTF"
done
check_member "arb_mode" || fail "missing iosched_arbiter_ops.arb_mode in kernel BTF"

if [ "$REMOTE" -eq 1 ] && [ -n "${REQUIRE_STAMP:-}" ] && [ -f "$REQUIRE_STAMP" ]; then
    RUNNING="$(remote 'cat /proc/version' 2>/dev/null || true)"
    echo "$RUNNING" | grep -q 'stale-ioext' && \
        fail "running kernel is stale prebuilt (stale-ioext); refresh /boot/efi/EFI/osext/vmlinuz.efi"
    echo "running kernel: $RUNNING"
fi

echo "OK: iosched_arbiter_ops lifecycle hooks present in BTF"
