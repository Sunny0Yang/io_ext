# TailBench Shore assets

This directory contains the small assets needed for the VM Shore smoke flow.
The default task runs `shore_bench.py` against `db-tpcc-1.smoke` and writes a
TailBench-compatible `lats.bin` file. This proves the VM scene, cgroup wrapper,
io-ext policy loader, command execution, latency parser, scorer, and evaluator
work without a redundant `mode: auto` fallback.

For a real upstream TailBench Shore experiment, download/build TailBench outside
Git and place or mount the required files here, or point the YAML at their
absolute paths:

- `shore_kits_integrated` — upstream integrated Shore binary.
- `db-tpcc-1` — pre-populated TPC-C 1-warehouse Shore-MT database from
  `tailbench.inputs.tgz`.

Those files are large/generated and are ignored by `.gitignore`. Do not commit
them. The scene intentionally assumes configured bench files exist and lets the
underlying command fail naturally if they do not.
