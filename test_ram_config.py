import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import json_data
import kusto_data
from ram_config import RAM_OVERRIDES, RAM_REPAIR_BUILD, repair_json_ram


def result(device="OtherDevice", iteration=1, ram="", build=RAM_REPAIR_BUILD):
    return {
        "device_config": {
            "device_name": device, "os_build": build, "usable_ram_config_gb": ram,
        },
        "run_info": {"run_number": iteration, "scenario": "web"},
        "power_metrics": [{"name": "rail", "value": 1, "unit": "W"}],
    }


class RamRepairTests(unittest.TestCase):
    def test_explicit_mappings_fill_only_missing_target_build_values(self):
        for device, expected in RAM_OVERRIDES.items():
            with self.subTest(device=device):
                documents = [result(device), result(device, ram="64"),
                             result(device, build="26707.1000")]
                repair_json_ram(documents)
                self.assertEqual(documents[0]["device_config"]["usable_ram_config_gb"], expected)
                self.assertEqual(documents[1]["device_config"]["usable_ram_config_gb"], "64")
                self.assertEqual(documents[2]["device_config"]["usable_ram_config_gb"], "")

    def test_aliases_use_the_canonical_device_mapping(self):
        document = result("Purwa_DC", ram=None)
        repair_json_ram([document])
        self.assertEqual(document["device_config"]["usable_ram_config_gb"], "16")

    def test_historical_mode_is_per_device_build_and_strictly_earlier(self):
        documents = [
            result(iteration=1, ram="8"), result(iteration=2, ram="8"),
            result(iteration=3, ram="16"), result(iteration=4),
            result(iteration=4, ram="32"), result(iteration=5, ram="32"),
        ]
        documents += [result("DifferentDevice", iteration=1, ram="64") for _ in range(10)]
        documents += [result(iteration=1, ram="64", build="26707.1000") for _ in range(10)]
        repair_json_ram(list(reversed(documents)))
        self.assertEqual(documents[3]["device_config"]["usable_ram_config_gb"], "8")

    def test_equal_counts_use_most_recent_observation(self):
        documents = [result(iteration=1, ram="8"), result(iteration=2, ram="16"),
                     result(iteration=3)]
        repair_json_ram(documents)
        self.assertEqual(documents[2]["device_config"]["usable_ram_config_gb"], "16")

    def test_imputed_values_never_become_history(self):
        documents = [result(iteration=1, ram="8"), result(iteration=2),
                     result(iteration=3), result(iteration=4, ram="16"),
                     result(iteration=5)]
        repair_json_ram(documents)
        self.assertEqual(documents[4]["device_config"]["usable_ram_config_gb"], "16")

    def test_no_history_or_no_order_stays_missing(self):
        documents = [result(iteration=1), result(iteration=None),
                     result(iteration=3, ram="8")]
        repair_json_ram(documents)
        self.assertEqual(documents[0]["device_config"]["usable_ram_config_gb"], "")
        self.assertEqual(documents[1]["device_config"]["usable_ram_config_gb"], "")

    def test_json_filters_metrics_and_headers_share_repaired_ram(self):
        documents = [result("ROM-MS-IDC-25"), result(iteration=1, ram="8"),
                     result(iteration=2)]
        with tempfile.TemporaryDirectory() as folder:
            for index, document in enumerate(documents):
                Path(folder, f"run_{index}.json").write_text(json.dumps(document), encoding="utf-8")
            with patch("json_data._json_dir", return_value=folder):
                self.assertEqual(json_data.get_filter_options()["rams"], ["8", "16"])
                columns = json_data.get_table_data(device="ROM-MS-IDC-25", ram="16")
                self.assertEqual(len(columns), 1)
                self.assertEqual(columns[0]["Header"]["Usable RAM"], "16")
                self.assertEqual(columns[0]["Ram"], "16")
                self.assertEqual(len(json_data.get_metrics(device="ROM-MS-IDC-25", ram="16")), 1)
                self.assertEqual(len(json_data.get_table_data(device="OtherDevice", ram="8")), 2)
            original = json.loads(Path(folder, "run_0.json").read_text(encoding="utf-8"))
            self.assertEqual(original["device_config"]["usable_ram_config_gb"], "")

    def test_kusto_repair_precedes_filters_and_counts_results_not_metric_rows(self):
        query = kusto_data._joined(["| where Ram == '16'"])
        self.assertLess(query.index("let _ram_resolved"), query.index("| where Ram == '16'"))
        self.assertIn("| join kind=inner (_ram_resolved) on TRID", query)
        self.assertIn("min(coalesce(RunDate, BuildDate)) by TRID", query)
        self.assertIn("| where _HistoryTime < _RamTime", query)
        self.assertIn("_RamCount desc, _RamLatest desc", query)
        for device, ram in RAM_OVERRIDES.items():
            self.assertIn(f"_RamDevice == '{device}', '{ram}'", query)


if __name__ == "__main__":
    unittest.main()
