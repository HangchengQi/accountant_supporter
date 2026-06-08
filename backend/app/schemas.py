from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EmailSampleIn:
    subject: str
    sender: str
    body: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailSampleIn":
        subject = str(data.get("subject", "")).strip()
        sender = str(data.get("sender", "")).strip()
        body = str(data.get("body", "")).strip()
        if not subject:
            raise ValueError("subject is required")
        if not sender:
            raise ValueError("sender is required")
        if not body:
            raise ValueError("body is required")
        if len(subject) > 300 or len(sender) > 300:
            raise ValueError("subject and sender must be 300 characters or less")
        return cls(subject=subject, sender=sender, body=body)


@dataclass(frozen=True)
class MailMessage:
    id: int
    provider: str
    provider_message_id: str
    received_at: str | None
    subject: str
    sender: str
    body_preview: str
    body: str
    classification_status: str
    classification_category: str | None
    classification_confidence: float | None
    created_at: datetime
    updated_at: datetime

    def to_email(self) -> EmailSampleIn:
        return EmailSampleIn(subject=self.subject, sender=self.sender, body=self.body or self.body_preview)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        result["updated_at"] = self.updated_at.isoformat()
        return result


@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    mail_message_id: int | None
    payload: dict[str, Any]
    error: str | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        result["updated_at"] = self.updated_at.isoformat()
        return result


@dataclass(frozen=True)
class ExtractedFields:
    category: str
    confidence: float
    needs_review: bool
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    amount: float | None = None
    currency: str | None = None
    expense_account_name: str | None = None
    expense_account_id: str | None = None
    account_confidence: float = 0.0
    account_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "ExtractedFields":
        data = json.loads(value)
        data.setdefault("expense_account_name", None)
        data.setdefault("expense_account_id", None)
        data.setdefault("account_confidence", 0.0)
        data.setdefault("account_reason", None)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessedEmail:
    id: int
    created_at: datetime
    subject: str
    sender: str
    summary: str
    extracted: ExtractedFields
    workflow_name: str
    workflow_version: int
    zoho_status: str
    zoho_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["created_at"] = self.created_at.isoformat()
        result["extracted"] = self.extracted.to_dict()
        return result
