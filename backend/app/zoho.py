from __future__ import annotations

from typing import Any

from .schemas import EmailSampleIn, ExtractedFields
from .workflow import Workflow


class ZohoBooksClient:
    def create_draft_from_email(
        self,
        email: EmailSampleIn,
        extracted: ExtractedFields,
        workflow: Workflow,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class DryRunZohoBooksClient(ZohoBooksClient):
    def create_draft_from_email(
        self,
        email: EmailSampleIn,
        extracted: ExtractedFields,
        workflow: Workflow,
    ) -> tuple[str, dict[str, Any]]:
        payload = {
            "target_object": workflow.raw.get("zoho", {}).get("target_object", "bill_draft"),
            "vendor_name": extracted.vendor_name,
            "invoice_number": extracted.invoice_number,
            "invoice_date": extracted.invoice_date,
            "due_date": extracted.due_date,
            "amount": extracted.amount,
            "currency": extracted.currency,
            "source_email": {
                "subject": email.subject,
                "sender": email.sender,
            },
            "requires_review_before_real_upload": extracted.needs_review,
        }
        return "dry_run_created", payload
