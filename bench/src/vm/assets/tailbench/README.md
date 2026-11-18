# TailBench assets

VM TailBench scenes keep their small, reproducible smoke helpers here. Large
upstream benchmark inputs are intentionally not committed.

For Shore runs, see `shore/README.md`. The default VM smoke uses the small
`shore_bench.py` helper and `db-tpcc-1.smoke` file to exercise the cgroup,
io-ext loader, command execution, and TailBench `lats.bin` parser path. For a
real upstream TailBench Shore run, place the upstream binary and input database
under `src/vm/assets/tailbench/shore/` or override the YAML paths explicitly.
