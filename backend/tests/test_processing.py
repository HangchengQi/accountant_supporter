import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from app.ai import LocalHeuristicProcessor
from app.main import _finalize_processed_email, _initial_zoho_state, process_mail_message, render_record, storage
from app.schemas import EmailSampleIn, ExtractedFields, MailMessage, ProcessedEmail
from app.workflow import Workflow


class LocalProcessorTest(unittest.TestCase):
    def test_local_processor_extracts_invoice_fields(self) -> None:
        workflow = Workflow(
            name="vendor_invoice_email",
            version=1,
            summary_sentences=3,
            require_human_review=True,
            minimum_confidence_for_auto_upload=0.9,
            zoho_mode="dry_run",
            ai_instructions="Extract bookkeeping fields.",
            raw={},
        )
        email = EmailSampleIn(
            subject="Invoice INV-1042 from Northstar",
            sender="Northstar <billing@example.com>",
            body="Invoice date: 05/20/2026\nDue date: 06/19/2026\nAmount due: $842.15",
        )

        result = LocalHeuristicProcessor().process(email, workflow)

        self.assertEqual(result.extracted.category, "invoice")
        self.assertEqual(result.extracted.invoice_number, "INV-1042")
        self.assertEqual(result.extracted.amount, 842.15)
        self.assertTrue(result.extracted.needs_review)

    def test_irrelevant_extraction_is_not_pending_approval(self) -> None:
        with patch("app.main.is_zoho_connected", return_value=True):
            status, payload = _initial_zoho_state(
                ExtractedFields(
                    category="irrelevant",
                    confidence=0.98,
                    needs_review=False,
                )
            )

        self.assertEqual(status, "not_bill")
        self.assertIn("not bill-relevant", payload["approval_reason"])

    def test_bill_extraction_is_not_pending_approval_without_zoho_connection(self) -> None:
        with patch("app.main.is_zoho_connected", return_value=False):
            status, payload = _initial_zoho_state(
                ExtractedFields(
                    category="invoice",
                    confidence=0.98,
                    needs_review=False,
                )
            )

        self.assertEqual(status, "zoho_not_connected")
        self.assertIn("Zoho Books is not connected", payload["approval_reason"])

    def test_finalize_bill_record_stays_out_of_approval_without_zoho_connection(self) -> None:
        record = ProcessedEmail(
            id=1,
            created_at=datetime.now(UTC),
            subject="Invoice",
            sender="Vendor <billing@example.com>",
            summary="Invoice summary.",
            extracted=ExtractedFields(category="invoice", confidence=0.98, needs_review=False),
            workflow_name="vendor_invoice_email",
            workflow_version=1,
            zoho_status="zoho_not_connected",
            zoho_payload={},
        )

        with (
            patch("app.main.is_zoho_connected", return_value=False),
            patch.object(storage, "update_processed_email_zoho") as update_zoho,
            patch.object(storage, "get_processed_email", return_value=record),
        ):
            result = _finalize_processed_email(record, [])

        self.assertEqual(result.zoho_status, "zoho_not_connected")
        update_zoho.assert_called_once()
        self.assertEqual(update_zoho.call_args.args[1], "zoho_not_connected")

    def test_process_mail_message_does_not_save_billing_artifacts_for_irrelevant_email(self) -> None:
        message = MailMessage(
            id=1,
            provider="gmail",
            provider_message_id="message-1",
            received_at="2026-06-13T12:00:00Z",
            subject="Promotional email",
            sender="Promo <promo@example.com>",
            body_preview="Sale",
            body="Sale",
            classification_status="relevant",
            classification_category="invoice",
            classification_confidence=0.9,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        processed = ProcessedEmail(
            id=1,
            created_at=datetime.now(UTC),
            subject="Promotional email",
            sender="Promo <promo@example.com>",
            summary="Promotional email.",
            extracted=ExtractedFields(category="irrelevant", confidence=0.98, needs_review=False),
            workflow_name="vendor_invoice_email",
            workflow_version=1,
            zoho_status="not_bill",
            zoho_payload={},
        )

        with (
            patch("app.main._list_bill_attachments", return_value=[]),
            patch("app.main.process_email_sample", return_value=processed),
            patch("app.main._handle_billing_artifacts") as artifacts,
        ):
            result = process_mail_message(message)

        self.assertEqual(result.zoho_status, "not_bill")
        artifacts.assert_not_called()

    def test_irrelevant_pending_record_renders_without_approval_actions(self) -> None:
        record = ProcessedEmail(
            id=1,
            created_at=datetime.now(UTC),
            subject="Promotional email",
            sender="Promo <promo@example.com>",
            summary="Promotional email.",
            extracted=ExtractedFields(category="irrelevant", confidence=0.98, needs_review=True),
            workflow_name="vendor_invoice_email",
            workflow_version=1,
            zoho_status="pending_approval",
            zoho_payload={"approval_reason": "Old pending record"},
        )

        html = render_record(record)

        self.assertIn("Not bill-relevant", html)
        self.assertIn("Extraction confidence", html)
        self.assertNotIn("Approve and upload to Zoho", html)


if __name__ == "__main__":
    unittest.main()
