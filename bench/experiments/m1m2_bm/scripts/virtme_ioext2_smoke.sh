#!/usr/bin/env bash
# Runs INSIDE virtme-ng guest (root). Host entry: experiments/m1m2_bm/scripts/run_virtme_ioext2_smoke.sh
set -euo pipefail

echo "=== uname ==="
uname -a

echo "=== io.cost @ root ==="
test -e /sys/fs/cgroup/io.cost.model && echo IOCOST_OK || { echo IOCOST_MISSING; exit 1; }

echo "=== io.latency @ child cgroup (kernel semantic) ==="
cd /sys/fs/cgroup
echo +io > cgroup.subtree_control
mkdir -p cgdeploy
test -f cgdeploy/io.latency && echo IOLAT_OK || { echo IOLAT_MISSING; exit 1; }

echo "=== block scheduler ==="
dev="$(lsblk -ndo NAME,TYPE | awk '$2=="disk" {print $1; exit}')"
if [ -z "$dev" ]; then
  echo "NO_DISK"
  exit 1
fi
echo "disk=$dev sched=$(cat "/sys/block/$dev/queue/scheduler")"
for s in none mq-deadline bfq kyber; do
  if grep -qw "$s" "/sys/block/$dev/queue/scheduler"; then
    echo "${s}=present"
  else
    echo "${s}=absent"
  fi
done
grep -qw bfq "/sys/block/$dev/queue/scheduler" || { echo BFQ_MISSING; exit 1; }

echo "=== fio smoke ==="
mkdir -p /tmp/vng_smoke && cd /tmp/vng_smoke
fio --name=s --filename=s --size=16M --bs=4k --rw=randread --direct=1 --iodepth=4 \
  --ioengine=io_uring --runtime=2 --time_based 2>&1 | grep -m1 IOPS || { echo FIO_FAIL; exit 1; }

echo "=== dmesg tail ==="
if dmesg | tail -n 100 | grep -Ei 'BUG:|Oops:|panic'; then
  echo DMESG_BAD
  exit 1
fi
echo DMESG_OK
echo VIRTME_SMOKE_PASS
