import unittest
from urllib.parse import parse_qs, urlparse

from app.gmail import GmailClient, GmailConfig
from app.main import gmail_status, save_gmail_settings, storage


class GmailClientTest(unittest.TestCase):
    def test_authorization_url_points_to_google_login(self) -> None:
        client = GmailClient(
            GmailConfig(
                client_id="gmail-client",
                client_secret="gmail-secret",
                scopes="https://www.googleapis.com/auth/gmail.readonly",
                redirect_uri="http://127.0.0.1:8080/auth/gmail/callback",
            )
        )

        url = client.authorization_url("state-value")
        query = parse_qs(urlparse(url).query)

        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth"))
        self.assertEqual(query["client_id"], ["gmail-client"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:8080/auth/gmail/callback"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["state"], ["state-value"])

    def test_list_inbox_messages_uses_gmail_query_and_paginates(self) -> None:
        class RecordingClient(GmailClient):
            def __init__(self) -> None:
                super().__init__(
                    GmailConfig(
                        client_id="gmail-client",
                        client_secret="gmail-secret",
                        scopes="https://www.googleapis.com/auth/gmail.readonly",
                    )
                )
                self.paths: list[str] = []

            def _get_gmail(self, path: str, token: dict[str, str]) -> dict[str, object]:
                self.paths.append(path)
                if path.startswith("/users/me/messages?"):
                    if "pageToken=" not in path:
                        return {
                            "messages": [{"id": "gmail-1"}],
                            "nextPageToken": "next-page",
                        }
                    return {"messages": [{"id": "gmail-2"}]}
                message_id = path.split("/messages/", 1)[1].split("?", 1)[0]
                return {
                    "id": message_id,
                    "internalDate": "1781006400000",
                    "snippet": "Invoice preview",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Invoice"},
                            {"name": "From", "value": "Vendor <billing@example.com>"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": "QW1vdW50IGR1ZSAkMTAw"},
                    },
                }

        client = RecordingClient()
        messages = client.list_inbox_messages(
            token={"access_token": "token"},
            top=2,
            received_since="2026-06-09T11:50:00Z",
        )

        self.assertEqual([message.id for message in messages], ["gmail-1", "gmail-2"])
        first_query = parse_qs(urlparse(client.paths[0]).query)
        self.assertEqual(first_query["maxResults"], ["2"])
        self.assertEqual(first_query["q"], ["in:inbox after:2026-06-09"])
        self.assertIn("pageToken=next-page", client.paths[2])

    def test_save_gmail_settings_requires_client_secret_and_saves_locally(self) -> None:
        original = storage.get_connector_settings("gmail")
        try:
            status = save_gmail_settings(
                {
                    "client_id": "gmail-client",
                    "client_secret": "gmail-secret",
                    "redirect_uri": "http://127.0.0.1:8080/auth/gmail/callback",
                    "scopes": "https://www.googleapis.com/auth/gmail.readonly",
                }
            )

            self.assertTrue(status["configured"])
            self.assertTrue(status["settings"]["has_client_secret"])
            self.assertTrue(gmail_status()["settings"]["saved_locally"])
            self.assertIn("https://www.googleapis.com/auth/gmail.send", status["settings"]["scopes"])
        finally:
            if original is not None:
                storage.save_connector_settings("gmail", original)
            else:
                storage.delete_connector_settings("gmail")


if __name__ == "__main__":
    unittest.main()
