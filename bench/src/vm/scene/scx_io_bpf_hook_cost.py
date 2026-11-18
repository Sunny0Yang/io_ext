"""scx_io BPF struct_ops hook boundary cost bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoBpfHookCostScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoBpfHookCostScene, "scx_io_bpf_hook_cost", "scx_io eBPF 函数钩子边界开销监控")
