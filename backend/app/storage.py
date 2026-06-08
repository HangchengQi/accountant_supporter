from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import EmailSampleIn, ExtractedFields, Job, MailMessage, ProcessedEmail
from .workflow import Workflow


class SQLiteStorage:
    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Any:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    body TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    extracted_json TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    workflow_version INTEGER NOT NULL,
                    zoho_status TEXT NOT NULL,
                    zoho_payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    provider TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    token_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connector_settings (
                    provider TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    settings_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    received_at TEXT,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    body_preview TEXT NOT NULL,
                    body TEXT NOT NULL,
                    classification_status TEXT NOT NULL DEFAULT 'pending',
                    classification_category TEXT,
                    classification_confidence REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, provider_message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    mail_message_id INTEGER,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(mail_message_id) REFERENCES mail_messages(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                ON jobs(status, priority, id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_mail_message_type
                ON jobs(mail_message_id, job_type)
                """
            )

    def save_processed_email(
        self,
        email: EmailSampleIn,
        summary: str,
        extracted: ExtractedFields,
        workflow: Workflow,
        zoho_status: str,
        zoho_payload: dict[str, Any],
    ) -> ProcessedEmail:
        now = datetime.now(UTC)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO processed_emails (
                    created_at, subject, sender, body, summary, extracted_json,
                    workflow_name, workflow_version, zoho_status, zoho_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    email.subject,
                    email.sender,
                    email.body,
                    summary,
                    extracted.to_json(),
                    workflow.name,
                    workflow.version,
                    zoho_status,
                    json.dumps(zoho_payload),
                ),
            )
            record_id = int(cursor.lastrowid)
        return ProcessedEmail(
            id=record_id,
            created_at=now,
            subject=email.subject,
            sender=email.sender,
            summary=summary,
            extracted=extracted,
            workflow_name=workflow.name,
            workflow_version=workflow.version,
            zoho_status=zoho_status,
            zoho_payload=zoho_payload,
        )

    def list_processed_emails(self) -> list[ProcessedEmail]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, subject, sender, summary, extracted_json,
                       workflow_name, workflow_version, zoho_status, zoho_payload_json
                FROM processed_emails
                ORDER BY id DESC
                LIMIT 50
                """
            ).fetchall()
        return [self._row_to_processed_email(row) for row in rows]

    def get_processed_email(self, processed_email_id: int) -> ProcessedEmail | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, created_at, subject, sender, summary, extracted_json,
                       workflow_name, workflow_version, zoho_status, zoho_payload_json
                FROM processed_emails
                WHERE id = ?
                """,
                (processed_email_id,),
            ).fetchone()
        return self._row_to_processed_email(row) if row else None

    def get_processed_email_body(self, processed_email_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT body
                FROM processed_emails
                WHERE id = ?
                """,
                (processed_email_id,),
            ).fetchone()
        return str(row["body"]) if row else None

    def update_processed_email_zoho(
        self,
        processed_email_id: int,
        zoho_status: str,
        zoho_payload: dict[str, Any],
        extracted: ExtractedFields | None = None,
    ) -> None:
        with self._connect() as conn:
            if extracted is None:
                conn.execute(
                    """
                    UPDATE processed_emails
                    SET zoho_status = ?,
                        zoho_payload_json = ?
                    WHERE id = ?
                    """,
                    (zoho_status, json.dumps(zoho_payload), processed_email_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE processed_emails
                    SET zoho_status = ?,
                        zoho_payload_json = ?,
                        extracted_json = ?
                    WHERE id = ?
                    """,
                    (
                        zoho_status,
                        json.dumps(zoho_payload),
                        extracted.to_json(),
                        processed_email_id,
                    ),
                )

    def save_oauth_token(self, provider: str, token: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_tokens (provider, updated_at, token_json)
                VALUES (?, ?, ?)
                ON CONFLICT(provider)
                DO UPDATE SET updated_at = excluded.updated_at,
                              token_json = excluded.token_json
                """,
                (provider, now, json.dumps(token)),
            )

    def get_oauth_token(self, provider: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT token_json
                FROM oauth_tokens
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["token_json"])

    def save_connector_settings(self, provider: str, settings: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_settings (provider, updated_at, settings_json)
                VALUES (?, ?, ?)
                ON CONFLICT(provider)
                DO UPDATE SET updated_at = excluded.updated_at,
                              settings_json = excluded.settings_json
                """,
                (provider, now, json.dumps(settings)),
            )

    def get_connector_settings(self, provider: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT settings_json
                FROM connector_settings
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["settings_json"])

    def delete_connector_settings(self, provider: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM connector_settings
                WHERE provider = ?
                """,
                (provider,),
            )

    def save_mail_message(
        self,
        provider: str,
        provider_message_id: str,
        received_at: str | None,
        subject: str,
        sender: str,
        body_preview: str,
        body: str,
    ) -> MailMessage:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mail_messages (
                    provider, provider_message_id, received_at, subject, sender,
                    body_preview, body, classification_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(provider, provider_message_id)
                DO UPDATE SET received_at = excluded.received_at,
                              subject = excluded.subject,
                              sender = excluded.sender,
                              body_preview = excluded.body_preview,
                              body = excluded.body,
                              updated_at = excluded.updated_at
                """,
                (
                    provider,
                    provider_message_id,
                    received_at,
                    subject,
                    sender,
                    body_preview,
                    body,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM mail_messages
                WHERE provider = ?
                  AND provider_message_id = ?
                """,
                (provider, provider_message_id),
            ).fetchone()
        return self._row_to_mail_message(row)

    def get_mail_message(self, mail_message_id: int) -> MailMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM mail_messages
                WHERE id = ?
                """,
                (mail_message_id,),
            ).fetchone()
        return self._row_to_mail_message(row) if row else None

    def list_mail_messages(self, limit: int = 50) -> list[MailMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM mail_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_mail_message(row) for row in rows]

    def update_mail_classification(
        self,
        mail_message_id: int,
        status: str,
        category: str | None,
        confidence: float | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mail_messages
                SET classification_status = ?,
                    classification_category = ?,
                    classification_confidence = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, category, confidence, now, mail_message_id),
            )

    def create_job(
        self,
        job_type: str,
        mail_message_id: int | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
        max_attempts: int = 3,
    ) -> Job:
        now = datetime.now(UTC).isoformat()
        payload = payload or {}
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    job_type, status, priority, attempts, max_attempts,
                    mail_message_id, payload_json, created_at, updated_at
                )
                VALUES (?, 'pending', ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_type,
                    priority,
                    max_attempts,
                    mail_message_id,
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO job_events (job_id, created_at, event_type, details_json)
                VALUES (?, ?, 'created', ?)
                """,
                (job_id, now, json.dumps({"job_type": job_type})),
            )
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("job was not created")
        return job

    def ensure_pending_job(
        self,
        job_type: str,
        mail_message_id: int,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
    ) -> Job:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE job_type = ?
                  AND mail_message_id = ?
                  AND status IN ('pending', 'running', 'completed')
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_type, mail_message_id),
            ).fetchone()
        if row:
            return self._row_to_job(row)
        return self.create_job(
            job_type=job_type,
            mail_message_id=mail_message_id,
            payload=payload,
            priority=priority,
        )

    def get_job(self, job_id: int) -> Job | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def claim_next_job(self) -> Job | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE status = 'pending'
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            conn.execute(
                """
                INSERT INTO job_events (job_id, created_at, event_type, details_json)
                VALUES (?, ?, 'claimed', '{}')
                """,
                (row["id"], now),
            )
        return self.get_job(int(row["id"]))

    def complete_job(self, job_id: int, details: dict[str, Any] | None = None) -> None:
        self._set_job_status(job_id, "completed", None, "completed", details or {})

    def fail_job(self, job: Job, error: str) -> None:
        status = "failed" if job.attempts >= job.max_attempts else "pending"
        self._set_job_status(job.id, status, error, "failed", {"error": error})

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def job_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM jobs
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _set_job_status(
        self,
        job_id: int,
        status: str,
        error: str | None,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, job_id),
            )
            conn.execute(
                """
                INSERT INTO job_events (job_id, created_at, event_type, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, now, event_type, json.dumps(details)),
            )

    def _row_to_processed_email(self, row: sqlite3.Row) -> ProcessedEmail:
        return ProcessedEmail(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            subject=row["subject"],
            sender=row["sender"],
            summary=row["summary"],
            extracted=ExtractedFields.from_json(row["extracted_json"]),
            workflow_name=row["workflow_name"],
            workflow_version=row["workflow_version"],
            zoho_status=row["zoho_status"],
            zoho_payload=json.loads(row["zoho_payload_json"]),
        )

    def _row_to_mail_message(self, row: sqlite3.Row) -> MailMessage:
        return MailMessage(
            id=row["id"],
            provider=row["provider"],
            provider_message_id=row["provider_message_id"],
            received_at=row["received_at"],
            subject=row["subject"],
            sender=row["sender"],
            body_preview=row["body_preview"],
            body=row["body"],
            classification_status=row["classification_status"],
            classification_category=row["classification_category"],
            classification_confidence=row["classification_confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            job_type=row["job_type"],
            status=row["status"],
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            mail_message_id=row["mail_message_id"],
            payload=json.loads(row["payload_json"]),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
