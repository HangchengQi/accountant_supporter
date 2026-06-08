from __future__ import annotations

import html
import json
import mimetypes
import os
import secrets
import time
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .ai import (
    DEFAULT_OPENAI_CLASSIFICATION_MODEL,
    DEFAULT_OPENAI_MODEL,
    ai_status,
    create_ai_processor,
)
from .billing import BillAttachment, save_billing_artifacts
from .jobs import REVIEW_ACCOUNT, enqueue_mail_message, queue_status, run_queue_once
from .outlook import DeviceCodeSession, OutlookConfig, OutlookGraphClient
from .pdf_context import append_attachment_context, build_attachment_context
from .schemas import EmailSampleIn, ExtractedFields, MailMessage, ProcessedEmail
from .storage import SQLiteStorage
from .workflow import Workflow, load_workflow
from .zoho import DryRunZohoBooksClient, ZohoConfig, ZohoOAuthClient


ZOHO_BOOKS_API_ROOT = "https://www.zohoapis.com/books/v3"


def get_database_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/accountant_support.db")


def get_workflow_path() -> str:
    return os.getenv("WORKFLOW_PATH", "../workflows/vendor_invoice.v1.json")


def get_bills_root() -> str:
    return os.getenv("BILLS_ROOT", "./data/bills")


def get_logs_root() -> str:
    return os.getenv("BILLING_LOGS_ROOT", "./data/logs")


storage = SQLiteStorage(get_database_path())
zoho_client = DryRunZohoBooksClient()
pending_outlook_auth: DeviceCodeSession | None = None
pending_outlook_state: str | None = None
pending_zoho_state: str | None = None


def health() -> dict[str, str]:
    return {"status": "ok"}


def active_workflow() -> Workflow:
    return load_workflow(get_workflow_path())


def process_email_sample(email: EmailSampleIn) -> ProcessedEmail:
    workflow = load_workflow(get_workflow_path())
    ai_processor = create_ai_processor(storage.get_connector_settings("ai"))
    ai_email = _email_with_account_context(email)
    ai_result = ai_processor.process(ai_email, workflow)
    zoho_status, zoho_payload = _initial_zoho_state(ai_result.extracted)
    return storage.save_processed_email(
        email=ai_email,
        summary=ai_result.summary,
        extracted=ai_result.extracted,
        workflow=workflow,
        zoho_status=zoho_status,
        zoho_payload=zoho_payload,
    )


def process_mail_message(message: MailMessage) -> ProcessedEmail:
    attachments = []
    email = message.to_email()
    if message.provider == "outlook":
        attachments = _list_outlook_bill_attachments(message)
        email = _email_with_attachment_context(email, attachments)
    record = process_email_sample(email)
    if message.provider == "outlook":
        artifacts = _handle_outlook_billing_artifacts(message, record, attachments)
        record = _finalize_processed_email(record, artifacts.saved_files)
    return record


def _list_outlook_bill_attachments(message: MailMessage) -> list[BillAttachment]:
    token = get_outlook_token()
    client = get_outlook_client()
    return [
        BillAttachment(name=attachment.name, content=attachment.content)
        for attachment in client.list_file_attachments(token, message.provider_message_id)
    ]


def _email_with_attachment_context(
    email: EmailSampleIn,
    attachments: list[BillAttachment],
) -> EmailSampleIn:
    return EmailSampleIn(
        subject=email.subject,
        sender=email.sender,
        body=append_attachment_context(email.body, build_attachment_context(attachments)),
    )


def _handle_outlook_billing_artifacts(
    message: MailMessage,
    record: ProcessedEmail,
    attachments: list[BillAttachment],
) -> Any:
    token = get_outlook_token()
    client = get_outlook_client()
    artifacts = save_billing_artifacts(
        message=message,
        processed=record,
        attachments=attachments,
        bills_root=get_bills_root(),
        logs_root=get_logs_root(),
    )
    receiver = log_settings()["receiver_email"]
    if receiver:
        email_date = artifacts.daily_log.stem.replace("billing-log-", "", 1)
        try:
            client.send_mail(
                token=token,
                to_address=receiver,
                subject=f"Daily billing log {email_date}",
                body=artifacts.daily_log.read_text(encoding="utf-8"),
            )
        except Exception as exc:
            with artifacts.daily_log.open("a", encoding="utf-8") as f:
                f.write(f"- Log email send failed: {exc}\n\n")
    return artifacts


def _email_with_account_context(email: EmailSampleIn) -> EmailSampleIn:
    context = build_zoho_account_context()
    if not context:
        return email
    body = f"{email.body}\n\n---\n{context}"
    return EmailSampleIn(subject=email.subject, sender=email.sender, body=body)


def _initial_zoho_state(extracted: ExtractedFields) -> tuple[str, dict[str, Any]]:
    settings = approval_settings()
    return (
        "pending_approval",
        {
            "approval_reason": _approval_reason(extracted, settings),
            "approval_settings": settings,
            "saved_files": [],
        },
    )


def _finalize_processed_email(record: ProcessedEmail, saved_files: list[Path]) -> ProcessedEmail:
    payload = dict(record.zoho_payload)
    payload["saved_files"] = [str(path) for path in saved_files]
    settings = approval_settings()
    if _should_auto_upload(record.extracted, settings):
        try:
            uploaded_payload, updated_extracted = upload_record_to_zoho(record, saved_files)
            storage.update_processed_email_zoho(
                record.id,
                "uploaded",
                uploaded_payload,
                updated_extracted,
            )
            refreshed = storage.get_processed_email(record.id)
            return refreshed or record
        except Exception as exc:
            payload["upload_error"] = str(exc)
            storage.update_processed_email_zoho(record.id, "upload_failed", payload)
            refreshed = storage.get_processed_email(record.id)
            return refreshed or record
    payload["approval_reason"] = _approval_reason(record.extracted, settings)
    storage.update_processed_email_zoho(record.id, "pending_approval", payload)
    refreshed = storage.get_processed_email(record.id)
    return refreshed or record


def list_processed_emails() -> list[ProcessedEmail]:
    return storage.list_processed_emails()


def get_outlook_client() -> OutlookGraphClient:
    return OutlookGraphClient(
        OutlookConfig.from_settings(storage.get_connector_settings("outlook"))
    )


def get_zoho_oauth_client() -> ZohoOAuthClient:
    return ZohoOAuthClient(ZohoConfig.from_settings(storage.get_connector_settings("zoho")))


def outlook_status() -> dict[str, Any]:
    settings = storage.get_connector_settings("outlook") or {}
    client = get_outlook_client()
    required_scopes = ["offline_access", "User.Read", "Mail.Read", "Mail.Send"]
    configured_scopes = set(client.config.scopes.split())
    status = client.configured_status(
        has_token=storage.get_oauth_token("outlook") is not None
    )
    status["settings"] = {
        "client_id": client.config.client_id,
        "tenant_id": client.config.tenant_id,
        "account_type": _outlook_account_type(client.config.tenant_id),
        "scopes": client.config.scopes,
        "required_scopes": " ".join(required_scopes),
        "missing_scopes": [scope for scope in required_scopes if scope not in configured_scopes],
        "redirect_uri": client.config.redirect_uri,
        "saved_locally": bool(settings),
    }
    return status


def save_outlook_settings(data: dict[str, Any]) -> dict[str, Any]:
    existing = storage.get_connector_settings("outlook") or {}
    env_config = OutlookConfig.from_env()
    client_id = str(data.get("client_id", existing.get("client_id", env_config.client_id))).strip()
    account_type = str(data.get("account_type", "")).strip().lower()
    tenant_id = str(data.get("tenant_id", existing.get("tenant_id", env_config.tenant_id))).strip() or "common"
    if account_type in {"common", "organizations", "consumers"}:
        tenant_id = account_type
    elif account_type == "custom":
        tenant_id = tenant_id or "common"
    scopes = str(
        existing.get("scopes")
        or env_config.scopes
        or "offline_access User.Read Mail.Read Mail.Send"
    ).strip()
    for scope in ["offline_access", "User.Read", "Mail.Read", "Mail.Send"]:
        if scope not in scopes.split():
            scopes = f"{scopes} {scope}".strip()
    redirect_uri = str(
        existing.get("redirect_uri")
        or env_config.redirect_uri
        or "http://127.0.0.1:8080/auth/outlook/callback"
    ).strip()
    client_secret = str(existing.get("client_secret", env_config.client_secret)).strip()

    if not client_id:
        raise ValueError("client_id is required")
    if not tenant_id:
        raise ValueError("tenant_id is required")

    storage.save_connector_settings(
        "outlook",
        {
            "client_id": client_id,
            "tenant_id": tenant_id,
            "account_type": _outlook_account_type(tenant_id),
            "scopes": scopes,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    return outlook_status()


def _outlook_account_type(tenant_id: str) -> str:
    if tenant_id in {"common", "organizations", "consumers"}:
        return tenant_id
    return "custom"


def zoho_status() -> dict[str, Any]:
    client = get_zoho_oauth_client()
    status = client.configured_status(has_token=storage.get_oauth_token("zoho") is not None)
    status["settings"]["saved_locally"] = bool(storage.get_connector_settings("zoho"))
    return status


def save_zoho_settings(data: dict[str, Any]) -> dict[str, Any]:
    existing = storage.get_connector_settings("zoho") or {}
    env_config = ZohoConfig.from_env()
    client_id = str(data.get("client_id", existing.get("client_id", env_config.client_id))).strip()
    client_secret = str(data.get("client_secret", "")).strip() or str(
        existing.get("client_secret", env_config.client_secret)
    ).strip()
    redirect_uri = str(
        data.get(
            "redirect_uri",
            existing.get("redirect_uri", env_config.redirect_uri),
        )
    ).strip()
    scopes = str(
        data.get("scopes", existing.get("scopes", env_config.scopes))
    ).strip() or "ZohoBooks.fullaccess.all"

    if not client_id:
        raise ValueError("client_id is required")
    if not client_secret:
        raise ValueError("client_secret is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required")

    storage.save_connector_settings(
        "zoho",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
        },
    )
    return zoho_status()


def get_ai_status() -> dict[str, Any]:
    return ai_status(storage.get_connector_settings("ai"))


def save_ai_settings(data: dict[str, Any]) -> dict[str, Any]:
    existing = storage.get_connector_settings("ai") or {}
    provider = str(data.get("provider", existing.get("provider", "local"))).strip().lower()
    model = str(data.get("openai_model", existing.get("openai_model", DEFAULT_OPENAI_MODEL))).strip()
    classification_model = str(
        data.get(
            "openai_classification_model",
            existing.get("openai_classification_model", DEFAULT_OPENAI_CLASSIFICATION_MODEL),
        )
    ).strip()
    api_key = str(data.get("openai_api_key", "")).strip()
    clear_api_key = bool(data.get("clear_openai_api_key", False))

    if provider not in {"local", "openai", "chatgpt"}:
        raise ValueError("provider must be local, openai, or chatgpt")
    if not model:
        raise ValueError("openai_model is required")
    if not classification_model:
        raise ValueError("openai_classification_model is required")

    settings = {
        "provider": provider,
        "openai_model": model,
        "openai_classification_model": classification_model,
        "openai_api_key": "" if clear_api_key else api_key or existing.get("openai_api_key", ""),
    }
    storage.save_connector_settings("ai", settings)
    return get_ai_status()


def log_settings() -> dict[str, Any]:
    settings = storage.get_connector_settings("billing_log") or {}
    return {
        "receiver_email": settings.get("receiver_email", ""),
        "saved_locally": bool(settings),
    }


def approval_settings() -> dict[str, Any]:
    settings = storage.get_connector_settings("approval") or {}
    threshold = settings.get("account_confidence_threshold", 0.85)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.85
    threshold = max(0.0, min(1.0, threshold))
    return {
        "account_confidence_threshold": threshold,
        "manual_approval_required": bool(settings.get("manual_approval_required", False)),
        "saved_locally": bool(settings),
    }


def save_approval_settings(data: dict[str, Any]) -> dict[str, Any]:
    threshold = float(data.get("account_confidence_threshold", 0.85))
    threshold = max(0.0, min(1.0, threshold))
    manual = bool(data.get("manual_approval_required", False))
    storage.save_connector_settings(
        "approval",
        {
            "account_confidence_threshold": threshold,
            "manual_approval_required": manual,
        },
    )
    return approval_settings()


def mail_poll_settings() -> dict[str, Any]:
    settings = storage.get_connector_settings("mail_poll") or {}
    interval = settings.get("interval_minutes", 0)
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = 0
    interval = max(0.0, min(1440.0, interval))
    return {
        "interval_minutes": interval,
        "enabled": interval > 0,
        "saved_locally": bool(settings),
    }


def save_mail_poll_settings(data: dict[str, Any]) -> dict[str, Any]:
    interval = float(data.get("interval_minutes", 0))
    interval = max(0.0, min(1440.0, interval))
    storage.save_connector_settings("mail_poll", {"interval_minutes": interval})
    return mail_poll_settings()


def save_log_settings(data: dict[str, Any]) -> dict[str, Any]:
    receiver_email = str(data.get("receiver_email", "")).strip()
    if receiver_email and ("@" not in receiver_email or len(receiver_email) > 320):
        raise ValueError("receiver_email must be a valid email address")
    storage.save_connector_settings("billing_log", {"receiver_email": receiver_email})
    return log_settings()


def _should_auto_upload(extracted: ExtractedFields, settings: dict[str, Any]) -> bool:
    if settings["manual_approval_required"]:
        return False
    if not extracted.expense_account_name:
        return False
    if not extracted.vendor_name or not extracted.invoice_number or extracted.amount is None:
        return False
    return extracted.account_confidence >= settings["account_confidence_threshold"]


def _approval_reason(extracted: ExtractedFields, settings: dict[str, Any]) -> str:
    if settings["manual_approval_required"]:
        return "Manual approval is required by settings."
    if not extracted.expense_account_name:
        return "AI did not select an expense/account line account."
    if not extracted.vendor_name or not extracted.invoice_number or extracted.amount is None:
        return "Vendor, invoice number, or amount is missing."
    if extracted.account_confidence < settings["account_confidence_threshold"]:
        return (
            f"Account confidence {extracted.account_confidence:.2f} is below "
            f"threshold {settings['account_confidence_threshold']:.2f}."
        )
    return "Ready for automatic upload."


def get_zoho_token() -> dict[str, Any]:
    token = storage.get_oauth_token("zoho")
    if not token:
        raise ValueError("Zoho Books is not connected")
    if float(token.get("expires_at", 0)) <= time.time() + 60:
        token = get_zoho_oauth_client().refresh_token(token)
        storage.save_oauth_token("zoho", token)
    return token


def _zoho_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = get_zoho_token()
    url = f"{ZOHO_BOOKS_API_ROOT}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Zoho-oauthtoken {token['access_token']}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise ValueError(f"Zoho Books request failed: HTTP {exc.code} {body}") from exc


def zoho_organization() -> dict[str, Any]:
    response = _zoho_request("GET", "/organizations")
    organizations = response.get("organizations", [])
    if not organizations:
        raise ValueError("Zoho Books has no organization")
    return next((org for org in organizations if org.get("is_default_org")), organizations[0])


def zoho_expense_accounts() -> list[dict[str, Any]]:
    org_id = str(zoho_organization()["organization_id"])
    response = _zoho_request("GET", "/chartofaccounts", query={"organization_id": org_id})
    accounts = response.get("chartofaccounts") or response.get("accounts") or []
    result = []
    for account in accounts:
        name = str(account.get("account_name") or account.get("name") or "").strip()
        account_type = str(account.get("account_type") or "").lower()
        if account.get("is_active") is False or not name:
            continue
        if "expense" in account_type or "cost_of_goods_sold" in account_type or "expense" in name.lower():
            result.append(
                {
                    "account_id": str(account.get("account_id", "")),
                    "account_name": name,
                    "account_type": account.get("account_type", ""),
                }
            )
    return result


def build_zoho_account_context() -> str:
    if not storage.get_oauth_token("zoho"):
        return ""
    try:
        accounts = zoho_expense_accounts()
    except Exception:
        return ""
    if not accounts:
        return ""
    lines = [
        "Available Zoho Books expense/account line accounts. Choose exactly one account_name when possible:"
    ]
    for account in accounts[:80]:
        lines.append(
            f"- {account['account_name']} | account_id={account['account_id']} | type={account['account_type']}"
        )
    return "\n".join(lines)


def _match_zoho_account(extracted: ExtractedFields) -> dict[str, Any]:
    accounts = zoho_expense_accounts()
    wanted = (extracted.expense_account_name or "").strip().lower()
    for account in accounts:
        if account["account_name"].strip().lower() == wanted:
            return account
    for account in accounts:
        if wanted and wanted in account["account_name"].strip().lower():
            return account
    raise ValueError(f"Zoho account not found: {extracted.expense_account_name or 'missing'}")


def _find_or_create_zoho_vendor(org_id: str, vendor_name: str) -> dict[str, Any]:
    response = _zoho_request(
        "GET",
        "/contacts",
        query={
            "organization_id": org_id,
            "contact_type": "vendor",
            "contact_name_contains": vendor_name[:100],
        },
    )
    for contact in response.get("contacts", []):
        if str(contact.get("contact_name", "")).strip().lower() == vendor_name.strip().lower():
            return contact
    created = _zoho_request(
        "POST",
        "/contacts",
        payload={
            "contact_name": vendor_name,
            "company_name": vendor_name,
            "contact_type": "vendor",
            "notes": "Created by Accountant Supporter local MVP from invoice email processing.",
        },
        query={"organization_id": org_id},
    )
    return created["contact"]


def upload_record_to_zoho(
    record: ProcessedEmail,
    saved_files: list[Path] | None = None,
) -> tuple[dict[str, Any], ExtractedFields]:
    org = zoho_organization()
    org_id = str(org["organization_id"])
    extracted = record.extracted
    if not extracted.vendor_name:
        raise ValueError("vendor_name is required for Zoho upload")
    if not extracted.invoice_number:
        raise ValueError("invoice_number is required for Zoho upload")
    if extracted.amount is None:
        raise ValueError("amount is required for Zoho upload")
    invoice_date = _zoho_date(extracted.invoice_date)
    due_date = _zoho_date(extracted.due_date)
    account = _match_zoho_account(extracted)
    vendor = _find_or_create_zoho_vendor(org_id, extracted.vendor_name)
    updated_extracted = replace(
        extracted,
        invoice_date=invoice_date,
        due_date=due_date,
        expense_account_id=account["account_id"],
    )
    bill_payload = {
        "vendor_id": str(vendor["contact_id"]),
        "bill_number": extracted.invoice_number,
        "date": invoice_date,
        "due_date": due_date,
        "notes": record.summary,
        "line_items": [
            {
                "description": f"Imported invoice {extracted.invoice_number} from {extracted.vendor_name}",
                "account_id": account["account_id"],
                "rate": extracted.amount,
                "quantity": 1,
            }
        ],
    }
    bill_response = _zoho_request(
        "POST",
        "/bills",
        payload=bill_payload,
        query={"organization_id": org_id},
    )
    bill = bill_response.get("bill", {})
    bill_id = str(bill.get("bill_id", ""))
    if not bill_id:
        raise ValueError(f"Zoho did not return bill_id: {bill_response}")
    attachments = []
    for path in saved_files or _saved_files_from_payload(record.zoho_payload):
        attachments.append(_upload_zoho_bill_attachment(org_id, bill_id, path))
    return (
        {
            "organization": {"organization_id": org_id, "name": org.get("name")},
            "vendor": {
                "contact_id": vendor.get("contact_id"),
                "contact_name": vendor.get("contact_name"),
            },
            "account": account,
            "bill": {
                "bill_id": bill_id,
                "bill_number": bill.get("bill_number"),
                "status": bill.get("status"),
                "total": bill.get("total"),
            },
            "attachments": attachments,
            "saved_files": [str(path) for path in (saved_files or _saved_files_from_payload(record.zoho_payload))],
        },
        updated_extracted,
    )


def _zoho_date(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(cleaned, date_format).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Invalid invoice date format for Zoho Books: {value}")


def _saved_files_from_payload(payload: dict[str, Any]) -> list[Path]:
    return [Path(path) for path in payload.get("saved_files", []) if path]


def _upload_zoho_bill_attachment(org_id: str, bill_id: str, path: Path) -> dict[str, Any]:
    token = get_zoho_token()
    boundary = f"----accountant-supporter-{uuid4().hex}"
    file_bytes = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="attachment"; filename="{path.name}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = Request(
        f"{ZOHO_BOOKS_API_ROOT}/bills/{bill_id}/attachment?{urlencode({'organization_id': org_id})}",
        data=body,
        headers={
            "Authorization": f"Zoho-oauthtoken {token['access_token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8")
        raise ValueError(f"Zoho attachment upload failed: HTTP {exc.code} {body_text}") from exc


def start_outlook_redirect_auth() -> str:
    global pending_outlook_state
    outlook_client = get_outlook_client()
    pending_outlook_state = secrets.token_urlsafe(24)
    return outlook_client.authorization_url(pending_outlook_state)


def complete_outlook_redirect_auth(query: str) -> str:
    global pending_outlook_state
    params = parse_qs(query)
    if "error" in params:
        description = params.get("error_description", params["error"])[0]
        return auth_result_page("Mail connection failed", description, False)
    code = params.get("code", [""])[0]
    state = params.get("state", [""])[0]
    if not code:
        return auth_result_page("Mail connection failed", "Missing authorization code.", False)
    if not pending_outlook_state or state != pending_outlook_state:
        return auth_result_page("Mail connection failed", "Invalid authorization state.", False)

    token = get_outlook_client().exchange_authorization_code(code)
    storage.save_oauth_token("outlook", token)
    pending_outlook_state = None
    return auth_result_page(
        "Mail connected",
        "Outlook is connected. Return to Accountant Supporter to fetch messages.",
        True,
    )


def start_zoho_redirect_auth() -> str:
    global pending_zoho_state
    zoho_oauth = get_zoho_oauth_client()
    pending_zoho_state = secrets.token_urlsafe(24)
    return zoho_oauth.authorization_url(pending_zoho_state)


def complete_zoho_redirect_auth(query: str) -> str:
    global pending_zoho_state
    params = parse_qs(query)
    if "error" in params:
        return auth_result_page("Zoho Books connection failed", params["error"][0], False)
    code = params.get("code", [""])[0]
    state = params.get("state", [""])[0]
    if not code:
        return auth_result_page("Zoho Books connection failed", "Missing authorization code.", False)
    if not pending_zoho_state or state != pending_zoho_state:
        return auth_result_page("Zoho Books connection failed", "Invalid authorization state.", False)

    token = get_zoho_oauth_client().exchange_authorization_code(code)
    storage.save_oauth_token("zoho", token)
    pending_zoho_state = None
    return auth_result_page(
        "Zoho Books connected",
        "Zoho Books is connected. Return to Accountant Supporter to continue.",
        True,
    )


def start_outlook_auth() -> dict[str, Any]:
    global pending_outlook_auth
    outlook_client = get_outlook_client()
    pending_outlook_auth = outlook_client.start_device_code()
    return pending_outlook_auth.to_dict()


def poll_outlook_auth() -> dict[str, Any]:
    global pending_outlook_auth
    if pending_outlook_auth is None:
        return {"status": "not_started"}
    outlook_client = get_outlook_client()
    result = outlook_client.poll_device_code(pending_outlook_auth)
    if result.get("status") == "connected":
        storage.save_oauth_token("outlook", result["token"])
        pending_outlook_auth = None
    return {key: value for key, value in result.items() if key != "token"}


def get_outlook_token() -> dict[str, Any]:
    token = storage.get_oauth_token("outlook")
    if not token:
        raise ValueError("Outlook is not connected")
    if float(token.get("expires_at", 0)) <= time.time() + 60:
        token = get_outlook_client().refresh_token(token)
        storage.save_oauth_token("outlook", token)
    return token


def list_outlook_messages(top: int = 10) -> list[dict[str, Any]]:
    token = get_outlook_token()
    return [
        message.to_dict()
        for message in get_outlook_client().list_inbox_messages(token=token, top=top)
    ]


def ingest_outlook_messages(top: int = 10) -> dict[str, Any]:
    token = get_outlook_token()
    messages = get_outlook_client().list_inbox_messages(token=token, top=top)
    ingested: list[dict[str, Any]] = []
    for message in messages:
        stored = storage.save_mail_message(
            provider="outlook",
            provider_message_id=message.id,
            received_at=message.received_at,
            subject=message.subject,
            sender=message.sender,
            body_preview=message.body_preview,
            body=message.body,
        )
        job = enqueue_mail_message(storage, stored)
        item = message.to_dict()
        item["mail_message_id"] = stored.id
        item["classification_status"] = stored.classification_status
        item["classification_job_id"] = job.id
        ingested.append(item)
    return {"messages": ingested, "ingested": len(ingested)}


def run_jobs(max_jobs: int = 10) -> dict[str, Any]:
    return run_queue_once(
        storage,
        process_mail_message,
        process_account_review,
        max_jobs=max_jobs,
        ai_settings=storage.get_connector_settings("ai"),
    ).to_dict()


def approve_processed_email(processed_email_id: int) -> dict[str, Any]:
    record = storage.get_processed_email(processed_email_id)
    if record is None:
        raise ValueError("processed email not found")
    payload, extracted = upload_record_to_zoho(record)
    storage.update_processed_email_zoho(record.id, "uploaded", payload, extracted)
    refreshed = storage.get_processed_email(record.id)
    return refreshed.to_dict() if refreshed else record.to_dict()


def reject_processed_email(processed_email_id: int, suggested_account: str) -> dict[str, Any]:
    suggested_account = suggested_account.strip()
    if not suggested_account:
        raise ValueError("suggested_account is required")
    record = storage.get_processed_email(processed_email_id)
    if record is None:
        raise ValueError("processed email not found")
    superseded_payload = dict(record.zoho_payload)
    superseded_payload["superseded_by"] = "review_account"
    superseded_payload["reviewer_suggestion"] = suggested_account
    storage.update_processed_email_zoho(processed_email_id, "superseded", superseded_payload)
    job = storage.create_job(
        job_type=REVIEW_ACCOUNT,
        payload={
            "processed_email_id": processed_email_id,
            "suggested_account": suggested_account,
        },
        priority=30,
    )
    result = run_queue_once(
        storage,
        process_mail_message,
        process_account_review,
        max_jobs=1,
        ai_settings=storage.get_connector_settings("ai"),
    )
    return {"job": job.to_dict(), "queue_run": result.to_dict()}


def discard_processed_email(processed_email_id: int, reason: str = "") -> dict[str, Any]:
    record = storage.get_processed_email(processed_email_id)
    if record is None:
        raise ValueError("processed email not found")
    payload = dict(record.zoho_payload)
    payload["discard_reason"] = reason.strip() or "Discarded by user."
    payload["discarded_at"] = datetime.now().isoformat()
    storage.update_processed_email_zoho(record.id, "discarded", payload)
    refreshed = storage.get_processed_email(record.id)
    return refreshed.to_dict() if refreshed else record.to_dict()


def process_account_review(payload: dict[str, Any]) -> ProcessedEmail:
    processed_id = int(payload["processed_email_id"])
    suggestion = str(payload["suggested_account"]).strip()
    record = storage.get_processed_email(processed_id)
    body = storage.get_processed_email_body(processed_id)
    if record is None or body is None:
        raise ValueError("processed email not found")
    review_instruction = (
        "\n\n---\nReviewer account suggestion: "
        f"{suggestion}\n"
        "Highest priority instruction for expense/account line account: use the reviewer suggestion as the selected account when it matches an available Zoho account name or is clearly equivalent to one. Do not override it with your previous recommendation unless the suggestion is impossible to map to any available account. Return a fresh account confidence and reason that explains how the suggestion was applied."
    )
    email = EmailSampleIn(
        subject=record.subject,
        sender=record.sender,
        body=f"{body}{review_instruction}",
    )
    new_record = process_email_sample(email)
    payload_update = dict(new_record.zoho_payload)
    payload_update["reviewed_from_processed_email_id"] = processed_id
    payload_update["reviewer_suggestion"] = suggestion
    payload_update["saved_files"] = list(record.zoho_payload.get("saved_files", []))
    storage.update_processed_email_zoho(new_record.id, new_record.zoho_status, payload_update)
    refreshed = storage.get_processed_email(new_record.id)
    if refreshed and _should_auto_upload(refreshed.extracted, approval_settings()):
        payload_uploaded, extracted = upload_record_to_zoho(refreshed)
        storage.update_processed_email_zoho(refreshed.id, "uploaded", payload_uploaded, extracted)
        return storage.get_processed_email(refreshed.id) or refreshed
    return refreshed or new_record


def page_shell(title: str, body: str, active: str = "") -> str:
    nav = f"""
      <nav>
        <a class="{_active(active, 'home')}" href="/">Connections</a>
        <a class="{_active(active, 'processing')}" href="/processing">Processing</a>
      </nav>
    """
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <style>{base_css()}</style>
      </head>
      <body>
        <header>
          <div>
            <h1>Accountant Supporter</h1>
            <p>Local-first bookkeeping workflow connections and processing.</p>
          </div>
          {nav}
        </header>
        {body}
      </body>
    </html>
    """


def index() -> str:
    body = """
      <main class="connection-main">
        <section class="connection-card">
          <div class="connector-head">
            <div>
              <h2>Mail Authentication</h2>
              <p>Connect Outlook so the app can read selected mailbox messages for summarization.</p>
            </div>
            <span class="pill" id="outlook-pill">Checking</span>
          </div>
          <label for="outlook-account-type">Outlook Account Type</label>
          <select id="outlook-account-type">
            <option value="personal">Personal Outlook account</option>
            <option value="common">Microsoft app: work, school, or personal</option>
            <option value="organizations">Microsoft app: work or school only</option>
            <option value="custom">Microsoft app: specific business tenant</option>
          </select>
          <div class="notice" id="outlook-mode-note"></div>
          <div class="toolbar" id="outlook-connect-toolbar">
            <button type="button" id="outlook-device-connect">Connect Outlook</button>
            <a class="button-link secondary" id="outlook-redirect-connect" href="/auth/outlook/start" target="_blank" rel="noopener noreferrer">Redirect sign-in</a>
          </div>
          <div class="notice" id="outlook-personal-action" style="display:none"></div>
          <div class="status" id="outlook-status">Checking status...</div>
          <div class="notice" id="outlook-config"></div>
          <div class="notice" id="outlook-device-code" style="display:none"></div>
          <div id="outlook-app-fields">
            <label for="outlook-client-id">Microsoft App Client ID</label>
            <input id="outlook-client-id" autocomplete="off" placeholder="Application (client) ID from app registration" />
            <label for="outlook-tenant-id">Tenant or Login Audience</label>
            <input id="outlook-tenant-id" autocomplete="off" placeholder="common or tenant ID" />
            <div class="toolbar">
              <button class="secondary" type="button" id="outlook-save">Save Outlook settings</button>
            </div>
          </div>
          <div class="status" id="outlook-save-status"></div>
        </section>

        <section class="connection-card">
          <div class="connector-head">
            <div>
              <h2>Zoho Books Authentication</h2>
              <p>Connect Zoho Books so approved invoice drafts can be uploaded through the accounting workflow.</p>
            </div>
            <span class="pill" id="zoho-pill">Checking</span>
          </div>
          <div class="toolbar">
            <a class="button-link" id="zoho-connect" href="/auth/zoho/start">Connect Zoho Books</a>
          </div>
          <div class="status" id="zoho-status">Checking status...</div>
          <div class="notice" id="zoho-config"></div>
          <label for="zoho-client-id">Zoho Client ID</label>
          <input id="zoho-client-id" autocomplete="off" placeholder="Zoho API Console client ID" />
          <label for="zoho-client-secret">Zoho Client Secret</label>
          <input id="zoho-client-secret" type="password" autocomplete="off" placeholder="Paste secret to save or rotate" />
          <label for="zoho-redirect-uri">Zoho Redirect URI</label>
          <input id="zoho-redirect-uri" autocomplete="off" placeholder="http://127.0.0.1:8080/auth/zoho/callback" />
          <label for="zoho-scopes">Zoho Scopes</label>
          <input id="zoho-scopes" autocomplete="off" placeholder="ZohoBooks.fullaccess.all" />
          <div class="toolbar">
            <button class="secondary" type="button" id="zoho-save">Save Zoho settings</button>
          </div>
          <div class="status" id="zoho-save-status"></div>
        </section>

        <section class="connection-card">
          <div class="connector-head">
            <div>
              <h2>ChatGPT Processing</h2>
              <p>Connect OpenAI so summaries and invoice-field extraction use ChatGPT.</p>
            </div>
            <span class="pill" id="ai-pill">Checking</span>
          </div>
          <div class="status" id="ai-status">Checking status...</div>
          <label for="ai-provider">AI Provider</label>
          <input id="ai-provider" autocomplete="off" placeholder="local or openai" />
          <label for="openai-model">OpenAI Model</label>
          <input id="openai-model" autocomplete="off" placeholder="gpt-5.5" />
          <label for="openai-classification-model">Classification Model</label>
          <input id="openai-classification-model" autocomplete="off" placeholder="gpt-5.4-mini" />
          <label for="openai-api-key">OpenAI API Key</label>
          <input id="openai-api-key" type="password" autocomplete="off" placeholder="Paste key to save or rotate" />
          <div class="toolbar">
            <button type="button" id="ai-save">Save ChatGPT settings</button>
            <button class="secondary" type="button" id="ai-clear-key">Clear saved key</button>
          </div>
          <div class="notice" id="ai-config"></div>
        </section>

        <section class="connection-card">
          <div class="connector-head">
            <div>
              <h2>Daily Billing Log</h2>
              <p>Send the generated billing log to a receiver after bill emails are processed.</p>
            </div>
            <span class="pill" id="log-pill">Checking</span>
          </div>
          <label for="log-receiver">Log Receiver</label>
          <input id="log-receiver" type="email" autocomplete="off" placeholder="billing-log@example.com" />
          <div class="toolbar">
            <button class="secondary" type="button" id="log-save">Save log receiver</button>
          </div>
          <div class="status" id="log-status">Daily billing log receiver is not set.</div>
        </section>

        <section class="connection-card">
          <div class="connector-head">
            <div>
              <h2>Approval Rules</h2>
              <p>Control when AI-selected Zoho expense accounts can auto-upload.</p>
            </div>
            <span class="pill" id="approval-pill">Checking</span>
          </div>
          <label for="account-threshold">Account Confidence Threshold</label>
          <input id="account-threshold" type="number" min="0" max="1" step="0.01" />
          <label class="checkbox-row" for="manual-approval">
            <input id="manual-approval" type="checkbox" />
            Require manual approval for every invoice
          </label>
          <div class="toolbar">
            <button class="secondary" type="button" id="approval-save">Save approval rules</button>
          </div>
          <div class="status" id="approval-status">Approval rules are loading.</div>
        </section>
      </main>
      <script>
        async function refreshConnectionStatus() {
          await Promise.all([refreshOutlookStatus(), refreshZohoStatus(), refreshAIStatus(), refreshLogSettings(), refreshApprovalSettings()]);
        }

        async function refreshOutlookStatus() {
          const response = await fetch('/api/outlook/status');
          const status = await response.json();
          const target = document.getElementById('outlook-status');
          const pill = document.getElementById('outlook-pill');
          const config = document.getElementById('outlook-config');
          document.getElementById('outlook-client-id').value = status.settings?.client_id || '';
          document.getElementById('outlook-account-type').value =
            status.configured ? (status.settings?.account_type || 'common') : 'personal';
          document.getElementById('outlook-tenant-id').value = status.settings?.tenant_id || 'common';
          syncOutlookMode();
          if (!status.configured) {
            target.textContent = 'Mail authentication is not configured for direct local OAuth.';
            config.textContent = 'Personal Outlook mailboxes do not have a Client ID. Use the plugin-backed workflow for MVP testing, or choose a Microsoft app mode if you have an app registration.';
            pill.textContent = 'Not configured';
            pill.className = 'pill';
            return;
          }
          const missingScopes = status.settings?.missing_scopes || [];
          const scopeMessage = missingScopes.length
            ? ` Missing scopes: ${missingScopes.join(', ')}. Reconnect Outlook after updating scopes.`
            : '';
          config.textContent =
            `Redirect URI: ${status.settings?.redirect_uri || ''} | Required scopes: ${status.settings?.required_scopes || ''}.${scopeMessage}`;
          pill.textContent = status.connected ? 'Connected' : 'Configured';
          pill.className = status.connected ? 'pill ok' : 'pill';
          target.textContent = status.connected ? 'Outlook is connected' : 'Ready to connect Outlook';
        }

        function syncOutlookMode() {
          const accountType = document.getElementById('outlook-account-type').value;
          const fields = document.getElementById('outlook-app-fields');
          const tenant = document.getElementById('outlook-tenant-id');
          const clientId = document.getElementById('outlook-client-id');
          const note = document.getElementById('outlook-mode-note');
          const deviceConnect = document.getElementById('outlook-device-connect');
          const redirectConnect = document.getElementById('outlook-redirect-connect');
          const connectToolbar = document.getElementById('outlook-connect-toolbar');
          const personalAction = document.getElementById('outlook-personal-action');
          if (accountType === 'personal') {
            fields.style.display = 'none';
            clientId.value = '';
            tenant.value = 'consumers';
            note.textContent = 'A personal Outlook mailbox has no Client ID. Direct local OAuth needs a Microsoft app registration owned by the app; for MVP testing, use the already connected Outlook plugin workflow.';
            connectToolbar.style.display = 'none';
            personalAction.style.display = 'block';
            personalAction.textContent = 'Direct local Outlook connection is unavailable in personal mailbox mode. The MVP can still read your personal Outlook through the connected Codex Outlook plugin during testing.';
            deviceConnect.disabled = true;
            redirectConnect.removeAttribute('href');
            redirectConnect.setAttribute('aria-disabled', 'true');
            redirectConnect.className = 'button-link secondary disabled';
            return;
          }
          fields.style.display = 'block';
          connectToolbar.style.display = 'flex';
          personalAction.style.display = 'none';
          deviceConnect.disabled = false;
          redirectConnect.setAttribute('href', '/auth/outlook/start');
          redirectConnect.removeAttribute('aria-disabled');
          redirectConnect.className = 'button-link secondary';
          if (accountType === 'custom') {
            tenant.disabled = false;
            tenant.placeholder = 'Business tenant ID';
            note.textContent = 'Use this for a single business tenant app registration.';
            return;
          }
          tenant.value = accountType;
          tenant.disabled = true;
          tenant.placeholder = accountType;
          if (accountType === 'common') {
            note.textContent = 'Use this when your Microsoft app registration supports work, school, and personal Microsoft accounts.';
            return;
          }
          note.textContent = 'Use this when your Microsoft app registration supports work or school accounts only.';
        }

        document.getElementById('outlook-account-type').addEventListener('change', syncOutlookMode);

        document.getElementById('outlook-device-connect').addEventListener('click', async () => {
          const response = await fetch('/api/outlook/auth/start', { method: 'POST' });
          const result = await response.json();
          const codePanel = document.getElementById('outlook-device-code');
          if (!response.ok) {
            codePanel.style.display = 'block';
            codePanel.textContent = result.error || 'Unable to start Outlook sign-in';
            return;
          }
          codePanel.style.display = 'block';
          codePanel.innerHTML =
            `Open Microsoft sign-in and enter code <strong>${result.user_code}</strong>. ` +
            `<a href="${result.verification_uri}" target="_blank" rel="noopener noreferrer">Open sign-in page</a>`;
          window.open(result.verification_uri, '_blank', 'noopener,noreferrer');
          pollOutlookDeviceCode(result.interval || 5);
        });

        async function pollOutlookDeviceCode(intervalSeconds) {
          const codePanel = document.getElementById('outlook-device-code');
          const poll = async () => {
            const response = await fetch('/api/outlook/auth/poll', { method: 'POST' });
            const result = await response.json();
            if (!response.ok) {
              codePanel.textContent = result.error || 'Unable to finish Outlook sign-in';
              return;
            }
            if (result.status === 'connected') {
              codePanel.textContent = 'Outlook connected.';
              await refreshOutlookStatus();
              return;
            }
            if (result.status === 'expired') {
              codePanel.textContent = 'Outlook sign-in expired. Click Connect Outlook again.';
              return;
            }
            setTimeout(poll, Math.max(2, intervalSeconds) * 1000);
          };
          setTimeout(poll, Math.max(2, intervalSeconds) * 1000);
        }

        document.getElementById('outlook-save').addEventListener('click', async () => {
          if (document.getElementById('outlook-account-type').value === 'personal') {
            document.getElementById('outlook-save-status').textContent =
              'No Outlook app settings are required for a personal mailbox. Use plugin-backed import until a Microsoft app registration is available.';
            return;
          }
          const response = await fetch('/api/outlook/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              client_id: document.getElementById('outlook-client-id').value,
              account_type: document.getElementById('outlook-account-type').value,
              tenant_id: document.getElementById('outlook-tenant-id').value
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('outlook-save-status').textContent = result.error || 'Unable to save Outlook settings';
            return;
          }
          document.getElementById('outlook-save-status').textContent = 'Outlook settings saved locally.';
          await refreshOutlookStatus();
        });

        async function refreshZohoStatus() {
          const response = await fetch('/api/zoho/status');
          const status = await response.json();
          const target = document.getElementById('zoho-status');
          const pill = document.getElementById('zoho-pill');
          const config = document.getElementById('zoho-config');
          const connect = document.getElementById('zoho-connect');
          document.getElementById('zoho-client-id').value = status.settings?.client_id || '';
          document.getElementById('zoho-client-secret').value = '';
          document.getElementById('zoho-redirect-uri').value =
            status.settings?.redirect_uri || 'http://127.0.0.1:8080/auth/zoho/callback';
          document.getElementById('zoho-scopes').value = status.settings?.scopes || 'ZohoBooks.fullaccess.all';
          if (!status.configured) {
            target.textContent = 'Zoho Books authentication is not configured by the app admin yet.';
            config.textContent = 'Enter the Zoho Client ID, Client Secret, and Redirect URI, then save.';
            pill.textContent = 'Not configured';
            pill.className = 'pill';
            connect.removeAttribute('href');
            connect.setAttribute('aria-disabled', 'true');
            connect.className = 'button-link disabled';
            return;
          }
          connect.setAttribute('href', '/auth/zoho/start');
          connect.removeAttribute('aria-disabled');
          connect.className = 'button-link';
          config.textContent =
            `US Zoho login: ${status.login_url} | Redirect URI: ${status.redirect_uri}. ` +
            `Client secret is ${status.settings?.has_client_secret ? 'saved locally and hidden' : 'not saved'}.`;
          pill.textContent = status.connected ? 'Connected' : 'Configured';
          pill.className = status.connected ? 'pill ok' : 'pill';
          target.textContent = status.connected ? 'Zoho Books is connected' : 'Ready to connect Zoho Books';
        }

        document.getElementById('zoho-save').addEventListener('click', async () => {
          const response = await fetch('/api/zoho/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              client_id: document.getElementById('zoho-client-id').value,
              client_secret: document.getElementById('zoho-client-secret').value,
              redirect_uri: document.getElementById('zoho-redirect-uri').value,
              scopes: document.getElementById('zoho-scopes').value
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('zoho-save-status').textContent = result.error || 'Unable to save Zoho settings';
            return;
          }
          document.getElementById('zoho-save-status').textContent = 'Zoho settings saved locally.';
          await refreshZohoStatus();
        });

        async function refreshAIStatus() {
          const response = await fetch('/api/ai/status');
          const status = await response.json();
          const target = document.getElementById('ai-status');
          const pill = document.getElementById('ai-pill');
          const config = document.getElementById('ai-config');
          document.getElementById('ai-provider').value = status.settings?.provider || 'local';
          document.getElementById('openai-model').value = status.settings?.openai_model || 'gpt-5.5';
          document.getElementById('openai-classification-model').value =
            status.settings?.openai_classification_model || 'gpt-5.4-mini';
          document.getElementById('openai-api-key').value = '';
          if (status.active_provider === 'openai') {
            pill.textContent = 'OpenAI';
            pill.className = 'pill ok';
            target.textContent = 'ChatGPT/OpenAI processing is active';
            config.textContent =
              `Models: classify_email=${status.job_models?.classify_email || 'rules'}, process_email=${status.job_models?.process_email || status.model}. API key is saved locally and hidden.`;
            return;
          }
          pill.textContent = 'Local';
          pill.className = 'pill';
          target.textContent = 'Using local heuristic processing';
          config.textContent = status.settings?.has_openai_api_key
            ? 'API key is saved locally. Set provider to openai to activate ChatGPT processing.'
            : 'Set provider to openai and save an API key to enable ChatGPT processing.';
        }

        document.getElementById('ai-save').addEventListener('click', async () => {
          const response = await fetch('/api/ai/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              provider: document.getElementById('ai-provider').value,
              openai_model: document.getElementById('openai-model').value,
              openai_classification_model: document.getElementById('openai-classification-model').value,
              openai_api_key: document.getElementById('openai-api-key').value,
              clear_openai_api_key: false
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('ai-status').textContent = result.error || 'Unable to save AI settings';
            return;
          }
          await refreshAIStatus();
        });

        document.getElementById('ai-clear-key').addEventListener('click', async () => {
          const response = await fetch('/api/ai/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              provider: document.getElementById('ai-provider').value,
              openai_model: document.getElementById('openai-model').value,
              openai_classification_model: document.getElementById('openai-classification-model').value,
              clear_openai_api_key: true
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('ai-status').textContent = result.error || 'Unable to clear AI key';
            return;
          }
          await refreshAIStatus();
        });

        async function refreshLogSettings() {
          const response = await fetch('/api/log/settings');
          const settings = await response.json();
          const pill = document.getElementById('log-pill');
          document.getElementById('log-receiver').value = settings.receiver_email || '';
          pill.textContent = settings.receiver_email ? 'Configured' : 'Not set';
          pill.className = settings.receiver_email ? 'pill ok' : 'pill';
          document.getElementById('log-status').textContent = settings.receiver_email
            ? `Daily billing logs will be sent to ${settings.receiver_email}.`
            : 'Daily billing log receiver is not set.';
        }

        document.getElementById('log-save').addEventListener('click', async () => {
          const response = await fetch('/api/log/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              receiver_email: document.getElementById('log-receiver').value
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('log-status').textContent = result.error || 'Unable to save log receiver';
            return;
          }
          await refreshLogSettings();
        });

        async function refreshApprovalSettings() {
          const response = await fetch('/api/approval/settings');
          const settings = await response.json();
          document.getElementById('account-threshold').value = settings.account_confidence_threshold ?? 0.85;
          document.getElementById('manual-approval').checked = Boolean(settings.manual_approval_required);
          const pill = document.getElementById('approval-pill');
          const status = document.getElementById('approval-status');
          pill.textContent = settings.manual_approval_required ? 'Manual' : 'Auto when confident';
          pill.className = settings.manual_approval_required ? 'pill' : 'pill ok';
          status.textContent = settings.manual_approval_required
            ? 'Every processed invoice will wait for approval.'
            : `Invoices auto-upload when account confidence is at least ${Number(settings.account_confidence_threshold || 0).toFixed(2)}.`;
        }

        document.getElementById('approval-save').addEventListener('click', async () => {
          const response = await fetch('/api/approval/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              account_confidence_threshold: document.getElementById('account-threshold').value,
              manual_approval_required: document.getElementById('manual-approval').checked
            })
          });
          const result = await response.json();
          if (!response.ok) {
            document.getElementById('approval-status').textContent = result.error || 'Unable to save approval rules';
            return;
          }
          await refreshApprovalSettings();
        });

        refreshConnectionStatus();
      </script>
    """
    return page_shell("Accountant Supporter Connections", body, active="home")


def processing_page() -> str:
    records = storage.list_processed_emails()
    pending_records = [record for record in records if record.zoho_status in {"pending_approval", "upload_failed"}]
    history_records = [record for record in records if record.zoho_status not in {"pending_approval", "upload_failed"}]
    pending_items = "\n".join(render_record(record, section="pending") for record in pending_records)
    history_items = "\n".join(render_record(record, section="history") for record in history_records)
    body = f"""
      <main>
        <section>
          <div class="connector">
            <div class="connector-head">
              <div>
                <h2>Mail Messages</h2>
                <p>Fetch connected Outlook messages into the local queue for classification and processing.</p>
              </div>
              <span class="pill" id="outlook-pill">Checking</span>
            </div>
            <div class="toolbar">
              <button class="secondary" type="button" id="outlook-fetch">Fetch inbox into queue</button>
              <button class="secondary" type="button" id="queue-run">Run queue</button>
            </div>
            <div class="poll-settings">
              <div class="filter-field">
                <label for="mail-poll-interval">Auto-fetch interval (minutes)</label>
                <input id="mail-poll-interval" type="number" min="0" max="1440" step="0.25" placeholder="0 disables auto-fetch" />
              </div>
              <button class="secondary" type="button" id="mail-poll-save">Save auto-fetch</button>
            </div>
            <div class="status" id="outlook-status">Checking status...</div>
            <div class="status" id="mail-poll-status">Auto-fetch settings are loading.</div>
            <div class="notice" id="queue-status">Queue status will appear here.</div>
            <div id="outlook-messages"></div>
          </div>
        </section>
        <section class="records">
          <div class="connector-head">
            <div>
              <h2>Pending Approval</h2>
              <p>Invoices waiting for account review or upload approval.</p>
            </div>
            <span class="pill">{len(pending_records)} pending</span>
          </div>
          <div id="pending-records">{pending_items or "<p>No invoices are waiting for approval.</p>"}</div>
          <div class="history-head">
            <h2>Approval History</h2>
            <p>Completed approvals, automatic submissions, and reviewed items.</p>
          </div>
          <div class="history-filters">
            <div class="filter-field">
              <label for="history-status-filter">Status</label>
              <select id="history-status-filter">
                <option value="all">All history</option>
                <option value="manual_review">Review required</option>
                <option value="automated_submission">Automated submission</option>
                <option value="manual_submission">Manual submission</option>
                <option value="failed">Failed submission</option>
                <option value="discarded">Discarded</option>
              </select>
            </div>
            <div class="filter-field">
              <label for="history-from-filter">From timestamp</label>
              <input id="history-from-filter" type="datetime-local" />
            </div>
            <div class="filter-field">
              <label for="history-to-filter">To timestamp</label>
              <input id="history-to-filter" type="datetime-local" />
            </div>
          </div>
          <div class="toolbar">
            <button class="secondary" type="button" id="history-clear-filters">Clear filters</button>
          </div>
          <div class="status" id="history-filter-status"></div>
          <div id="history-records">{history_items or "<p>No approval history yet.</p>"}</div>
        </section>
      </main>
      <script>
        let outlookMessages = [];
        let outlookConnected = false;
        let mailPollTimer = null;
        let mailPollRunning = false;
        const escapeHtml = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({{
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
        }}[char]));

        async function outlookStatus() {{
          const response = await fetch('/api/outlook/status');
          const status = await response.json();
          const target = document.getElementById('outlook-status');
          const pill = document.getElementById('outlook-pill');
          outlookConnected = Boolean(status.connected);
          pill.textContent = status.connected ? 'Connected' : 'Not connected';
          pill.className = status.connected ? 'pill ok' : 'pill';
          target.textContent = status.connected ? 'Outlook is connected' : 'Connect Outlook on the Connections page first';
          return status;
        }}

        async function fetchInboxIntoQueue() {{
          const response = await fetch('/api/outlook/messages?top=5');
          const result = await response.json();
          if (!response.ok) {{
            document.getElementById('outlook-messages').textContent = result.error || 'Unable to fetch messages';
            throw new Error(result.error || 'Unable to fetch messages');
          }}
          outlookMessages = result.messages || [];
          await refreshQueueStatus();
          document.getElementById('outlook-messages').innerHTML = outlookMessages.map((message, index) => `
            <article>
              <strong>${{escapeHtml(message.subject)}}</strong>
              <div class="meta">${{escapeHtml(message.sender)}} | ${{escapeHtml(message.received_at || '')}}</div>
              <p>${{escapeHtml(message.body_preview)}}</p>
              <span class="pill">Queued</span>
            </article>
          `).join('') || '<p>No messages found.</p>';
          return result;
        }}

        async function runQueue() {{
          const response = await fetch('/api/jobs/run', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ max_jobs: 10 }})
          }});
          const result = await response.json();
          if (!response.ok) {{
            document.getElementById('queue-status').textContent = result.error || 'Unable to run queue';
            throw new Error(result.error || 'Unable to run queue');
          }}
          document.getElementById('queue-status').textContent =
            `Queue run: claimed ${{result.claimed}}, completed ${{result.completed}}, created processing jobs ${{result.created_processing_jobs}}, skipped ${{result.skipped_irrelevant}}.`;
          if (result.completed > 0) {{
            window.location.reload();
          }}
          return result;
        }}

        document.getElementById('outlook-fetch').addEventListener('click', async () => {{
          try {{
            await fetchInboxIntoQueue();
          }} catch (error) {{
            document.getElementById('mail-poll-status').textContent = error.message;
          }}
        }});

        document.getElementById('queue-run').addEventListener('click', async () => {{
          try {{
            await runQueue();
          }} catch (error) {{
            document.getElementById('mail-poll-status').textContent = error.message;
          }}
        }});

        async function refreshMailPollSettings() {{
          const response = await fetch('/api/mail-poll/settings');
          const settings = await response.json();
          document.getElementById('mail-poll-interval').value = settings.interval_minutes || 0;
          configureMailPoll(settings.interval_minutes || 0);
        }}

        function configureMailPoll(intervalMinutes) {{
          if (mailPollTimer) {{
            clearInterval(mailPollTimer);
            mailPollTimer = null;
          }}
          const status = document.getElementById('mail-poll-status');
          const interval = Number(intervalMinutes || 0);
          if (!interval) {{
            status.textContent = 'Auto-fetch is disabled. Set a minute interval above 0 to enable it.';
            return;
          }}
          if (!outlookConnected) {{
            status.textContent = `Auto-fetch every ${{interval}} minute${{interval === 1 ? '' : 's'}} is saved and will start after Outlook connects.`;
          }} else {{
            status.textContent = `Auto-fetch is active every ${{interval}} minute${{interval === 1 ? '' : 's'}}.`;
          }}
          mailPollTimer = setInterval(runMailPollOnce, Math.max(15000, interval * 60 * 1000));
        }}

        async function runMailPollOnce() {{
          if (mailPollRunning) {{
            return;
          }}
          mailPollRunning = true;
          const status = document.getElementById('mail-poll-status');
          status.textContent = 'Auto-fetch running...';
          try {{
            const outlook = await outlookStatus();
            if (!outlook.connected) {{
              status.textContent = 'Auto-fetch is waiting for Outlook connection.';
              return;
            }}
            const fetched = await fetchInboxIntoQueue();
            const queued = await runQueue();
            status.textContent =
              `Auto-fetch complete: ingested ${{fetched.ingested || 0}}, completed ${{queued.completed || 0}} jobs.`;
          }} catch (error) {{
            status.textContent = error.message || 'Auto-fetch failed.';
          }} finally {{
            mailPollRunning = false;
          }}
        }}

        document.getElementById('mail-poll-save').addEventListener('click', async () => {{
          const interval = document.getElementById('mail-poll-interval').value;
          const response = await fetch('/api/mail-poll/settings', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ interval_minutes: interval }})
          }});
          const result = await response.json();
          if (!response.ok) {{
            document.getElementById('mail-poll-status').textContent = result.error || 'Unable to save auto-fetch settings';
            return;
          }}
          configureMailPoll(result.interval_minutes || 0);
        }});

        document.querySelector('.records').addEventListener('click', async (event) => {{
          const approveButton = event.target.closest('.approve-record');
          const rejectButton = event.target.closest('.reject-record');
          const discardButton = event.target.closest('.discard-record');
          if (!approveButton && !rejectButton && !discardButton) {{
            return;
          }}
          const recordId = (approveButton || rejectButton || discardButton).dataset.recordId;
          const status = document.getElementById(`approval-status-${{recordId}}`);
          if (approveButton) {{
            status.textContent = 'Uploading to Zoho...';
            const response = await fetch(`/api/processed-emails/${{recordId}}/approve`, {{ method: 'POST' }});
            const result = await response.json();
            if (!response.ok) {{
              status.textContent = result.error || 'Upload failed';
              return;
            }}
            window.location.reload();
            return;
          }}
          if (discardButton) {{
            status.textContent = 'Discarding request...';
            const response = await fetch(`/api/processed-emails/${{recordId}}/discard`, {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ reason: 'Discarded from Pending Approval.' }})
            }});
            const result = await response.json();
            if (!response.ok) {{
              status.textContent = result.error || 'Unable to discard request';
              return;
            }}
            window.location.reload();
            return;
          }}
          const input = document.getElementById(`suggested-account-${{recordId}}`);
          const suggestion = input.value.trim();
          if (!suggestion) {{
            status.textContent = 'Enter an expense/account line account suggestion first.';
            return;
          }}
          status.textContent = 'Re-running account classification...';
          const response = await fetch(`/api/processed-emails/${{recordId}}/reject`, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ suggested_account: suggestion }})
          }});
          const result = await response.json();
          if (!response.ok) {{
            status.textContent = result.error || 'Unable to re-run classification';
            return;
          }}
          window.location.reload();
        }});

        function applyHistoryFilters() {{
          const statusFilter = document.getElementById('history-status-filter').value;
          const fromValue = document.getElementById('history-from-filter').value;
          const toValue = document.getElementById('history-to-filter').value;
          const fromTime = fromValue ? new Date(fromValue).getTime() : null;
          const toTime = toValue ? new Date(toValue).getTime() : null;
          let visible = 0;
          document.querySelectorAll('#history-records article').forEach((article) => {{
            const statusGroup = article.dataset.statusGroup || 'all';
            const createdAt = new Date(article.dataset.createdAt || '').getTime();
            const statusMatches = statusFilter === 'all' || statusFilter === statusGroup;
            const fromMatches = !fromTime || (!Number.isNaN(createdAt) && createdAt >= fromTime);
            const toMatches = !toTime || (!Number.isNaN(createdAt) && createdAt <= toTime);
            const show = statusMatches && fromMatches && toMatches;
            article.style.display = show ? '' : 'none';
            if (show) {{
              visible += 1;
            }}
          }});
          document.getElementById('history-filter-status').textContent =
            `${{visible}} history item${{visible === 1 ? '' : 's'}} shown.`;
        }}

        ['history-status-filter', 'history-from-filter', 'history-to-filter'].forEach((id) => {{
          document.getElementById(id).addEventListener('change', applyHistoryFilters);
        }});

        document.getElementById('history-clear-filters').addEventListener('click', () => {{
          document.getElementById('history-status-filter').value = 'all';
          document.getElementById('history-from-filter').value = '';
          document.getElementById('history-to-filter').value = '';
          applyHistoryFilters();
        }});

        async function refreshQueueStatus() {{
          const response = await fetch('/api/jobs/status');
          const status = await response.json();
          if (!response.ok) {{
            return;
          }}
          const counts = status.job_counts || {{}};
          document.getElementById('queue-status').textContent =
            `Jobs: pending ${{counts.pending || 0}}, running ${{counts.running || 0}}, completed ${{counts.completed || 0}}, failed ${{counts.failed || 0}}.`;
        }}

        outlookStatus().then(refreshMailPollSettings);
        refreshQueueStatus();
        applyHistoryFilters();
      </script>
    """
    return page_shell("Accountant Supporter Processing", body, active="processing")


class AccountantSupportHandler(BaseHTTPRequestHandler):
    server_version = "AccountantSupport/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(index())
            return
        if path == "/processing":
            self._send_html(processing_page())
            return
        if path == "/auth/outlook/start":
            self._auth_redirect(start_outlook_redirect_auth, "Mail setup needed")
            return
        if path == "/auth/outlook/callback":
            self._send_html(complete_outlook_redirect_auth(urlparse(self.path).query))
            return
        if path == "/auth/zoho/start":
            self._auth_redirect(start_zoho_redirect_auth, "Zoho Books setup needed")
            return
        if path == "/auth/zoho/callback":
            self._send_html(complete_zoho_redirect_auth(urlparse(self.path).query))
            return
        if path == "/health":
            self._send_json(health())
            return
        if path == "/api/workflow":
            workflow = active_workflow()
            self._send_json(workflow.public_status())
            return
        if path == "/api/processed-emails":
            self._send_json([record.to_dict() for record in list_processed_emails()])
            return
        if path == "/api/outlook/status":
            self._send_json(outlook_status())
            return
        if path == "/api/zoho/status":
            self._send_json(zoho_status())
            return
        if path == "/api/ai/status":
            self._send_json(get_ai_status())
            return
        if path == "/api/log/settings":
            self._send_json(log_settings())
            return
        if path == "/api/approval/settings":
            self._send_json(approval_settings())
            return
        if path == "/api/mail-poll/settings":
            self._send_json(mail_poll_settings())
            return
        if path == "/api/outlook/messages":
            self._send_outlook_messages()
            return
        if path == "/api/jobs/status":
            self._send_json(queue_status(storage))
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/outlook/auth/start":
            try:
                self._send_json(start_outlook_auth())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/outlook/auth/poll":
            try:
                self._send_json(poll_outlook_auth())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/outlook/settings":
            try:
                self._send_json(save_outlook_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/zoho/settings":
            try:
                self._send_json(save_zoho_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/ai/settings":
            try:
                self._send_json(save_ai_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/log/settings":
            try:
                self._send_json(save_log_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/approval/settings":
            try:
                self._send_json(save_approval_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/mail-poll/settings":
            try:
                self._send_json(save_mail_poll_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path.startswith("/api/processed-emails/") and path.endswith("/approve"):
            try:
                processed_id = int(path.split("/")[3])
                self._send_json(approve_processed_email(processed_id))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path.startswith("/api/processed-emails/") and path.endswith("/reject"):
            try:
                processed_id = int(path.split("/")[3])
                payload = self._read_json()
                self._send_json(
                    reject_processed_email(
                        processed_id,
                        str(payload.get("suggested_account", "")),
                    )
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path.startswith("/api/processed-emails/") and path.endswith("/discard"):
            try:
                processed_id = int(path.split("/")[3])
                payload = self._read_json()
                self._send_json(
                    discard_processed_email(
                        processed_id,
                        str(payload.get("reason", "")),
                    )
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/jobs/run":
            try:
                payload = self._read_json()
                max_jobs = int(payload.get("max_jobs", 10))
                self._send_json(run_jobs(max_jobs=max_jobs))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path != "/api/email-samples/process":
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json()
            email = EmailSampleIn.from_dict(payload)
            record = process_email_sample(email)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json(
                {"error": "processing failed", "detail": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self._send_json(record.to_dict(), status=HTTPStatus.CREATED)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _auth_redirect(self, factory: Any, error_title: str) -> None:
        try:
            self._redirect(factory())
        except Exception as exc:
            self._send_html(
                auth_result_page(error_title, str(exc), False),
                status=HTTPStatus.BAD_REQUEST,
            )

    def _send_outlook_messages(self) -> None:
        query = urlparse(self.path).query
        top = 10
        if query.startswith("top="):
            try:
                top = int(query.split("=", 1)[1])
            except ValueError:
                top = 10
        try:
            self._send_json(ingest_outlook_messages(top=top))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON object is required")
        return payload

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", "0")
        self.end_headers()


def render_record(record: ProcessedEmail, section: str = "history") -> str:
    extracted = record.extracted
    review = '<span class="badge">Review required</span>' if extracted.needs_review else ""
    approval = '<span class="badge">Approval pending</span>' if record.zoho_status in {"pending_approval", "upload_failed"} else ""
    amount = "" if extracted.amount is None else f"{extracted.currency or ''} {extracted.amount:,.2f}"
    account_confidence = f"{extracted.account_confidence:.2f}"
    status_group = _approval_status_group(record)
    status_label = _approval_status_label(record)
    actions = ""
    if record.zoho_status in {"pending_approval", "upload_failed"}:
        reason = html.escape(str(record.zoho_payload.get("approval_reason") or record.zoho_payload.get("upload_error") or "Review required."))
        actions = f"""
          <div class="approval-actions" data-record-id="{record.id}">
            <div class="notice">{reason}</div>
            <div class="toolbar">
              <button type="button" class="approve-record" data-record-id="{record.id}">Approve and upload to Zoho</button>
              <button type="button" class="discard-record secondary" data-record-id="{record.id}">Discard request</button>
            </div>
            <label for="suggested-account-{record.id}">Suggested expense/account line account</label>
            <input id="suggested-account-{record.id}" class="suggested-account" autocomplete="off" placeholder="Example: Repairs and Maintenance" />
            <div class="toolbar">
              <button type="button" class="reject-record secondary" data-record-id="{record.id}">No, re-run with suggestion</button>
            </div>
            <div class="status" id="approval-status-{record.id}"></div>
          </div>
        """
    return f"""
      <article data-status-group="{html.escape(status_group)}" data-created-at="{html.escape(record.created_at.isoformat())}">
        <strong>{html.escape(record.subject)}</strong>
        {review}
        {approval}
        <div class="meta">{html.escape(record.sender)} | {html.escape(record.created_at.isoformat())} | workflow v{record.workflow_version}</div>
        <div class="meta">Approval status: {html.escape(status_label)}</div>
        <p>{html.escape(record.summary)}</p>
        <dl>
          <dt>Category</dt><dd>{html.escape(extracted.category)}</dd>
          <dt>Vendor</dt><dd>{html.escape(extracted.vendor_name or "")}</dd>
          <dt>Invoice #</dt><dd>{html.escape(extracted.invoice_number or "")}</dd>
          <dt>Invoice date</dt><dd>{html.escape(extracted.invoice_date or "")}</dd>
          <dt>Due date</dt><dd>{html.escape(extracted.due_date or "")}</dd>
          <dt>Amount</dt><dd>{html.escape(amount)}</dd>
          <dt>Confidence</dt><dd>{extracted.confidence:.2f}</dd>
          <dt>Account</dt><dd>{html.escape(extracted.expense_account_name or "")}</dd>
          <dt>Account confidence</dt><dd>{html.escape(account_confidence)}</dd>
          <dt>Account reason</dt><dd>{html.escape(extracted.account_reason or "")}</dd>
        </dl>
        {actions}
      </article>
    """


def _approval_status_group(record: ProcessedEmail) -> str:
    if record.zoho_status == "uploaded":
        reason = str(record.zoho_payload.get("approval_reason", ""))
        if reason or record.zoho_payload.get("reviewed_from_processed_email_id"):
            return "manual_submission"
        return "automated_submission"
    if record.zoho_status == "upload_failed":
        return "failed"
    if record.zoho_status == "discarded":
        return "discarded"
    if record.zoho_status == "superseded":
        return "manual_review"
    if record.zoho_status == "pending_approval":
        return "manual_review"
    if record.extracted.needs_review:
        return "manual_review"
    return "automated_submission"


def _approval_status_label(record: ProcessedEmail) -> str:
    labels = {
        "manual_review": "Review required",
        "automated_submission": "Automated submission",
        "manual_submission": "Manual submission",
        "failed": "Failed submission",
        "discarded": "Discarded",
    }
    if record.zoho_status == "pending_approval":
        return "Review required"
    if record.zoho_status == "upload_failed":
        return "Failed submission"
    if record.zoho_status == "discarded":
        return "Discarded"
    if record.zoho_status == "superseded":
        return "Superseded by reviewer suggestion"
    return labels.get(_approval_status_group(record), record.zoho_status.replace("_", " ").title())


def auth_result_page(title: str, message: str, success: bool) -> str:
    color = "#176b3e" if success else "#8a2700"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <style>{base_css()}</style>
      </head>
      <body>
        <main class="auth-result">
          <section class="connection-card">
            <h1 style="color: {color}">{html.escape(title)}</h1>
            <p>{html.escape(message)}</p>
            <p><a href="/">Return to connections</a></p>
          </section>
        </main>
      </body>
    </html>
    """


def _active(current: str, value: str) -> str:
    return "active" if current == value else ""


def base_css() -> str:
    return """
      :root {
        color-scheme: light;
        --ink: #172026;
        --muted: #61717d;
        --line: #d8e0e6;
        --panel: #f7f9fb;
        --soft: #eef4f2;
        --accent: #176b5d;
        --accent-dark: #0f4d43;
        --warn: #8a5a00;
        --ok: #176b3e;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: var(--ink);
        background: #ffffff;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 22px;
        border-bottom: 1px solid var(--line);
        padding: 22px clamp(18px, 4vw, 52px);
        background: #fbfcfd;
      }
      nav { display: flex; flex-wrap: wrap; gap: 8px; }
      nav a {
        border: 1px solid var(--line);
        border-radius: 6px;
        color: var(--ink);
        padding: 8px 10px;
        text-decoration: none;
        font-weight: 700;
        font-size: 14px;
      }
      nav a.active { background: var(--accent); border-color: var(--accent); color: #ffffff; }
      main {
        display: grid;
        grid-template-columns: minmax(360px, 0.92fr) minmax(420px, 1.08fr);
        gap: 28px;
        padding: 28px clamp(18px, 4vw, 52px);
      }
      .connection-main {
        grid-template-columns: repeat(2, minmax(320px, 1fr));
        align-items: start;
      }
      .auth-result {
        display: block;
        max-width: 720px;
        margin: 10vh auto 0;
      }
      h1 { margin: 0; font-size: 24px; }
      h2 { margin: 0 0 14px; font-size: 18px; }
      p { color: var(--muted); line-height: 1.5; }
      form, .records, .connection-card {
        border: 1px solid var(--line);
        border-radius: 8px;
        background: var(--panel);
        padding: 18px;
      }
      .connector, .connection-card {
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #ffffff;
        padding: 18px;
      }
      .connector { margin-top: 18px; }
      .connector-head {
        display: flex;
        align-items: start;
        justify-content: space-between;
        gap: 14px;
      }
      .connector-head p { margin: 2px 0 0; font-size: 13px; }
      label {
        display: block;
        margin: 14px 0 6px;
        color: #31404a;
        font-weight: 650;
        font-size: 14px;
      }
      .checkbox-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .checkbox-row input { width: auto; }
      input, select, textarea {
        width: 100%;
        border: 1px solid #bdc9d1;
        border-radius: 6px;
        padding: 10px 12px;
        font: inherit;
        background: #ffffff;
      }
      textarea { min-height: 240px; resize: vertical; }
      button, .button-link {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        margin-top: 16px;
        border: 0;
        border-radius: 6px;
        padding: 10px 14px;
        background: var(--accent);
        color: #ffffff;
        font-weight: 700;
        font: inherit;
        text-decoration: none;
        cursor: pointer;
      }
      button:hover, .button-link:hover { background: var(--accent-dark); }
      .button-link.disabled {
        background: #d8e0e6;
        color: #61717d;
        cursor: not-allowed;
        pointer-events: none;
      }
      button.secondary {
        background: #eef3f5;
        color: var(--ink);
        border: 1px solid #c9d4db;
      }
      button.secondary:hover { background: #e0e9ed; }
      .toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
      .poll-settings {
        display: grid;
        grid-template-columns: minmax(190px, 260px) auto;
        gap: 10px;
        align-items: end;
        margin: 10px 0;
      }
      .approval-actions { margin-top: 14px; }
      .history-head {
        border-top: 1px solid var(--line);
        margin-top: 22px;
        padding-top: 18px;
      }
      .history-head p { margin: 2px 0 0; font-size: 13px; }
      .history-filters {
        display: grid;
        grid-template-columns: minmax(180px, 1.1fr) minmax(190px, 1fr) minmax(190px, 1fr);
        gap: 12px;
        align-items: start;
        margin-top: 10px;
      }
      .filter-field {
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-width: 0;
      }
      .filter-field label { margin: 0; }
      .filter-field input,
      .filter-field select {
        min-height: 42px;
      }
      .status { color: var(--muted); font-size: 13px; min-height: 20px; }
      .pill {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        border: 1px solid var(--line);
        padding: 3px 9px;
        font-size: 12px;
        font-weight: 700;
        color: var(--muted);
        white-space: nowrap;
      }
      .pill.ok { color: var(--ok); border-color: #a6d4ba; background: #eef8f2; }
      .notice {
        border: 1px solid #cddbd8;
        border-radius: 8px;
        background: var(--soft);
        padding: 10px 12px;
        margin-top: 12px;
        font-size: 13px;
        color: #31404a;
      }
      article { border-top: 1px solid var(--line); padding: 14px 0; }
      article:first-child { border-top: 0; padding-top: 0; }
      .meta { color: var(--muted); font-size: 13px; margin: 4px 0 8px; }
      .badge {
        display: inline-block;
        border: 1px solid #d6bd7c;
        color: var(--warn);
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 12px;
        font-weight: 700;
      }
      dl {
        display: grid;
        grid-template-columns: 130px 1fr;
        gap: 5px 12px;
        margin: 10px 0 0;
        font-size: 14px;
      }
      dt { color: var(--muted); }
      dd { margin: 0; }
      @media (max-width: 840px) {
        header { align-items: stretch; flex-direction: column; }
        main, .connection-main { grid-template-columns: 1fr; }
        .poll-settings { grid-template-columns: 1fr; }
        .history-filters { grid-template-columns: 1fr; }
      }
    """


def run() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), AccountantSupportHandler)
    print(f"Accountant Support running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
