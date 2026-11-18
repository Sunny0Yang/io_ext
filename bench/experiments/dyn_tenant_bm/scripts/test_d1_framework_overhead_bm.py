#!/usr/bin/env python3
"""Unit/provenance checks for D1 framework-only reporting."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analyzer = load_module("d1_analyzer", "d1_analyze_bm.py")
renderer = load_module("d1_renderer", "render_d1_framework_overhead_bm.py")


class D1Checks(unittest.TestCase):
    def test_nanosecond_thresholds_strictly_greater(self):
        values = [0, 500_000, 500_001, 1_000_000, 1_000_001,
                  2_000_000, 2_000_001, 4_000_000, 4_000_001]
        stats = analyzer.summarize(values)
        for field, count in (("frac_gt500us", 7), ("frac_gt1ms", 5),
                             ("frac_gt2ms", 3), ("frac_gt4ms", 1)):
            self.assertAlmostEqual(stats[field], count / len(values))

    def test_log_units_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_text("0, 1000, 0\n1, 1000001, 0\n")
            values = analyzer.parse_latlog(path)
            self.assertEqual(values, [1000.0, 1000001.0])
            self.assertEqual(analyzer.summarize(values)["frac_gt1ms"], 0.5)

    def test_nearest_rank_percentile_boundaries(self):
        values = list(range(1, 101))
        for percentile, expected in ((0, 1), (50, 50), (99, 99), (100, 100)):
            self.assertEqual(analyzer.pct(values, percentile), expected)

    def test_multiple_log_files_are_sorted_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, values in ((1, range(101, 201)), (2, range(1, 101))):
                path = root / f"ta_slat.{number}.log"
                path.write_text("".join(f"0, {value}, 0\n" for value in values))
            row = analyzer.analyze_dir(directory)
            self.assertEqual(row["slat_n"], 200)
            self.assertEqual(row["slat_p99"], 198)
            self.assertEqual(row["slat_mx"], 200)

    def test_real_data_selection_and_aggregation(self):
        layers, horizontal, jobs, used, source, files = renderer.load_data(
            renderer.BASE / "results/D1_overhead_bm_data")
        self.assertEqual(len(source), 73)
        self.assertEqual(len(used), 65)
        self.assertEqual(len(jobs), 60)
        self.assertEqual(len(layers), 5)
        self.assertEqual([r["config"] for r in layers[:4]],
                         [r[0] for r in renderer.A_CONFIGS])
        for row in used:
            self.assertNotIn("arbfb", row["config"])
            self.assertFalse(row["config"].startswith("C"))
        for row in horizontal:
            matching = [job for job in jobs
                        if job["stack"] == row["stack"]
                        and job["active_cgroups"] == row["active_cgroups"]]
            self.assertEqual(len(matching), row["active_cgroups"])
            self.assertAlmostEqual(sum(job["iops"] for job in matching), row["aggregate_iops"])
        sixteen = next(row for row in horizontal
                       if row["active_cgroups"] == 16 and row["stack"] == "qfifo")
        self.assertAlmostEqual(sixteen["clat_job_p99_max_us"], 148.480)
        self.assertTrue(73 < sixteen["iops_retained_percent"] < 74)
        for row in layers:
            self.assertAlmostEqual(row["slat_mean_us"] + row["clat_mean_us"],
                                   row["lat_mean_us"], places=5)
            for row_b in horizontal:
                for metric in ("slat", "clat"):
                    mean = row_b[f"{metric}_job_p99_mean_us"]
                    self.assertTrue(row_b[f"{metric}_job_p99_min_us"] <= mean
                                    <= row_b[f"{metric}_job_p99_max_us"])
        self.assertEqual(layers[0]["dlat_mean_us"], 0.0)
        self.assertIsNone(layers[4]["dlat_mean_us"])
        self.assertAlmostEqual(layers[1]["dlat_mean_us"], -0.003, places=3)
        self.assertAlmostEqual(layers[2]["dlat_mean_us"], 0.037, places=3)
        self.assertAlmostEqual(layers[3]["dlat_mean_us"], 1.417, places=3)
        stack_total = layers[0]["lat_mean_us"] + sum(
            layer["dlat_mean_us"] for layer in layers[1:4])
        self.assertAlmostEqual(stack_total, layers[3]["lat_mean_us"], places=9)


if __name__ == "__main__":
    unittest.main()
