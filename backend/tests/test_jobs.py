import tempfile
import unittest
from pathlib import Path

from app.jobs import PROCESS_EMAIL, VERIFY_FRAUD, enqueue_mail_message, run_queue_once
from app.schemas import MailMessage
from app.storage import SQLiteStorage


class JobQueueTest(unittest.TestCase):
    def test_classification_promotes_bill_relevant_email_to_fraud_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(str(Path(directory) / "queue.db"))
            message = storage.save_mail_message(
                provider="outlook",
                provider_message_id="message-1",
                received_at="2026-06-05T12:00:00Z",
                subject="Invoice INV-100 from Vendor",
                sender="Vendor <billing@example.com>",
                body_preview="Amount due $125.00",
                body="Invoice INV-100\nAmount due $125.00",
            )
            enqueue_mail_message(storage, message)

            result = run_queue_once(storage, self._fake_process, max_jobs=1)

            updated = storage.get_mail_message(message.id)
            jobs = storage.list_jobs()
            self.assertEqual(result.completed, 1)
            self.assertEqual(updated.classification_status, "relevant")
            self.assertEqual(updated.classification_category, "invoice")
            self.assertTrue(any(job.job_type == VERIFY_FRAUD for job in jobs))
            self.assertFalse(any(job.job_type == PROCESS_EMAIL for job in jobs))

    def test_queue_processes_relevant_email_after_classification(self) -> None:
        processed_subjects = []

        def process(message: MailMessage) -> object:
            processed_subjects.append(message.subject)
            return type("Processed", (), {"id": 42})()

        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(str(Path(directory) / "queue.db"))
            message = storage.save_mail_message(
                provider="outlook",
                provider_message_id="message-2",
                received_at="2026-06-05T12:00:00Z",
                subject="Receipt from Vendor",
                sender="Vendor <billing@example.com>",
                body_preview="Payment received",
                body="Receipt\nPayment received",
            )
            enqueue_mail_message(storage, message)

            result = run_queue_once(storage, process, max_jobs=5)

            self.assertEqual(result.completed, 3)
            self.assertEqual(processed_subjects, ["Receipt from Vendor"])

    def test_high_risk_bill_is_isolated_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(str(Path(directory) / "queue.db"))
            message = storage.save_mail_message(
                provider="outlook",
                provider_message_id="message-fraud",
                received_at="2026-06-05T12:00:00Z",
                subject="Invoice INV-200 urgent payment",
                sender="Vendor <billing@example.com>",
                body_preview="Use new bank details for urgent wire transfer today",
                body="Invoice INV-200\nUse new bank details for updated payment. This is urgent, pay now by wire transfer today.",
            )
            enqueue_mail_message(storage, message)

            result = run_queue_once(storage, self._fake_process, max_jobs=5)

            updated = storage.get_mail_message(message.id)
            reviews = storage.list_fraud_reviews(status="pending_review")
            self.assertEqual(result.isolated_fraud, 1)
            self.assertEqual(updated.classification_status, "fraud_review")
            self.assertEqual(len(reviews), 1)
            self.assertFalse(any(job.job_type == PROCESS_EMAIL and job.status == "pending" for job in storage.list_jobs()))

    def test_irrelevant_email_is_skipped_without_processing_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(str(Path(directory) / "queue.db"))
            message = storage.save_mail_message(
                provider="outlook",
                provider_message_id="message-3",
                received_at="2026-06-05T12:00:00Z",
                subject="Team lunch",
                sender="Person <person@example.com>",
                body_preview="Where should we eat?",
                body="Where should we eat?",
            )
            enqueue_mail_message(storage, message)

            result = run_queue_once(storage, self._fake_process, max_jobs=5)

            updated = storage.get_mail_message(message.id)
            jobs = storage.list_jobs()
            self.assertEqual(result.skipped_irrelevant, 1)
            self.assertEqual(updated.classification_status, "skipped")
            self.assertFalse(any(job.job_type == PROCESS_EMAIL for job in jobs))

    def test_mail_ingestion_deduplicates_by_provider_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(str(Path(directory) / "queue.db"))

            first = storage.save_mail_message(
                provider="outlook",
                provider_message_id="duplicate",
                received_at=None,
                subject="Invoice A",
                sender="Vendor",
                body_preview="Amount due $10",
                body="Amount due $10",
            )
            second = storage.save_mail_message(
                provider="outlook",
                provider_message_id="duplicate",
                received_at=None,
                subject="Invoice A updated",
                sender="Vendor",
                body_preview="Amount due $10",
                body="Amount due $10",
            )

            self.assertEqual(first.id, second.id)
            self.assertEqual(len(storage.list_mail_messages()), 1)
            self.assertEqual(second.subject, "Invoice A updated")

    def _fake_process(self, message: MailMessage) -> object:
        return type("Processed", (), {"id": 1})()


if __name__ == "__main__":
    unittest.main()
