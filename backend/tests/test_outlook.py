import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.main import (
    mail_fetch_since_from_cursor,
    mail_poll_settings,
    run_mail_poll_worker_once,
    save_mail_poll_settings,
    save_outlook_settings,
    start_mail_poll_worker,
    stop_mail_poll_worker,
    storage,
    update_mail_fetch_cursor,
)
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

    def test_list_inbox_messages_uses_since_filter_and_paginates(self) -> None:
        class RecordingClient(OutlookGraphClient):
            def __init__(self) -> None:
                super().__init__(
                    OutlookConfig(
                        client_id="abc-123",
                        tenant_id="common",
                        scopes="offline_access User.Read Mail.Read",
                    )
                )
                self.paths: list[str] = []

            def _get_graph(self, path: str, token: dict[str, str]) -> dict[str, object]:
                self.paths.append(path)
                if len(self.paths) == 1:
                    return {
                        "value": [
                            {
                                "id": "message-1",
                                "subject": "Invoice",
                                "from": {"emailAddress": {"address": "billing@example.com"}},
                                "receivedDateTime": "2026-06-09T12:00:00Z",
                                "bodyPreview": "Preview",
                                "body": {"content": "<p>Hello</p>"},
                            }
                        ],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages?page=2",
                    }
                return {
                    "value": [
                        {
                            "id": "message-2",
                            "subject": "Receipt",
                            "from": {"emailAddress": {"name": "Vendor"}},
                            "receivedDateTime": "2026-06-09T12:01:00Z",
                            "bodyPreview": "Preview 2",
                            "body": {"content": "<p>World</p>"},
                        }
                    ]
                }

        client = RecordingClient()
        messages = client.list_inbox_messages(
            token={"access_token": "token"},
            top=75,
            received_since="2026-06-09T11:50:00Z",
        )

        self.assertEqual([message.id for message in messages], ["message-1", "message-2"])
        first_query = parse_qs(urlparse(client.paths[0]).query)
        self.assertEqual(first_query["$top"], ["50"])
        self.assertEqual(first_query["$orderby"], ["receivedDateTime desc"])
        self.assertEqual(first_query["$filter"], ["receivedDateTime ge 2026-06-09T11:50:00Z"])
        self.assertEqual(client.paths[1], "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages?page=2")

    def test_save_outlook_settings_updates_client_and_tenant(self) -> None:
        original = storage.get_connector_settings("outlook")
        try:
            status = save_outlook_settings(
                {
                    "client_id": "client-from-ui",
                    "account_type": "custom",
                    "tenant_id": "common",
                }
            )

            self.assertEqual(status["settings"]["client_id"], "client-from-ui")
            self.assertEqual(status["settings"]["tenant_id"], "common")
            self.assertIn("Mail.Send", status["settings"]["scopes"])
        finally:
            if original is not None:
                storage.save_connector_settings("outlook", original)
            else:
                storage.delete_connector_settings("outlook")

    def test_save_outlook_settings_supports_common_login_option(self) -> None:
        original = storage.get_connector_settings("outlook")
        try:
            status = save_outlook_settings(
                {
                    "client_id": "client-from-ui",
                    "account_type": "common",
                    "tenant_id": "ignored-tenant",
                }
            )

            self.assertEqual(status["settings"]["tenant_id"], "common")
            self.assertEqual(status["settings"]["account_type"], "common")
        finally:
            if original is not None:
                storage.save_connector_settings("outlook", original)
            else:
                storage.delete_connector_settings("outlook")

    def test_save_mail_poll_settings_controls_auto_fetch_interval(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            status = save_mail_poll_settings({"interval_minutes": "15"})

            self.assertEqual(status["interval_minutes"], 15)
            self.assertFalse(status["enabled"])
            self.assertEqual(status["health_status"], "stopped")
            self.assertTrue(mail_poll_settings()["saved_locally"])

            capped = save_mail_poll_settings({"interval_minutes": "2000"})
            self.assertEqual(capped["interval_minutes"], 1440)
        finally:
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_poll_worker_start_and_stop_controls_lifecycle(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings({"interval_minutes": "5", "mail_provider": "outlook"})
            with patch("app.main.is_outlook_configured", return_value=True):
                started = start_mail_poll_worker()

            self.assertTrue(started["enabled"])
            self.assertTrue(started["worker_alive"])

            stopped = stop_mail_poll_worker()
            self.assertFalse(stopped["enabled"])
            self.assertFalse(stopped["worker_alive"])
            self.assertEqual(stopped["health_status"], "stopped")
        finally:
            stop_mail_poll_worker()
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_poll_worker_start_requires_outlook_configuration(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings({"interval_minutes": "5", "mail_provider": "outlook"})
            with patch("app.main.is_outlook_configured", return_value=False):
                with self.assertRaises(ValueError):
                    start_mail_poll_worker()

            settings = mail_poll_settings()
            self.assertFalse(settings["enabled"])
            self.assertFalse(settings["worker_alive"])
            self.assertEqual(settings["health_status"], "stopped")
        finally:
            stop_mail_poll_worker()
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_fetch_cursor_uses_overlap_window(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings(
                {
                    "interval_minutes": "5",
                    "mail_provider": "outlook",
                    "fetch_not_before_at": "2026-06-01T00:00",
                }
            )
            status = update_mail_fetch_cursor(datetime(2026, 6, 9, 12, 0, tzinfo=UTC))

            self.assertEqual(status["last_successful_fetch_at"], "2026-06-09T12:00:00+00:00")
            self.assertEqual(mail_fetch_since_from_cursor(), "2026-06-09T11:50:00Z")
            self.assertEqual(mail_poll_settings()["interval_minutes"], 5)
        finally:
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_fetch_threshold_protects_first_fetch(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings(
                {
                    "interval_minutes": "5",
                    "mail_provider": "outlook",
                    "fetch_not_before_at": "2026-06-09T09:15",
                }
            )

            self.assertEqual(mail_fetch_since_from_cursor("outlook"), "2026-06-09T09:15:00Z")
            self.assertEqual(mail_poll_settings()["fetch_not_before_at"], "2026-06-09T09:15:00+00:00")
        finally:
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_fetch_threshold_caps_cursor_overlap(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings(
                {
                    "interval_minutes": "5",
                    "mail_provider": "outlook",
                    "fetch_not_before_at": "2026-06-09T11:55:00Z",
                }
            )
            update_mail_fetch_cursor(datetime(2026, 6, 9, 12, 0, tzinfo=UTC), "outlook")

            self.assertEqual(mail_fetch_since_from_cursor("outlook"), "2026-06-09T11:55:00Z")
        finally:
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")

    def test_mail_poll_worker_reports_waiting_for_outlook(self) -> None:
        original = storage.get_connector_settings("mail_poll")
        try:
            save_mail_poll_settings({"interval_minutes": "5", "mail_provider": "outlook"})
            with patch("app.main.get_outlook_token", side_effect=ValueError("Outlook is not connected")):
                result = run_mail_poll_worker_once()

            settings = storage.get_connector_settings("mail_poll") or {}
            self.assertEqual(result["status"], "waiting_for_mail")
            self.assertEqual(settings["last_worker_status"], "waiting_for_mail")
            self.assertIn("Outlook is not connected", settings["last_worker_error"])
        finally:
            if original is not None:
                storage.save_connector_settings("mail_poll", original)
            else:
                storage.delete_connector_settings("mail_poll")


if __name__ == "__main__":
    unittest.main()
