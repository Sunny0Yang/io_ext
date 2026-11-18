#!/bin/bash
# gen_fio_job.sh - generate a fio job file for a given load ID
# Usage: gen_fio_job.sh <load_id> <output_file> <target> [runtime] [nproc]
set -euo pipefail

LOAD_ID="${1:?need load_id}"
OUT="${2:?need output_file}"
TARGET="${3:?need target device/file}"
RUNTIME="${4:-120}"
NPROC="${5:-$(nproc 2>/dev/null || echo 32)}"
SIZE="${SIZE:-4G}"
IOENGINE="${IOENGINE:-io_uring}"

case "$LOAD_ID" in
    L1_seq_write_heavy)
        RW="write"; BS="4k"; IODEPTH="128"; RWMIXREAD="" ;;
    L2_seq_rw_heavy)
        RW="rw"; BS="4k"; IODEPTH="64"; RWMIXREAD="rwmixread=30" ;;
    L3_rand_write_heavy)
        RW="randwrite"; BS="4k"; IODEPTH="64"; RWMIXREAD="" ;;
    L4_mixed_bs)
        RW="write"; BS="16k"; IODEPTH="64"; RWMIXREAD="" ;;
    L5_seq_read_heavy)
        RW="read"; BS="4k"; IODEPTH="64"; RWMIXREAD="" ;;
    L6_low_qd_regress)
        RW="write"; BS="4k"; IODEPTH="2"; NPROC="2"; RWMIXREAD="" ;;
    *)
        echo "unknown load_id: $LOAD_ID" >&2; exit 1 ;;
esac

MIX_LINE=""
if [ -n "$RWMIXREAD" ]; then
    MIX_LINE="  ${RWMIXREAD}"
fi

cat > "$OUT" <<EOF
[global]
  runtime=${RUNTIME}
  time_based=1
  direct=1
  ioengine=${IOENGINE}
  group_reporting=1
  numjobs=${NPROC}
  iodepth=${IODEPTH}
  size=${SIZE}
  filename=${TARGET}
  log_avg_msec=1000
  write_bw_log=1
  write_lat_log=1
  write_iops_log=1

[job]
  rw=${RW}
  bs=${BS}${MIX_LINE}
EOF
echo "[gen_fio_job] $LOAD_ID -> $OUT (rw=$RW bs=$BS iodepth=$IODEPTH nproc=$NPROC rt=${RUNTIME}s)" >&2