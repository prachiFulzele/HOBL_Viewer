import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
import json_data
from regression import build_options, compare_builds, metric_options


def column(build, values, *, ram="16", scenario="work", device="TestDevice", unit="W"):
    return {
        "Device": device, "Ram": ram, "Scenario": scenario,
        "Header": {"OS Build": build},
        "PowerCalculation": {"system_power": {"value": values[0], "unit": unit}} if values else {},
        "PerfAll": [
            {"name": "launch", "pt": 123, "unit": "ms", "value": value}
            for value in values
        ],
    }


def metric(columns, source="PowerCalculation"):
    return next(option["value"] for option in metric_options(columns) if source in option["value"])


class BuildRegressionTests(unittest.TestCase):
    def test_numeric_build_order_and_exact_p50_changes(self):
        columns = [
            column("10.0.100.10", [30]), column("10.0.100.2", [10]),
            column("10.0.100.2", [14]), column("10.0.100.3", [9]),
        ]
        rows = compare_builds(columns, metric(columns), "lower")["rows"]
        self.assertEqual([row["build"] for row in rows], [
            "10.0.100.2", "10.0.100.3", "10.0.100.10",
        ])
        self.assertEqual(rows[0]["p50"], 12)
        self.assertEqual(rows[0]["samples"], 2)
        self.assertEqual(rows[0]["iterations"], 2)
        self.assertEqual(rows[0]["status"], "Baseline")
        self.assertIsNone(rows[0]["delta"])
        self.assertEqual(rows[1]["previous_build"], "10.0.100.2")
        self.assertEqual(rows[1]["delta"], -3)
        self.assertEqual(rows[1]["percent"], -25)
        self.assertEqual(rows[1]["status"], "Improvement")
        self.assertEqual(rows[2]["delta"], 21)
        self.assertAlmostEqual(rows[2]["percent"], 233.33333333333334)
        self.assertEqual(rows[2]["status"], "Regression")

    def test_build_options_and_exclusion_recalculate_previous_build(self):
        columns = [
            column("10", [10]), column("2", [20]), column("3", [30]),
        ]
        self.assertEqual(build_options(columns), ["2", "3", "10"])

        rows = compare_builds(
            columns, metric(columns), "lower", included_builds=["2", "10"],
        )["rows"]
        self.assertEqual([row["build"] for row in rows], ["2", "10"])
        self.assertEqual(rows[1]["previous_build"], "2")
        self.assertEqual(rows[1]["delta"], -10)
        self.assertEqual(rows[1]["status"], "Improvement")

    def test_higher_is_better_and_unchanged(self):
        columns = [column("1", [10]), column("2", [12]), column("3", [12]), column("4", [9])]
        rows = compare_builds(columns, metric(columns), "higher")["rows"]
        self.assertEqual([row["status"] for row in rows],
                         ["Baseline", "Improvement", "Unchanged", "Regression"])
        self.assertEqual(rows[1]["percent"], 20)

    def test_zero_and_negative_baselines(self):
        columns = [column("1", [0]), column("2", [-2]), column("3", [-1])]
        rows = compare_builds(columns, metric(columns), "lower")["rows"]
        self.assertIsNone(rows[1]["percent"])
        self.assertEqual(rows[1]["delta"], -2)
        self.assertEqual(rows[2]["percent"], 50)

    def test_missing_metric_does_not_skip_the_previous_build(self):
        columns = [column("1", [10]), column("2", []), column("3", [15])]
        rows = compare_builds(columns, metric(columns), "lower")["rows"]
        self.assertEqual(rows[1]["status"], "No samples")
        self.assertEqual(rows[2]["previous_build"], "2")
        self.assertEqual(rows[2]["status"], "Not comparable")
        self.assertIsNone(rows[2]["delta"])

    def test_perf_p50_pools_samples_not_iteration_medians(self):
        columns = [column("1", [1, 2, 3]), column("1", [100])]
        row = compare_builds(columns, metric(columns, "PerfMetrics"), "lower")["rows"][0]
        self.assertEqual(row["p50"], 2.5)
        self.assertEqual(row["samples"], 4)
        self.assertEqual(row["iterations"], 2)

    def test_invalid_samples_and_missing_builds_are_counted(self):
        columns = [column("", [20]), column(None, [20]), column("1", [float("nan")]),
                   column("1", [float("inf")]), column("1", [True]), column("1", ["20"]),
                   column("1", [10])]
        result = compare_builds(columns, metric(columns), "lower")
        self.assertEqual(result["missing_build_iterations"], 2)
        self.assertEqual(result["invalid_samples"], 4)
        self.assertEqual(result["rows"][0]["p50"], 10)
        self.assertEqual(result["rows"][0]["samples"], 1)

    def test_catalog_separates_type_unit_and_perftrack_id(self):
        columns = [column("1", [1]), column("2", [1000], unit="mW")]
        columns[0]["PowerMetrics"] = {"system_power": {"value": 2, "unit": "W"}}
        columns[0]["PerfAll"].append({"name": "launch", "pt": 456, "unit": "ms", "value": 20})
        options = metric_options(columns)
        self.assertEqual(len(options), 5)
        self.assertEqual(len({option["value"] for option in options}), 5)
        power_watts = next(option["value"] for option in options
                           if option["label"] == "PowerCalculation / system_power (W)")
        rows = compare_builds(columns, power_watts, "lower")["rows"]
        self.assertIsNone(rows[1]["p50"])

    def test_invalid_metric_and_direction_raise(self):
        columns = [column("1", [1])]
        with self.assertRaises(ValueError):
            compare_builds(columns, "unknown", "lower")
        with self.assertRaises(ValueError):
            compare_builds(columns, metric(columns), "invalid")

    def test_iteration_filter_keeps_eight_and_recalculates_remaining_builds(self):
        columns = (
            [column("1", [10]) for _ in range(8)]
            + [column("2", [50]) for _ in range(7)]
            + [column("3", [15]) for _ in range(9)]
        )
        selected = metric(columns)
        result = compare_builds(columns, selected, "lower", exclude_under_8=True)
        self.assertEqual(result["excluded_builds"], 1)
        self.assertEqual([row["build"] for row in result["rows"]], ["1", "3"])
        self.assertEqual(result["rows"][0]["iterations"], 8)
        self.assertEqual(result["rows"][0]["status"], "Baseline")
        self.assertEqual(result["rows"][1]["previous_build"], "1")
        self.assertEqual(result["rows"][1]["delta"], 5)
        self.assertEqual(result["rows"][1]["percent"], 50)
        self.assertEqual(result["rows"][1]["status"], "Regression")
        unfiltered = compare_builds(columns, selected, "lower")
        self.assertEqual(unfiltered["excluded_builds"], 0)
        self.assertEqual([row["build"] for row in unfiltered["rows"]], ["1", "2", "3"])
        self.assertEqual(unfiltered["rows"][2]["previous_build"], "2")

    def test_iteration_filter_counts_valid_iterations_not_perf_samples(self):
        columns = [column("1", list(range(20)))] + [column("2", [10]) for _ in range(7)]
        columns += [column("2", [float("nan")]), column("2", [])]
        result = compare_builds(columns, metric(columns, "PerfMetrics"), "lower", exclude_under_8=True)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["excluded_builds"], 2)

    def test_iteration_filter_removes_initial_build_and_resets_baseline(self):
        columns = [column("1", [10])] + [column("2", [20]) for _ in range(8)]
        result = compare_builds(columns, metric(columns), "lower", exclude_under_8=True)
        self.assertEqual(result["rows"][0]["build"], "2")
        self.assertEqual(result["rows"][0]["status"], "Baseline")
        self.assertIsNone(result["rows"][0]["previous_build"])
        self.assertIsNone(result["rows"][0]["delta"])


class RegressionRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_page_and_navigation(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<option value="regression">Build Regression</option>', response.data)
        self.assertIn(b'id="regressionView" hidden', response.data)
        self.assertIn(b'js/regression.js', response.data)
        self.assertNotIn(b'href="/regression"', response.data)
        self.assertEqual(self.client.get("/regression").status_code, 404)

    def test_cascading_options_use_the_active_backend(self):
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = [column("1", [10])]
            response = self.client.get("/api/regression", query_string={"device": "TestDevice", "ram": "16"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["rams"], ["16"])
            self.assertEqual(response.json["scenarios"], ["work"])
            backend.return_value.get_table_data.assert_called_once_with(
                device="TestDevice", ram="16", scenario="",
            )

    def test_analysis_shape_and_fixed_filters(self):
        columns = [column("1", [10]), column("2", [12])]
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = columns
            response = self.client.get("/api/regression", query_string={
                "device": "TestDevice", "ram": "16", "scenario": "work",
                "metric": metric(columns), "direction": "lower",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["rows"][1], {
                "build": "2", "previous_build": "1", "p50": 12, "samples": 1,
                "iterations": 1, "delta": 2, "percent": 20, "status": "Regression",
            })
            backend.return_value.get_table_data.assert_called_once_with(
                device="TestDevice", ram="16", scenario="work",
            )

    def test_multiple_metrics_return_separate_comparisons(self):
        columns = [column("1", [10]), column("2", [12])]
        selected = [option["value"] for option in metric_options(columns)]
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = columns
            response = self.client.get("/api/regression", query_string=[
                ("device", "TestDevice"), ("ram", "16"), ("scenario", "work"),
                ("metric", selected[0]), ("metric", selected[1]), ("direction", "lower"),
            ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["comparisons"]), 2)
        self.assertEqual(
            [comparison["metric"]["value"] for comparison in response.json["comparisons"]],
            selected,
        )
        self.assertTrue(all(len(comparison["rows"]) == 2
                            for comparison in response.json["comparisons"]))

    def test_selected_builds_are_applied_to_every_metric(self):
        columns = [column("1", [10]), column("2", [12]), column("3", [15])]
        selected_metrics = [option["value"] for option in metric_options(columns)]
        query = [
            ("device", "TestDevice"), ("ram", "16"), ("scenario", "work"),
            ("direction", "lower"), ("build", "1"), ("build", "3"),
        ]
        query.extend(("metric", value) for value in selected_metrics)
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = columns
            response = self.client.get("/api/regression", query_string=query)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["builds"], ["1", "2", "3"])
        self.assertTrue(all(
            [row["build"] for row in comparison["rows"]] == ["1", "3"]
            for comparison in response.json["comparisons"]
        ))
        self.assertTrue(all(
            comparison["rows"][1]["previous_build"] == "1"
            for comparison in response.json["comparisons"]
        ))

    def test_missing_and_repeated_selections_are_rejected(self):
        for query in [
            {}, {"device": ["A", "B"]}, {"device": "A", "ram": ["8", "16"]},
            {"device": "A", "scenario": ["a", "b"]},
            {"device": "A", "metric": "x"},
            {"device": "A", "direction": ["lower", "higher"]},
            {"device": "A", "exclude_under_8": ["0", "1"]},
            {"device": "A", "exclude_under_8": "yes"},
        ]:
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/regression", query_string=query).status_code, 400)

    def test_iteration_filter_is_regression_only_and_optional(self):
        columns = [column("1", [10]) for _ in range(7)] + [column("2", [20]) for _ in range(8)]
        query = {
            "device": "TestDevice", "ram": "16", "scenario": "work",
            "metric": metric(columns), "direction": "lower",
        }
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = columns
            backend.return_value.get_metrics.return_value = [{"BuildNumber": "1"}, {"BuildNumber": "2"}]
            filtered = self.client.get("/api/regression", query_string={**query, "exclude_under_8": "1"})
            self.assertEqual(filtered.status_code, 200)
            self.assertEqual([row["build"] for row in filtered.json["rows"]], ["2"])
            self.assertEqual(filtered.json["excluded_builds"], 1)
            for value in (None, "0"):
                with self.subTest(exclude_under_8=value):
                    params = query if value is None else {**query, "exclude_under_8": value}
                    unfiltered = self.client.get("/api/regression", query_string=params)
                    self.assertEqual(len(unfiltered.json["rows"]), 2)
                    self.assertEqual(unfiltered.json["excluded_builds"], 0)
            table = self.client.get("/api/table", query_string={**query, "exclude_under_8": "1"})
            self.assertEqual(table.status_code, 200)
            self.assertEqual(len(table.json), 15)
            metrics = self.client.get("/api/metrics", query_string={**query, "exclude_under_8": "1"})
            self.assertEqual(metrics.status_code, 200)
            self.assertEqual(len(metrics.json), 2)

    def test_backend_errors_are_visible(self):
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.side_effect = RuntimeError("Source unavailable")
            response = self.client.get("/api/regression?device=TestDevice")
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json["error"], "Source unavailable")

    def test_empty_options_and_invalid_metric(self):
        with patch("app.get_backend") as backend:
            backend.return_value.get_table_data.return_value = []
            response = self.client.get("/api/regression?device=TestDevice")
            self.assertEqual(
                response.json,
                {"builds": [], "rams": [], "scenarios": [], "metrics": []},
            )
            response = self.client.get("/api/regression", query_string={
                "device": "TestDevice", "ram": "16", "scenario": "work",
                "metric": "missing", "direction": "lower",
            })
            self.assertEqual(response.status_code, 400)


class JsonRegressionIntegrationTests(unittest.TestCase):
    def test_uploaded_json_shape_preserves_builds_and_fixed_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            for index, (build, ram, power) in enumerate([
                ("100.2", 16, 10), ("100.2", 16, 14), ("100.10", 16, 15),
                ("100.10", 8, 99),
            ]):
                result = {
                    "run_info": {"scenario": "web", "run_number": index + 1},
                    "device_config": {
                        "device_name": "RegressionTestDevice", "usable_ram_config_gb": ram,
                        "os_build": build,
                    },
                    "power_calculation": [{"name": "system_power", "unit": "W", "value": power}],
                }
                Path(folder, f"run_{index}.json").write_text(json.dumps(result), encoding="utf-8")
            with patch("json_data._json_dir", return_value=folder):
                columns = json_data.get_table_data(
                    device="RegressionTestDevice", ram="16", scenario="web",
                )
            self.assertEqual(len(columns), 3)
            rows = compare_builds(columns, metric(columns), "lower")["rows"]
            self.assertEqual([row["build"] for row in rows], ["100.2", "100.10"])
            self.assertEqual(rows[0]["p50"], 12)
            self.assertEqual(rows[1]["delta"], 3)
            self.assertEqual(rows[1]["percent"], 25)
            self.assertEqual(rows[1]["status"], "Regression")


if __name__ == "__main__":
    unittest.main()
