from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import EmailSampleIn, ExtractedFields, ProcessedEmail
from .workflow import Workflow


class SQLiteStorage:
    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

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
