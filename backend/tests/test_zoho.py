import unittest

from app.zoho import ZohoConfig, ZohoOAuthClient


class ZohoOAuthTest(unittest.TestCase):
    def test_configured_status_without_credentials(self) -> None:
        client = ZohoOAuthClient(
            ZohoConfig(
                client_id="",
                client_secret="",
                redirect_uri="http://127.0.0.1:8080/auth/zoho/callback",
                scopes="ZohoBooks.fullaccess.all",
            )
        )

        status = client.configured_status(has_token=False)

        self.assertFalse(status["configured"])
        self.assertFalse(status["connected"])
        self.assertFalse(status["settings"]["has_client_secret"])

    def test_config_from_settings_overrides_env(self) -> None:
        config = ZohoConfig.from_settings(
            {
                "client_id": "saved-client",
                "client_secret": "saved-secret",
                "redirect_uri": "http://127.0.0.1:8080/auth/zoho/callback",
                "scopes": "ZohoBooks.bills.CREATE",
            }
        )

        self.assertEqual(config.client_id, "saved-client")
        self.assertEqual(config.client_secret, "saved-secret")
        self.assertEqual(config.scopes, "ZohoBooks.bills.CREATE")

    def test_authorization_url_points_to_zoho_accounts(self) -> None:
        client = ZohoOAuthClient(
            ZohoConfig(
                client_id="client-123",
                client_secret="secret-123",
                redirect_uri="http://127.0.0.1:8080/auth/zoho/callback",
                scopes="ZohoBooks.fullaccess.all",
            )
        )

        url = client.authorization_url("state-value")

        self.assertTrue(url.startswith("https://accounts.zoho.com/oauth/v2/auth?"))
        self.assertIn("client_id=client-123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("state=state-value", url)


if __name__ == "__main__":
    unittest.main()
