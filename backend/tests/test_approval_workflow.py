import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import discard_processed_email, reject_processed_email
from app.schemas import EmailSampleIn, ExtractedFields
from app.storage import SQLiteStorage
from app.workflow import Workflow


class ApprovalWorkflowTest(unittest.TestCase):
    def test_discard_marks_processed_email_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_storage = SQLiteStorage(str(Path(directory) / "test.db"))
            record = self._save_pending(test_storage)

            with patch("app.main.storage", test_storage):
                result = discard_processed_email(record.id, "Not a bill")

            self.assertEqual(result["zoho_status"], "discarded")
            self.assertEqual(result["zoho_payload"]["discard_reason"], "Not a bill")

    def test_reject_supersedes_original_pending_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_storage = SQLiteStorage(str(Path(directory) / "test.db"))
            record = self._save_pending(test_storage)

            with patch("app.main.storage", test_storage), patch("app.main.run_queue_once") as run_queue:
                run_queue.return_value.to_dict.return_value = {"completed": 1}
                reject_processed_email(record.id, "Repairs and Maintenance")

            updated = test_storage.get_processed_email(record.id)
            self.assertEqual(updated.zoho_status, "superseded")
            self.assertEqual(updated.zoho_payload["reviewer_suggestion"], "Repairs and Maintenance")

    def _save_pending(self, storage: SQLiteStorage):
        workflow = Workflow(
            name="vendor_invoice_email",
            version=1,
            summary_sentences=3,
            require_human_review=True,
            minimum_confidence_for_auto_upload=0.9,
            zoho_mode="dry_run",
            ai_instructions="Extract.",
            raw={},
        )
        return storage.save_processed_email(
            email=EmailSampleIn("Invoice", "Vendor <billing@example.com>", "Amount due $10"),
            summary="Invoice summary",
            extracted=ExtractedFields(
                category="invoice",
                confidence=0.9,
                needs_review=True,
                vendor_name="Vendor",
                invoice_number="INV-1",
                amount=10,
                currency="USD",
                expense_account_name="Office Supplies",
                account_confidence=0.5,
            ),
            workflow=workflow,
            zoho_status="pending_approval",
            zoho_payload={"approval_reason": "Low confidence"},
        )


if __name__ == "__main__":
    unittest.main()
