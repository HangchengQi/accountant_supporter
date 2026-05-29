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

    def test_config_from_settings_uses_saved_values(self) -> None:
        config = OutlookConfig.from_settings(
            {
                "client_id": "abc-123",
                "tenant_id": "tenant-456",
                "scopes": "offline_access User.Read Mail.Read",
            }
        )

        self.assertTrue(config.is_configured)
        self.assertEqual(config.client_id, "abc-123")
        self.assertEqual(config.tenant_id, "tenant-456")


if __name__ == "__main__":
    unittest.main()
