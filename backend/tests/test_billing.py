import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.billing import BillAttachment, save_billing_artifacts
from app.schemas import ExtractedFields, MailMessage, ProcessedEmail


class BillingArtifactsTest(unittest.TestCase):
    def test_saves_renamed_attachment_and_daily_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = MailMessage(
                id=1,
                provider="outlook",
                provider_message_id="message-1",
                received_at="2026-06-05T14:30:00Z",
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
            processed = ProcessedEmail(
                id=1,
                created_at=datetime.now(UTC),
                subject=message.subject,
                sender=message.sender,
                summary="Invoice for testing.",
                extracted=ExtractedFields(
                    category="invoice",
                    confidence=0.95,
                    needs_review=True,
                    vendor_name="Test Vendor",
                    invoice_number="INV-100",
                    invoice_date="2026-06-05",
                    amount=125.0,
                    currency="USD",
                ),
                workflow_name="vendor_invoice_email",
                workflow_version=1,
                zoho_status="dry_run",
                zoho_payload={},
            )

            artifacts = save_billing_artifacts(
                message=message,
                processed=processed,
                attachments=[BillAttachment(name="original.pdf", content=b"pdf bytes")],
                bills_root=str(root / "bills"),
                logs_root=str(root / "logs"),
            )

            saved_file = artifacts.saved_files[0]
            self.assertEqual(artifacts.billing_folder.name, "Billing2026-06-05")
            self.assertEqual(saved_file.name, "Test_Vendor_2026-06-05_INV-100.pdf")
            self.assertEqual(saved_file.read_bytes(), b"pdf bytes")
            log_text = artifacts.daily_log.read_text(encoding="utf-8")
            self.assertIn("Invoice for testing.", log_text)
            self.assertIn("Test_Vendor_2026-06-05_INV-100.pdf", log_text)


if __name__ == "__main__":
    unittest.main()
