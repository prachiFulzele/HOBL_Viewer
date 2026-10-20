import unittest
from unittest.mock import patch

import kusto_data


NAMES = [
    "Perftrack_R1_Reddit_Edge_Navigation_To_First_Paint__Stable__1",
    "Perftrack_Reddit_Edge_Navigation_To_First_Paint__Stable__1",
    "Perftrack_Reddit_Edge_Navigation_To_First_Paint__Stable__2",
    "Perftrack_UnknownWorkload_Unmapped_Metric__1",
]


class AllPerformanceMetricsTests(unittest.TestCase):
    def test_original_names_and_values_survive_shared_pt_mapping(self):
        rows = [
            {
                "TRID": "result-1", "Device": "TestDevice", "Ram": "16",
                "Scenario": "web", "Date": "2026-10-19", "Iteration": 1,
                "RunTs": "2026-10-19 10:00:00", "M_OSBuild": "26708.1000",
                "Name": name, "Value": value, "Unit": "ms", "MetricType": "PerfMetrics",
            }
            for value, name in enumerate(NAMES, 10)
        ]
        with patch("kusto_data._run_query", return_value=rows):
            columns = kusto_data.get_table_data()
        self.assertEqual(len(columns), 1)
        entries = columns[0]["PerfAll"]
        self.assertEqual(len(entries), len(rows))
        self.assertEqual([entry["raw_name"] for entry in entries], NAMES)
        self.assertEqual([entry["value"] for entry in entries], [10, 11, 12, 13])
        self.assertEqual([entry["pt"] for entry in entries], [9412, 9412, 9412, None])
        self.assertEqual(columns[0]["PerfByPt"][9412], [10, 11, 12])
        self.assertEqual(columns[0]["PerfByName"][NAMES[-1]], 13)

    def test_legacy_names_are_preserved_without_changing_curated_samples(self):
        name = "Edge_Navigation_To_First_Paint__Stable__9412_1"
        with patch("kusto_data._run_query", return_value=[{
            "TRID": "result-1", "Name": name, "Value": 22, "Unit": "ms",
            "MetricType": "PerfMetrics",
        }]):
            column = kusto_data.get_table_data()[0]
        self.assertEqual(column["PerfAll"][0]["raw_name"], name)
        self.assertEqual(column["PerfAll"][0]["name"], "Edge_Navigation_To_First_Paint__Stable")
        self.assertEqual(column["PerfByPt"][9412], [22])


if __name__ == "__main__":
    unittest.main()
