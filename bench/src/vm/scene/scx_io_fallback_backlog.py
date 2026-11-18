"""scx_io fallback backlog depth and policy lifecycle bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoFallbackBacklogScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoFallbackBacklogScene, "scx_io_fallback_backlog", "scx_io fallback backlog 深度与策略生命周期监控")