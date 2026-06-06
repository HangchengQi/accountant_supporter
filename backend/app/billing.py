from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schemas import ExtractedFields, MailMessage, ProcessedEmail


@dataclass(frozen=True)
class BillAttachment:
    name: str
    content: bytes


@dataclass(frozen=True)
class BillingArtifacts:
    billing_folder: Path
    saved_files: list[Path]
    daily_log: Path


def save_billing_artifacts(
    message: MailMessage,
    processed: ProcessedEmail,
    attachments: list[BillAttachment],
    bills_root: str,
    logs_root: str,
) -> BillingArtifacts:
    email_date = _email_date(message.received_at)
    billing_folder = Path(bills_root) / f"Billing{email_date}"
    billing_folder.mkdir(parents=True, exist_ok=True)

    saved_files = [
        _save_attachment(billing_folder, processed.extracted, attachment)
        for attachment in attachments
    ]
    daily_log = append_daily_log(Path(logs_root), email_date, message, processed, saved_files)
    return BillingArtifacts(
        billing_folder=billing_folder,
        saved_files=saved_files,
        daily_log=daily_log,
    )


def append_daily_log(
    logs_root: Path,
    email_date: str,
    message: MailMessage,
    processed: ProcessedEmail,
    saved_files: list[Path],
) -> Path:
    logs_root.mkdir(parents=True, exist_ok=True)
    log_path = logs_root / f"billing-log-{email_date}.md"
    extracted = processed.extracted
    amount = "" if extracted.amount is None else f"{extracted.currency or ''} {extracted.amount:,.2f}".strip()
    file_list = ", ".join(str(path) for path in saved_files) if saved_files else "No invoice attachment saved"
    entry = (
        f"## {processed.subject}\n"
        f"- Email date: {email_date}\n"
        f"- Sender: {processed.sender}\n"
        f"- Vendor: {extracted.vendor_name or 'Unknown'}\n"
        f"- Invoice date: {extracted.invoice_date or 'Unknown'}\n"
        f"- Invoice #: {extracted.invoice_number or 'Unknown'}\n"
        f"- Amount: {amount or 'Unknown'}\n"
        f"- Summary: {processed.summary}\n"
        f"- Saved files: {file_list}\n"
        f"- Source message ID: {message.provider_message_id}\n\n"
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    return log_path


def _save_attachment(folder: Path, extracted: ExtractedFields, attachment: BillAttachment) -> Path:
    extension = Path(attachment.name).suffix or ".bin"
    stem = "_".join(
        [
            _safe_part(extracted.vendor_name or "UnknownVendor"),
            _safe_part(extracted.invoice_date or "UnknownDate"),
            _safe_part(extracted.invoice_number or "NoInv"),
        ]
    )
    target = _available_path(folder / f"{stem}{extension}")
    target.write_bytes(attachment.content)
    return target


def _available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"unable to find available filename for {path.name}")


def _email_date(received_at: str | None) -> str:
    if not received_at:
        return datetime.now(UTC).date().isoformat()
    normalized = received_at.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return datetime.now(UTC).date().isoformat()


def _safe_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "Unknown"
