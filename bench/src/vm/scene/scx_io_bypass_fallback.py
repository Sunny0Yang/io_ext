"""scx_io fallback/bypass bpftrace scene."""

from .scx_io_bpftrace_common import ScxIoBypassFallbackScene, run_standalone


if __name__ == "__main__":
    run_standalone(ScxIoBypassFallbackScene, "scx_io_bypass_fallback", "scx_io fallback bypass 原因与耗时监控")
