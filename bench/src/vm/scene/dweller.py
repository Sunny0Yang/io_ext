"""Queue-dwell distribution bpftrace scene (block_rq_insert→issue, Stage C MLPerfStorage track)."""

from .scx_io_bpftrace_common import BlockIoDwellScene, run_standalone


if __name__ == "__main__":
    run_standalone(BlockIoDwellScene, "dweller", "块层排队驻留 insert→issue / service 分布探针")