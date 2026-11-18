"""Heimdall v2 decision bpftrace scene."""

from .scx_io_bpftrace_common import MarkerBpftraceScene, run_standalone


class HeimdallDecisionScene(MarkerBpftraceScene):
    SCRIPT_BASENAME = "heimdall_decision.bt"
    DEFAULT_OUTPUT_DIR = "./test_results/block_bpf/heimdall_decision"
    START_MARKER = "HEIMDALL_DECISION_START"
    SUMMARY_BEGIN_MARKER = "HEIMDALL_DECISION_SUMMARY_BEGIN"
    SUMMARY_END_MARKER = "HEIMDALL_DECISION_SUMMARY_END"
    SCALAR_MAPS = ("hd_insert_count", "hd_dispatch_count")
    REQUIRED_MAPS = ()


if __name__ == "__main__":
    run_standalone(HeimdallDecisionScene, "heimdall_decision", "heimdall v2 decision timeline")
