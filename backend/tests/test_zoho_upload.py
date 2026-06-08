import unittest

from app.main import _zoho_date


class ZohoUploadTest(unittest.TestCase):
    def test_zoho_date_normalizes_common_us_formats(self) -> None:
        self.assertEqual(_zoho_date("2026-06-08"), "2026-06-08")
        self.assertEqual(_zoho_date("06/08/2026"), "2026-06-08")
        self.assertEqual(_zoho_date("6/8/26"), "2026-06-08")
        self.assertEqual(_zoho_date(None), None)

    def test_zoho_date_rejects_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            _zoho_date("June 8, 2026")


if __name__ == "__main__":
    unittest.main()
