import unittest

from power_calculations import (
    apply_battery_life,
    apply_heracles_component_power,
    apply_heracles_power,
    apply_minos_power,
)


class HeraclesPowerTests(unittest.TestCase):
    def test_applies_heracles_soc_and_system_formulas(self):
        calculations = {
            "memory_power": {"value": 2.0, "unit": "W"},
            "soc_power": {"value": 0, "unit": "W"},
            "system_power": {"value": 0, "unit": "W"},
            "display_power": {"value": 3.0, "unit": "W"},
        }
        rails = {
            "vsys_core": {"value": 10.0, "unit": "W"},
            "vsys_rop": {"value": 1.5, "unit": "W"},
        }

        result = apply_heracles_power(calculations, rails, "Heracles")

        self.assertEqual(result, (True, True))
        self.assertAlmostEqual(calculations["soc_power"]["value"], 7.4)
        self.assertEqual(calculations["system_power"]["value"], 11.5)
        self.assertEqual(calculations["display_power"]["value"], 3.0)

    def test_does_not_change_other_devices(self):
        calculations = {
            "memory_power": {"value": 2.0, "unit": "W"},
            "soc_power": {"value": 4.0, "unit": "W"},
            "system_power": {"value": 8.0, "unit": "W"},
        }
        original = {name: entry.copy() for name, entry in calculations.items()}
        rails = {
            "vsys_core": {"value": 10.0, "unit": "W"},
            "vsys_rop": {"value": 1.5, "unit": "W"},
        }

        result = apply_heracles_power(calculations, rails, "Cadmus")

        self.assertEqual(result, (False, False))
        self.assertEqual(calculations, original)

    def test_calculates_each_metric_only_when_its_inputs_are_available(self):
        calculations = {
            "soc_power": {"value": 4.0, "unit": "W"},
            "system_power": {"value": 8.0, "unit": "W"},
        }
        rails = {
            "vsys_core": {"value": 10.0, "unit": "W"},
            "vsys_rop": {"value": 1.5, "unit": "W"},
        }

        result = apply_heracles_power(calculations, rails, "HERACLES prototype")

        self.assertEqual(result, (False, True))
        self.assertEqual(calculations["soc_power"]["value"], 4.0)
        self.assertEqual(calculations["system_power"]["value"], 11.5)

    def test_builds_all_summary_metrics_for_qc_hes_rot_device(self):
        calculations = {}
        rails = {
            "pm_emi_vsys_core": {"value": 10.0, "unit": "W"},
            "pm_emi_vsys_rop": {"value": 1.5, "unit": "W"},
            "pm_emi_memory": {"value": 2.0, "unit": "W"},
            "pm_emi_3p3v_panel_in": {"value": 0.4, "unit": "W"},
            "pm_emi_bklt_in": {"value": 0.6, "unit": "W"},
        }

        corrected = apply_heracles_component_power(
            calculations, rails, "QC-HES-ROT-11"
        )
        result = apply_heracles_power(calculations, rails, "QC-HES-ROT-11")

        self.assertEqual(corrected, {"memory_power", "display_power"})
        self.assertEqual(result, (True, True))
        self.assertEqual(calculations["memory_power"]["value"], 2.0)
        self.assertEqual(calculations["display_power"]["value"], 1.0)
        self.assertAlmostEqual(calculations["soc_power"]["value"], 7.4)
        self.assertEqual(calculations["system_power"]["value"], 11.5)


class MinosPowerTests(unittest.TestCase):
    def test_builds_power_calculations_from_minos_rails(self):
        calculations = {}
        rails = {
            "pm_emi_sys": {"value": 20.0, "unit": "W"},
            "PMIC_MT6363": {"value": 1.0, "unit": "W"},
            "PMIC_MT6373": {"value": 2.0, "unit": "W"},
            "CPU_P": {"value": 3.0, "unit": "W"},
            "CPU_E": {"value": 4.0, "unit": "W"},
            "pm_emi_gpu_misc": {"value": 5.0, "unit": "W"},
            "pm_emi_gpu_compute": {"value": 6.0, "unit": "W"},
            "pm_emi_cpu_core": {"value": 7.0, "unit": "W"},
            "pm_emi_npu": {"value": 8.0, "unit": "W"},
            "pm_emi_memory_0": {"value": 1.1, "unit": "W"},
            "pm_emi_memory_1": {"value": 1.2, "unit": "W"},
            "pm_emi_display_panel": {"value": 0.4, "unit": "W"},
            "pm_emi_display_backlight": {"value": 0.6, "unit": "W"},
            "pm_emi_cpu_0": {"value": 2.1, "unit": "W"},
            "pm_emi_cpu_1": {"value": 2.2, "unit": "W"},
            "pm_emi_npu_0": {"value": 0.7, "unit": "W"},
            "pm_emi_gpu_0": {"value": 1.3, "unit": "W"},
            "pm_emi_storage_0": {"value": 0.2, "unit": "W"},
            "pm_emi_wifi_0": {"value": 0.3, "unit": "W"},
            "pm_emi_camera_0": {"value": 0.5, "unit": "W"},
            "pm_emi_keyboard_0": {"value": 0.1, "unit": "W"},
            "pm_emi_sam_0": {"value": 0.8, "unit": "W"},
            "pm_emi_touch_0": {"value": 0.9, "unit": "W"},
        }

        corrected = apply_minos_power(calculations, rails, "NV-DIO-MIN-09")

        self.assertEqual(corrected, set(calculations))
        self.assertEqual(calculations["total_power"]["value"], 20.0)
        self.assertEqual(calculations["system_power"]["value"], 20.0)
        self.assertAlmostEqual(calculations["soc_power"]["value"], 30.6)
        self.assertAlmostEqual(calculations["memory_power"]["value"], 2.3)
        self.assertEqual(calculations["display_power"]["value"], 1.0)
        self.assertAlmostEqual(calculations["cpu_power"]["value"], 11.3)
        self.assertEqual(calculations["npu_power"]["value"], 8.7)
        self.assertEqual(calculations["gpu_power"]["value"], 12.3)
        self.assertEqual(calculations["storage_power"]["value"], 0.2)
        self.assertEqual(calculations["wifi_power"]["value"], 0.3)
        self.assertEqual(calculations["camera_power"]["value"], 0.5)
        self.assertEqual(calculations["keyboard_power"]["value"], 0.1)
        self.assertEqual(calculations["sam_power"]["value"], 0.8)
        self.assertEqual(calculations["touch_power"]["value"], 0.9)

    def test_requires_all_soc_and_display_rails(self):
        calculations = {}
        rails = {
            "PMIC_MT6363": {"value": 1.0, "unit": "W"},
            "pm_emi_display_panel": {"value": 0.4, "unit": "W"},
            "pm_emi_memory_0": {"value": 1.1, "unit": "W"},
        }

        corrected = apply_minos_power(calculations, rails, "minos")

        self.assertEqual(corrected, {"memory_power"})
        self.assertNotIn("soc_power", calculations)
        self.assertNotIn("display_power", calculations)

    def test_does_not_change_other_devices(self):
        calculations = {"soc_power": {"value": 4.0, "unit": "W"}}
        original = {name: entry.copy() for name, entry in calculations.items()}

        corrected = apply_minos_power(
            calculations,
            {"pm_emi_system": {"value": 20.0, "unit": "W"}},
            "Heracles",
        )

        self.assertEqual(corrected, set())
        self.assertEqual(calculations, original)


class BatteryLifeTests(unittest.TestCase):
    def test_calculates_capacity_divided_by_system_power(self):
        calculations = {"system_power": {"value": 10.0, "unit": "W"}}

        corrected = apply_battery_life(calculations, 80)

        self.assertEqual(corrected, "battery_life")
        self.assertEqual(calculations["battery_life"], {"value": 8.0, "unit": "hrs"})

    def test_requires_positive_capacity_and_system_power(self):
        calculations = {"system_power": {"value": 0, "unit": "W"}}

        corrected = apply_battery_life(calculations, 80)

        self.assertIsNone(corrected)
        self.assertNotIn("battery_life", calculations)


if __name__ == "__main__":
    unittest.main()
