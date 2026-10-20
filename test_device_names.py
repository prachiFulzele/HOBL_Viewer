import unittest

from device_names import canonical_device_kql, canonical_device_name


class CanonicalDeviceNameTests(unittest.TestCase):
    def test_purwa_device_aliases_share_one_name(self):
        expected = "QC-LAM-PURWA-01"

        self.assertEqual(canonical_device_name("QCLAMPURWAIDL01"), expected)
        self.assertEqual(canonical_device_name("QC-LAM-PURWA-01"), expected)
        self.assertEqual(canonical_device_name("Purwa_DC"), expected)

    def test_kusto_expression_merges_purwa_devices(self):
        expression = canonical_device_kql()

        for alias in ("'qclampurwaidl01'", "'qc-lam-purwa-01'", "'purwa_dc'"):
            self.assertIn(alias, expression)
        self.assertIn("'QC-LAM-PURWA-01'", expression)

    def test_rom_devices_remain_separate(self):
        self.assertEqual(canonical_device_name("ROM-MS-IDCLAB-2"), "ROM-MS-IDCLAB-2")
        self.assertEqual(canonical_device_name("ROM-MS-IDC-25"), "ROM-MS-IDC-25")

    def test_kusto_expression_does_not_merge_rom_device(self):
        expression = canonical_device_kql()

        self.assertNotIn("'rom-ms-idc-25'", expression)
        self.assertIn("'ROM-MS-IDCLAB-2'", expression)


if __name__ == "__main__":
    unittest.main()
