import json
import tempfile
import unittest
from pathlib import Path

from app.workflow import load_workflow


class WorkflowLoadingTest(unittest.TestCase):
    def test_loads_private_ai_process_and_redacts_public_status(self) -> None:
        workflow_data = {
            "workflow": "vendor_invoice_email",
            "version": 2,
            "processing": {
                "summary_sentences": 2,
                "minimum_confidence_for_auto_upload": 0.8,
                "require_human_review": True,
            },
            "ai_process": {
                "instructions": "Private accounting workflow instructions.",
            },
            "zoho": {
                "mode": "dry_run",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            path.write_text(json.dumps(workflow_data), encoding="utf-8")

            workflow = load_workflow(str(path))

        self.assertEqual(workflow.summary_sentences, 2)
        self.assertEqual(workflow.ai_instructions, "Private accounting workflow instructions.")
        self.assertNotIn("ai_process", workflow.public_status())
        self.assertNotIn("raw", workflow.public_status())


if __name__ == "__main__":
    unittest.main()
