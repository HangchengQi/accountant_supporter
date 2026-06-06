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
                "client_secret": "secret-value",
                "redirect_uri": "http://127.0.0.1:8080/auth/outlook/callback",
            }
        )

        self.assertTrue(config.is_configured)
        self.assertEqual(config.client_id, "abc-123")
        self.assertEqual(config.tenant_id, "tenant-456")
        self.assertEqual(config.client_secret, "secret-value")

    def test_authorization_url_points_to_microsoft_login(self) -> None:
        client = OutlookGraphClient(
            OutlookConfig(
                client_id="abc-123",
                tenant_id="tenant-456",
                scopes="offline_access User.Read Mail.Read",
                redirect_uri="http://127.0.0.1:8080/auth/outlook/callback",
            )
        )

        url = client.authorization_url("state-value")

        self.assertTrue(url.startswith("https://login.microsoftonline.com/tenant-456/"))
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=abc-123", url)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A8080%2Fauth%2Foutlook%2Fcallback", url)
        self.assertIn("prompt=select_account", url)
        self.assertIn("state=state-value", url)


if __name__ == "__main__":
    unittest.main()
