import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.main import (
    _handle_billing_artifacts,
    get_bills_root,
    save_invoice_storage_settings,
    send_daily_billing_log_if_due,
    storage,
)
from app.billing import BillAttachment
from app.schemas import ExtractedFields, MailMessage, ProcessedEmail


def _message() -> MailMessage:
    return MailMessage(
        id=1,
        provider="gmail",
        provider_message_id="gmail-message-1",
        received_at="2026-06-13T14:30:00Z",
        subject="Invoice INV-100",
        sender="Vendor <billing@example.com>",
        body_preview="Amount due $125",
        body="Amount due $125",
        classification_status="relevant",
        classification_category="invoice",
        classification_confidence=0.9,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _processed() -> ProcessedEmail:
    return ProcessedEmail(
        id=1,
        created_at=datetime.now(UTC),
        subject="Invoice INV-100",
        sender="Vendor <billing@example.com>",
        summary="Invoice for testing.",
        extracted=ExtractedFields(
            category="invoice",
            confidence=0.95,
            needs_review=True,
            vendor_name="Test Vendor",
            invoice_number="INV-100",
            invoice_date="2026-06-13",
            amount=125.0,
            currency="USD",
        ),
        workflow_name="vendor_invoice_email",
        workflow_version=1,
        zoho_status="pending_approval",
        zoho_payload={},
    )


class DailyBillingLogSendTest(unittest.TestCase):
    def test_invoice_storage_setting_controls_billing_root(self) -> None:
        original = storage.get_connector_settings("invoice_storage")
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "custom-invoices"
                settings = save_invoice_storage_settings({"invoice_directory": str(root)})

                self.assertEqual(settings["invoice_directory"], str(root))
                self.assertEqual(get_bills_root(), str(root))

                with patch("app.main.get_logs_root", return_value=str(Path(directory) / "logs")):
                    artifacts = _handle_billing_artifacts(
                        _message(),
                        _processed(),
                        [BillAttachment(name="invoice.pdf", content=b"pdf")],
                    )

                self.assertEqual(artifacts.billing_folder.parent, root)
                self.assertTrue(artifacts.saved_files[0].exists())
        finally:
            if original is not None:
                storage.save_connector_settings("invoice_storage", original)
            else:
                storage.delete_connector_settings("invoice_storage")

    def test_processing_bill_only_appends_log_without_sending_email(self) -> None:
        original = storage.get_connector_settings("billing_log")
        try:
            storage.save_connector_settings(
                "billing_log",
                {"receiver_email": "owner@example.com", "send_time": "17:00"},
            )
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    patch("app.main.get_bills_root", return_value=str(root / "bills")),
                    patch("app.main.get_logs_root", return_value=str(root / "logs")),
                    patch("app.main.get_gmail_client") as client_factory,
                ):
                    artifacts = _handle_billing_artifacts(
                        _message(),
                        _processed(),
                        [BillAttachment(name="invoice.pdf", content=b"pdf")],
                    )

                self.assertTrue(artifacts.daily_log.exists())
                client_factory.assert_not_called()
        finally:
            if original is not None:
                storage.save_connector_settings("billing_log", original)
            else:
                storage.delete_connector_settings("billing_log")

    def test_daily_log_sender_sends_once_per_day_after_configured_time(self) -> None:
        original = storage.get_connector_settings("billing_log")
        try:
            storage.save_connector_settings(
                "billing_log",
                {"receiver_email": "owner@example.com", "send_time": "17:00"},
            )
            with tempfile.TemporaryDirectory() as directory:
                logs_root = Path(directory)
                (logs_root / "billing-log-2026-06-13.md").write_text("daily log", encoding="utf-8")
                client = Mock()
                now = datetime(2026, 6, 13, 17, 5, tzinfo=UTC)
                with (
                    patch("app.main.get_logs_root", return_value=str(logs_root)),
                    patch("app.main.active_mail_provider", return_value="gmail"),
                    patch("app.main.get_gmail_token", return_value={"access_token": "token"}),
                    patch("app.main.get_gmail_client", return_value=client),
                ):
                    first = send_daily_billing_log_if_due(now)
                    second = send_daily_billing_log_if_due(now)

            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["reason"], "already_sent")
            client.send_mail.assert_called_once()
        finally:
            if original is not None:
                storage.save_connector_settings("billing_log", original)
            else:
                storage.delete_connector_settings("billing_log")


if __name__ == "__main__":
    unittest.main()
