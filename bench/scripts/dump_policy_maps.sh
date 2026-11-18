#!/usr/bin/env bash
# Post-run BPF map dump for io-ext explore runs (exploration observability).
set -euo pipefail

OUT_DIR="${1:?usage: dump_policy_maps.sh <output_dir>}"
IOEXT="${IOEXT_WORKSPACE:-/opt/io-ext}"
MAP_DIR="$OUT_DIR/policy_maps"
mkdir -p "$MAP_DIR"

dump_map() {
    local name="$1"
    if sudo bpftool map show name "$name" -j 2>/dev/null | grep -q '"id"'; then
        sudo bpftool map dump name "$name" >"$MAP_DIR/${name}.txt" 2>/dev/null || true
    fi
}

for loader in "$IOEXT"/userspace/loader_*; do
    [ -x "$loader" ] || continue
    base="$(basename "$loader")"
    sudo bpftool prog show name "$base" -j >"$MAP_DIR/${base}_prog.json" 2>/dev/null || true
done

for map_name in \
    hm_v2_cfg iosched_heimdall_v2_stats kyber_merge_cfg \
    iosched_sp_edf_stats iosched_wedf_stats iosched_prio_stats; do
    dump_map "$map_name"
done

if [ -d /sys/fs/bpf ]; then
    sudo find /sys/fs/bpf -maxdepth 3 -type f 2>/dev/null | head -200 >"$MAP_DIR/bpffs_listing.txt" || true
fi

dmesg | tail -80 >"$MAP_DIR/dmesg_tail.txt" 2>/dev/null || true
echo "$MAP_DIR"
