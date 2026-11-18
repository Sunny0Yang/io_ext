"""scx_io lock contention bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoLockContentionScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoLockContentionScene, "scx_io_lock_contention", "scx_io 并发锁竞争监控")
