import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.jobs import PROCESS_EMAIL
from app.main import clear_fraud_review, confirm_fraud_review
from app.storage import SQLiteStorage


class FraudReviewWorkflowTest(unittest.TestCase):
    def test_confirm_fraud_review_saves_local_memory_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_storage = SQLiteStorage(str(Path(directory) / "fraud.db"))
            message = _save_message(test_storage)
            review = test_storage.save_fraud_review(
                mail_message_id=message.id,
                status="pending_review",
                risk_level="high",
                risk_score=0.91,
                reasons=["Requests new bank details."],
            )

            with patch("app.main.storage", test_storage):
                result = confirm_fraud_review(review.id)

            memory = test_storage.get_connector_settings("fraud_memory")
            updated = test_storage.get_fraud_review(review.id)
            message_after = test_storage.get_mail_message(message.id)
            self.assertEqual(result["memory_count"], 1)
            self.assertEqual(updated.status, "confirmed_fraud")
            self.assertEqual(message_after.classification_status, "confirmed_fraud")
            self.assertEqual(memory["confirmed_fraud_examples"][0]["sender"], message.sender)

    def test_clear_fraud_review_returns_message_to_processing_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_storage = SQLiteStorage(str(Path(directory) / "fraud.db"))
            message = _save_message(test_storage)
            review = test_storage.save_fraud_review(
                mail_message_id=message.id,
                status="pending_review",
                risk_level="high",
                risk_score=0.91,
                reasons=["Requests new bank details."],
            )

            with patch("app.main.storage", test_storage):
                result = clear_fraud_review(review.id)

            updated = test_storage.get_fraud_review(review.id)
            message_after = test_storage.get_mail_message(message.id)
            jobs = test_storage.list_jobs()
            self.assertEqual(updated.status, "cleared")
            self.assertEqual(message_after.classification_status, "relevant")
            self.assertEqual(result["job"]["job_type"], PROCESS_EMAIL)
            self.assertTrue(any(job.job_type == PROCESS_EMAIL and job.status == "pending" for job in jobs))


def _save_message(storage: SQLiteStorage):
    message = storage.save_mail_message(
        provider="outlook",
        provider_message_id="fraud-review-message",
        received_at="2026-06-14T12:00:00Z",
        subject="Invoice INV-500 urgent payment",
        sender="Vendor <billing@example.com>",
        body_preview="Use new bank details",
        body="Invoice INV-500. Use new bank details.",
    )
    storage.update_mail_classification(message.id, "fraud_review", "invoice", 0.91)
    return storage.get_mail_message(message.id)


if __name__ == "__main__":
    unittest.main()
