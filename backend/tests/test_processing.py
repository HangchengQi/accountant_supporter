import unittest

from app.ai import LocalHeuristicProcessor
from app.schemas import EmailSampleIn
from app.workflow import Workflow


class LocalProcessorTest(unittest.TestCase):
    def test_local_processor_extracts_invoice_fields(self) -> None:
        workflow = Workflow(
            name="vendor_invoice_email",
            version=1,
            require_human_review=True,
            minimum_confidence_for_auto_upload=0.9,
            zoho_mode="dry_run",
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


if __name__ == "__main__":
    unittest.main()
