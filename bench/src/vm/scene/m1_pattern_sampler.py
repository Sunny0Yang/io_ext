"""M1 solo load-pattern per-request sampler scene (task B, P0-2/M1)."""

from .scx_io_bpftrace_common import M1PatternSamplerScene, run_standalone


if __name__ == "__main__":
    run_standalone(M1PatternSamplerScene, "m1_pattern_sampler", "M1 solo load-pattern per-request sampler")