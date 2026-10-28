import unittest
from unittest.mock import patch

import kusto_data
from app import app


def run(device, scenario, iterations, build="build-1", first="2026-10-20", last="2026-10-21"):
    return {
        "Device": device, "Scenario": scenario, "Iterations": iterations,
        "Build": build, "FirstUpload": first, "LastUpload": last,
    }


class ReviewSummaryTests(unittest.TestCase):
    def summary(self, rows, build=""):
        with patch("kusto_data._run_query", return_value=rows) as query:
            result = kusto_data.get_review_summary("2026-10-16", "2026-10-22", build)
        kql = query.call_args.args[0]
        self.assertIn("by TRID;", kql)
        self.assertIn("by Device, Scenario, M_OSBuild", kql)
        self.assertIn("_dt < datetime(2026-10-22) + 1d", kql)
        return result

    def test_targets_at_below_and_above_threshold(self):
        for scenario, target in [
            ("enterprise_collab", 20), ("consumer_multitasker", 20), ("youtube", 10),
        ]:
            for count in [0, target - 1, target, target + 4]:
                with self.subTest(scenario=scenario, count=count):
                    result = self.summary([run("A", scenario, count)])
                    row = result["scenarios"][0]
                    self.assertEqual(row["target"], target)
                    self.assertEqual(row["remaining"], max(0, target - count))
                    self.assertEqual(result["remaining_iterations"], row["remaining"])
                    self.assertEqual(len(result["remaining"]), int(count < target))

    def test_missing_device_scenario_combinations_are_included(self):
        result = self.summary([
            run("A", "enterprise_collab", 22),
            run("B", "youtube", 8),
        ])
        self.assertEqual(result["total_devices"], 2)
        self.assertEqual(result["total_scenarios"], 2)
        self.assertEqual(result["total_iterations"], 30)
        self.assertEqual(result["remaining_iterations"], 32)
        missing = next(row for row in result["scenarios"]
                       if row["device"] == "B" and row["scenario"] == "enterprise_collab")
        self.assertEqual(missing["iterations"], 0)
        self.assertEqual(missing["first_upload"], "")
        self.assertEqual(missing["remaining"], 20)

    def test_build_filter_preserves_date_range_checklist_and_build_options(self):
        result = self.summary([
            run("A", "enterprise_collab", 8),
            run("B", "youtube", 12, "build-2"),
        ], "build-1")
        self.assertEqual(result["builds"], ["build-1", "build-2"])
        self.assertEqual(result["build"], "build-1")
        self.assertEqual(result["total_iterations"], 8)
        self.assertEqual(len(result["scenarios"]), 4)
        self.assertEqual(result["remaining_iterations"], 52)

    def test_all_builds_combine_counts_and_upload_dates(self):
        result = self.summary([
            run("A", "youtube", 6, first="2026-10-20", last="2026-10-22"),
            run("A", "youtube", 5, "build-2", first="2026-10-16", last="2026-10-19"),
        ])
        row = result["scenarios"][0]
        self.assertEqual(row["iterations"], 11)
        self.assertEqual(row["first_upload"], "2026-10-16")
        self.assertEqual(row["last_upload"], "2026-10-22")
        self.assertEqual(result["remaining"], [])

    def test_no_data(self):
        result = self.summary([])
        self.assertEqual(result["scenarios"], [])
        self.assertEqual(result["remaining_iterations"], 0)
        self.assertEqual(result["builds"], [])
        self.assertEqual(result["total_run_hours"], 0)
        self.assertEqual(result["missing_record_time"], 0)

    def test_record_time_hours_and_coverage_across_builds(self):
        first = {**run("A", "youtube", 3), "RecordedMinutes": 90, "RecordedIterations": 2}
        second = {
            **run("A", "youtube", 2, "build-2"),
            "RecordedMinutes": 45, "RecordedIterations": 2,
        }
        result = self.summary([first, second])
        self.assertEqual(result["total_run_hours"], 2.25)
        self.assertEqual(result["scenarios"][0]["run_hours"], 2.25)
        self.assertEqual(result["missing_record_time"], 1)
        filtered = self.summary([first, second], "build-2")
        self.assertEqual(filtered["total_run_hours"], 0.75)
        self.assertEqual(filtered["missing_record_time"], 0)

    def test_missing_record_time_does_not_mean_zero_duration_coverage(self):
        result = self.summary([run("A", "youtube", 4), run("B", "enterprise_collab", 2)])
        self.assertEqual(result["total_run_hours"], 0)
        self.assertEqual(result["missing_record_time"], 6)
        zero_run = next(row for row in result["scenarios"]
                        if row["device"] == "B" and row["scenario"] == "youtube")
        self.assertEqual(zero_run["run_hours"], 0)
        self.assertEqual(zero_run["missing_record_time"], 0)

    def test_record_time_query_deduplicates_and_validates_minutes(self):
        with patch("kusto_data._run_query", return_value=[]) as query:
            kusto_data.get_review_summary("2026-10-16", "2026-10-22")
        kql = query.call_args.args[0]
        self.assertIn("_recordMinutes=maxif(todouble(Value)", kql)
        self.assertIn("Name =~ 'record_time'", kql)
        self.assertIn("Unit =~ 'min'", kql)
        self.assertIn("isfinite(todouble(Value)) and todouble(Value) >= 0", kql)
        self.assertIn("RecordedMinutes=sum(_recordMinutes)", kql)
        self.assertIn("RecordedIterations=countif(isnotnull(_recordMinutes))", kql)

    def test_invalid_build_and_dates(self):
        with patch("kusto_data._run_query", return_value=[run("A", "youtube", 1)]):
            with self.assertRaisesRegex(ValueError, "Select a build"):
                kusto_data.get_review_summary("2026-10-16", "2026-10-22", "invalid")
        with patch("kusto_data._run_query") as query:
            for start, end in [("", ""), ("bad", "2026-10-22"), ("2026-10-22", "2026-10-16")]:
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    kusto_data.get_review_summary(start, end)
            query.assert_not_called()


class ReviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_page_has_build_filter(self):
        response = self.client.get("/review")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="reviewBuild"', response.data)

    def test_build_is_forwarded_to_kusto_independently_of_active_source(self):
        with patch("app.kusto_data") as backend, patch("app.get_backend") as active:
            backend.get_review_summary.return_value = {"scenarios": []}
            response = self.client.get("/api/review-summary", query_string={
                "start_date": "2026-10-16", "end_date": "2026-10-22", "build": "build-1",
            })
            self.assertEqual(response.status_code, 200)
            backend.get_review_summary.assert_called_once_with(
                "2026-10-16", "2026-10-22", "build-1"
            )
            active.assert_not_called()

    def test_error_responses(self):
        with patch("app.kusto_data", None):
            self.assertEqual(self.client.get("/api/review-summary").status_code, 503)
        for error, status in [(ValueError("Bad filter"), 400), (RuntimeError("Kusto failed"), 500)]:
            with patch("app.kusto_data") as backend:
                backend.get_review_summary.side_effect = error
                response = self.client.get("/api/review-summary")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json["error"], str(error))


if __name__ == "__main__":
    unittest.main()
