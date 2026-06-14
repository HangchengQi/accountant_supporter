from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ai import classify_email, verify_bill_fraud
from .schemas import Job, MailMessage, ProcessedEmail
from .storage import SQLiteStorage


CLASSIFY_EMAIL = "classify_email"
VERIFY_FRAUD = "verify_fraud"
PROCESS_EMAIL = "process_email"
REVIEW_ACCOUNT = "review_account"


@dataclass(frozen=True)
class QueueRunResult:
    claimed: int
    completed: int
    failed: int
    created_processing_jobs: int
    isolated_fraud: int
    skipped_irrelevant: int
    results: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "created_processing_jobs": self.created_processing_jobs,
            "isolated_fraud": self.isolated_fraud,
            "skipped_irrelevant": self.skipped_irrelevant,
            "results": self.results,
        }


def enqueue_mail_message(storage: SQLiteStorage, message: MailMessage) -> Job:
    return storage.ensure_pending_job(
        job_type=CLASSIFY_EMAIL,
        mail_message_id=message.id,
        payload={"provider": message.provider, "provider_message_id": message.provider_message_id},
        priority=20,
    )


def run_queue_once(
    storage: SQLiteStorage,
    process_email: Any,
    review_account: Any | None = None,
    max_jobs: int = 10,
    ai_settings: dict[str, Any] | None = None,
) -> QueueRunResult:
    claimed = 0
    completed = 0
    failed = 0
    created_processing_jobs = 0
    isolated_fraud = 0
    skipped_irrelevant = 0
    results: list[dict[str, Any]] = []

    for _ in range(max(1, max_jobs)):
        job = storage.claim_next_job()
        if job is None:
            break
        claimed += 1
        try:
            outcome = _run_job(storage, job, process_email, review_account, ai_settings)
            completed += 1
            created_processing_jobs += int(outcome.get("created_processing_job", False))
            isolated_fraud += int(outcome.get("isolated_fraud", False))
            skipped_irrelevant += int(outcome.get("skipped_irrelevant", False))
            results.append({"job_id": job.id, **outcome})
            storage.complete_job(job.id, outcome)
        except Exception as exc:
            failed += 1
            error = str(exc)
            results.append({"job_id": job.id, "status": "failed", "error": error})
            storage.fail_job(job, error)

    return QueueRunResult(
        claimed=claimed,
        completed=completed,
        failed=failed,
        created_processing_jobs=created_processing_jobs,
        isolated_fraud=isolated_fraud,
        skipped_irrelevant=skipped_irrelevant,
        results=results,
    )


def queue_status(storage: SQLiteStorage) -> dict[str, Any]:
    return {
        "job_counts": storage.job_counts(),
        "jobs": [job.to_dict() for job in storage.list_jobs(limit=25)],
        "mail_messages": [message.to_dict() for message in storage.list_mail_messages(limit=25)],
    }


def _run_job(
    storage: SQLiteStorage,
    job: Job,
    process_email: Any,
    review_account: Any | None,
    ai_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    if job.job_type == CLASSIFY_EMAIL:
        return _classify_message(storage, job, ai_settings)
    if job.job_type == VERIFY_FRAUD:
        return _verify_fraud_message(storage, job, ai_settings)
    if job.job_type == PROCESS_EMAIL:
        return _process_message(storage, job, process_email)
    if job.job_type == REVIEW_ACCOUNT:
        if review_account is None:
            raise ValueError("review_account handler is not configured")
        processed: ProcessedEmail = review_account(job.payload)
        return {
            "status": "reviewed",
            "processed_email_id": processed.id,
        }
    raise ValueError(f"unknown job type: {job.job_type}")


def _classify_message(
    storage: SQLiteStorage,
    job: Job,
    ai_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    message = _job_message(storage, job)
    classification = classify_email(message.to_email(), ai_settings)
    status = "relevant" if classification.is_bill_relevant else "skipped"
    if classification.needs_review and not classification.is_bill_relevant:
        status = "needs_review"

    storage.update_mail_classification(
        mail_message_id=message.id,
        status=status,
        category=classification.category,
        confidence=classification.confidence,
    )

    if classification.is_bill_relevant:
        fraud_job = storage.ensure_pending_job(
            job_type=VERIFY_FRAUD,
            mail_message_id=message.id,
            payload={"classification_category": classification.category},
            priority=30,
        )
        return {
            "status": "classified",
            "category": classification.category,
            "confidence": classification.confidence,
            "mode": classification.mode,
            "created_fraud_verification_job": fraud_job.status == "pending",
            "fraud_verification_job_id": fraud_job.id,
        }

    return {
        "status": status,
        "category": classification.category,
        "confidence": classification.confidence,
        "mode": classification.mode,
        "skipped_irrelevant": status == "skipped",
    }


def _verify_fraud_message(
    storage: SQLiteStorage,
    job: Job,
    ai_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    message = _job_message(storage, job)
    memory = storage.get_connector_settings("fraud_memory") or {}
    examples = memory.get("confirmed_fraud_examples", [])
    result = verify_bill_fraud(message.to_email(), ai_settings, examples)
    if result.is_high_risk:
        review = storage.save_fraud_review(
            mail_message_id=message.id,
            status="pending_review",
            risk_level=result.risk_level,
            risk_score=result.risk_score,
            reasons=result.reasons,
        )
        storage.update_mail_classification(
            mail_message_id=message.id,
            status="fraud_review",
            category=message.classification_category,
            confidence=message.classification_confidence,
        )
        return {
            "status": "isolated_fraud_review",
            "risk_level": result.risk_level,
            "risk_score": result.risk_score,
            "mode": result.mode,
            "fraud_review_id": review.id,
            "isolated_fraud": True,
        }

    processing_job = storage.ensure_pending_job(
        job_type=PROCESS_EMAIL,
        mail_message_id=message.id,
        payload={
            "classification_category": message.classification_category,
            "fraud_risk_level": result.risk_level,
            "fraud_risk_score": result.risk_score,
        },
        priority=40,
    )
    return {
        "status": "fraud_checked",
        "risk_level": result.risk_level,
        "risk_score": result.risk_score,
        "mode": result.mode,
        "created_processing_job": processing_job.status == "pending",
        "processing_job_id": processing_job.id,
    }


def _process_message(storage: SQLiteStorage, job: Job, process_email: Any) -> dict[str, Any]:
    message = _job_message(storage, job)
    processed: ProcessedEmail = process_email(message)
    return {
        "status": "processed",
        "processed_email_id": processed.id,
        "mail_message_id": message.id,
    }


def _job_message(storage: SQLiteStorage, job: Job) -> MailMessage:
    if job.mail_message_id is None:
        raise ValueError("job is missing mail_message_id")
    message = storage.get_mail_message(job.mail_message_id)
    if message is None:
        raise ValueError(f"mail message not found: {job.mail_message_id}")
    return message
