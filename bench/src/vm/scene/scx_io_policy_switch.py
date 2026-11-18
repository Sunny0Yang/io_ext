"""scx_io policy switch and teardown evacuation bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoPolicySwitchScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoPolicySwitchScene, "scx_io_policy_switch", "scx_io 策略热切换与排空监控")
