"""scx_io hot-switch timeline bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoSwitchTimelineScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoSwitchTimelineScene, "scx_io_switch_timeline", "scx_io 热切换时序监控")