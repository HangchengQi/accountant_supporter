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
