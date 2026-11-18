"""Priority inversion bpftrace scene."""

from .scx_io_bpftrace_common import MarkerBpftraceScene, run_standalone


class PrioInversionScene(MarkerBpftraceScene):
    SCRIPT_BASENAME = "prio_inversion.bt"
    DEFAULT_OUTPUT_DIR = "./test_results/block_bpf/prio_inversion"
    START_MARKER = "PRIO_INVERSION_START"
    SUMMARY_BEGIN_MARKER = "PRIO_INVERSION_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "PRIO_INVERSION_SUMMARY_END"
    SCALAR_MAPS = (
        "prio_inversion_count",
        "high_insert_count",
        "low_insert_count",
        "high_pending",
    )
    REQUIRED_MAPS = ()


if __name__ == "__main__":
    run_standalone(PrioInversionScene, "prio_inversion", "block IO priority inversion detector")
