#!/usr/bin/env python3
"""Unit/provenance checks for the D1' ladder renderer (run d1_ladder_141816)."""

import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer = load_module("d1_ladder_renderer", "render_d1_ladder_bm.py")
DATA = renderer.BASE / "results/D1_overhead_bm_data" / renderer.RUN_ID


class D1LadderChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = renderer.load_run(DATA)

    def test_rep_count_inventory(self):
        data = self.data
        # 5 ladder arms x 5 reps, all present (load_run asserts file existence)
        self.assertEqual(len(data["ladder"]), 25)
        self.assertEqual({arm for arm, _ in data["ladder"]}, set(renderer.LADDER_ARMS))
        self.assertEqual({rep for _, rep in data["ladder"]}, set(renderer.LADDER_REPS))
        # 30 E4a runs x 4 jobs
        self.assertEqual(len(data["e4a"]), 30)
        for jobs in data["e4a"].values():
            self.assertEqual([j["job"] for j in jobs], [1, 2, 3, 4])
        # 40 E4b runs (4 N x 2 stacks x 5 reps)
        self.assertEqual(len(data["e4b"]), 40)
        self.assertEqual({n for n, _, _ in data["e4b"]}, set(renderer.E4B_N))
        # attach: 5 loaders x 5 reps = 20 samples, positionally parsed
        self.assertEqual(len(data["attach"]), 20)
        # device probe present
        self.assertGreater(data["probe"]["iops"], 40000)

    def test_cpu_us_per_io_recomputed_independently(self):
        # 3 sampled runs: recompute from the raw fio json with the documented
        # formula and compare against the renderer's value.
        samples = [
            DATA / "ladder/bare_r1/ta_fio.json",
            DATA / "ladder/qfifo_arb_r3/ta_fio.json",
            DATA / "e4b/N7_q_noop_r2/act_fio.json",
        ]
        for path in samples:
            document = json.loads(path.read_text())
            job = document["jobs"][0]
            expected = (job["usr_cpu"] + job["sys_cpu"]) / 100.0 \
                * job["job_runtime"] / 1000.0 \
                / job["read"]["total_ios"] * 1e6
            parsed = renderer.job_stats(path)
            self.assertAlmostEqual(parsed["cpu_us_per_io"], expected, places=9,
                                   msg=str(path))
            # sanity: fio json usr_cpu/sys_cpu are % of one core, job_runtime in ms
            self.assertAlmostEqual(job["job_runtime"] / 1000.0, 10.0, places=6)

    def test_e4a_aggregate_iops_is_sum_of_jobs(self):
        e4a = renderer.e4a_aggregates(self.data["e4a"])
        self.assertEqual(len(e4a), 6)  # 3 layouts x 2 stacks
        for (layout, stack), reps in e4a.items():
            self.assertEqual(len(reps), 5)
            for rep_index, rep in enumerate(renderer.LADDER_REPS):
                jobs = self.data["e4a"][(layout, stack, rep)]
                agg = sum(j["iops"] for j in jobs)
                self.assertAlmostEqual(agg, reps[rep_index]["agg_iops"], places=6)
        # headline anchors (recomputed from raw data, not hardcoded inputs)
        native = renderer.median([r["agg_iops"] for r in e4a[("4on1cg", "native")]])
        q_noop = renderer.median([r["agg_iops"] for r in e4a[("4on1cg", "q_noop")]])
        self.assertTrue(215000 < native < 226000, native)
        self.assertTrue(195000 < q_noop < 210000, q_noop)
        # the two known outlier reps are present and retained
        self.assertAlmostEqual(min(r["agg_iops"] for r in e4a[("4on1cg", "q_noop")]),
                               162801, delta=2)
        self.assertAlmostEqual(min(r["agg_iops"] for r in e4a[("fourx1", "q_noop")]),
                               166823, delta=2)

    def test_attach_medians_positional_parse(self):
        groups = renderer.attach_policy_groups(self.data["attach"])
        # the stale header must not leak spawn/seen epochs into attach_ms
        for policy, values in groups.items():
            for value in values:
                self.assertGreater(value, 5.0, policy)   # not an epoch
                self.assertLess(value, 100.0, policy)    # not a delta of epochs
        self.assertAlmostEqual(renderer.median(groups["q_noop"]), 11.9, places=6)
        self.assertAlmostEqual(renderer.median(groups["arb_noop"]), 11.4, places=6)
        self.assertAlmostEqual(renderer.median(groups["q_fifo"]), 23.7, places=6)
        self.assertEqual(len(groups["q_fifo"]), 10)  # 2 arms x 5 reps

    def test_ladder_aggregates_match_sanity_anchors(self):
        arms = renderer.arm_aggregates(self.data["ladder"])
        anchors = {  # (median IOPS, median cpu_us_per_io) from the runner's report
            "native": (58889, 2.538), "bare": (58905, 2.530),
            "q_noop": (55275, 3.139), "q_fifo": (54794, 3.235),
            "qfifo_arb": (54655, 3.269),
        }
        for arm, (iops, cpu) in anchors.items():
            self.assertAlmostEqual(arms[arm]["iops_median"], iops, delta=1, msg=arm)
            self.assertAlmostEqual(arms[arm]["cpu_median"], cpu, delta=0.001, msg=arm)
            self.assertTrue(46.0 < arms[arm]["clat_p99_min"] <=
                            arms[arm]["clat_p99_max"] < 48.0, arm)

    def test_e4b_n_mapping_and_flatness(self):
        e4b = renderer.e4b_aggregates(self.data)
        for n in renderer.E4B_N:
            for stack in ("native", "q_noop"):
                rows = e4b[(n, stack)]
                self.assertEqual(len(rows), 5)
                iops = renderer.median([r["iops"] for r in rows])
                if stack == "native":
                    self.assertTrue(54300 < iops < 55700, (n, stack, iops))
                else:
                    self.assertTrue(50700 < iops < 51200, (n, stack, iops))
                p99s = [r["clat_p99_us"] for r in rows]
                self.assertTrue(53.0 < renderer.median(p99s) < 55.0, (n, stack))


if __name__ == "__main__":
    unittest.main()
