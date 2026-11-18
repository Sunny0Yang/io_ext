#!/usr/bin/env bash
# sc_sampler_bm.sh — 20Hz in-flight / block-stat sampler (runs on kvmhost).
# Usage: sc_sampler_bm.sh OUT.csv [INTERVAL_S]
# Columns: uptime_s, inflight_read+write, sdb stat(15 fields), memfree_kb, load1
set -u
OUT="${1:?usage: sc_sampler_bm.sh OUT.csv [INTERVAL_S]}"
INTERVAL="${2:-0.05}"
: > "$OUT"
while :; do
  UP=$(cut -d' ' -f1 /proc/uptime)
  INFL=$(cat /sys/block/sdb/inflight 2>/dev/null | tr ' ' '+')
  STAT=$(cat /sys/block/sdb/stat 2>/dev/null | tr -s ' ' ',')
  MF=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  LD=$(cut -d' ' -f1 /proc/loadavg)
  echo "$UP,$INFL,$STAT,$MF,$LD" >> "$OUT"
  sleep "$INTERVAL"
done
