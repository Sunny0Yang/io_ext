"""Merged block latency and software path cost bpftrace scene."""

from .scx_io_bpftrace_common import BlockIoLatencyCostScene, run_standalone


if __name__ == "__main__":
    run_standalone(BlockIoLatencyCostScene, "block_io_latency_cost", "合并块层延迟与 IO 栈软件路径耗时")
