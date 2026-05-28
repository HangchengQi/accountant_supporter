import unittest

from app.outlook import OutlookConfig, OutlookGraphClient


class OutlookConfigTest(unittest.TestCase):
    def test_configured_status_without_client_id(self) -> None:
        client = OutlookGraphClient(
            OutlookConfig(
                client_id="",
                tenant_id="common",
                scopes="offline_access User.Read Mail.Read",
            )
        )

        status = client.configured_status(has_token=False)

        self.assertFalse(status["configured"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["tenant_id"], "common")


if __name__ == "__main__":
    unittest.main()
