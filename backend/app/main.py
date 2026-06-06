from __future__ import annotations

import html
import json
import os
import secrets
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .ai import (
    DEFAULT_OPENAI_CLASSIFICATION_MODEL,
    DEFAULT_OPENAI_MODEL,
    ai_status,
    create_ai_processor,
)
from .billing import BillAttachment, save_billing_artifacts
from .jobs import enqueue_mail_message, queue_status, run_queue_once
from .outlook import DeviceCodeSession, OutlookConfig, OutlookGraphClient
from .schemas import EmailSampleIn, MailMessage, ProcessedEmail
from .storage import SQLiteStorage
from .workflow import Workflow, load_workflow
from .zoho import DryRunZohoBooksClient, ZohoConfig, ZohoOAuthClient


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
    ai_result = ai_processor.process(email, workflow)
    zoho_status, zoho_payload = zoho_client.create_draft_from_email(
        email,
        ai_result.extracted,
        workflow,
    )
    return storage.save_processed_email(
        email=email,
        summary=ai_result.summary,
        extracted=ai_result.extracted,
        workflow=workflow,
        zoho_status=zoho_status,
        zoho_payload=zoho_payload,
    )


def process_mail_message(message: MailMessage) -> ProcessedEmail:
    record = process_email_sample(message.to_email())
    if message.provider == "outlook":
        _handle_outlook_billing_artifacts(message, record)
    return record


def _handle_outlook_billing_artifacts(message: MailMessage, record: ProcessedEmail) -> None:
    token = get_outlook_token()
    client = get_outlook_client()
    attachments = [
        BillAttachment(name=attachment.name, content=attachment.content)
        for attachment in client.list_file_attachments(token, message.provider_message_id)
    ]
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


def list_processed_emails() -> list[ProcessedEmail]:
    return storage.list_processed_emails()


def get_outlook_client() -> OutlookGraphClient:
    return OutlookGraphClient(
        OutlookConfig.from_settings(storage.get_connector_settings("outlook"))
    )


def get_zoho_oauth_client() -> ZohoOAuthClient:
    return ZohoOAuthClient(ZohoConfig.from_env())


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
    return client.configured_status(has_token=storage.get_oauth_token("zoho") is not None)


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


def save_log_settings(data: dict[str, Any]) -> dict[str, Any]:
    receiver_email = str(data.get("receiver_email", "")).strip()
    if receiver_email and ("@" not in receiver_email or len(receiver_email) > 320):
        raise ValueError("receiver_email must be a valid email address")
    storage.save_connector_settings("billing_log", {"receiver_email": receiver_email})
    return log_settings()


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
        max_jobs=max_jobs,
        ai_settings=storage.get_connector_settings("ai"),
    ).to_dict()


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
          <div class="toolbar">
            <button type="button" id="outlook-device-connect">Connect Outlook</button>
            <a class="button-link secondary" href="/auth/outlook/start" target="_blank" rel="noopener noreferrer">Redirect sign-in</a>
          </div>
          <div class="status" id="outlook-status">Checking status...</div>
          <div class="notice" id="outlook-config"></div>
          <div class="notice" id="outlook-device-code" style="display:none"></div>
          <label for="outlook-client-id">Outlook Client ID</label>
          <input id="outlook-client-id" autocomplete="off" placeholder="Microsoft app client ID" />
          <label for="outlook-account-type">Outlook Login Type</label>
          <select id="outlook-account-type">
            <option value="common">Common login: work, school, or personal Microsoft</option>
            <option value="organizations">Work or school accounts only</option>
            <option value="consumers">Personal Microsoft accounts only</option>
            <option value="custom">Specific business tenant</option>
          </select>
          <label for="outlook-tenant-id">Outlook Tenant ID</label>
          <input id="outlook-tenant-id" autocomplete="off" placeholder="common or tenant ID" />
          <div class="toolbar">
            <button class="secondary" type="button" id="outlook-save">Save Outlook settings</button>
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
      </main>
      <script>
        async function refreshConnectionStatus() {
          await Promise.all([refreshOutlookStatus(), refreshZohoStatus(), refreshAIStatus(), refreshLogSettings()]);
        }

        async function refreshOutlookStatus() {
          const response = await fetch('/api/outlook/status');
          const status = await response.json();
          const target = document.getElementById('outlook-status');
          const pill = document.getElementById('outlook-pill');
          const config = document.getElementById('outlook-config');
          document.getElementById('outlook-client-id').value = status.settings?.client_id || '';
          document.getElementById('outlook-account-type').value = status.settings?.account_type || 'common';
          document.getElementById('outlook-tenant-id').value = status.settings?.tenant_id || 'common';
          syncOutlookTenantInput();
          if (!status.configured) {
            target.textContent = 'Mail authentication is not configured by the app admin yet.';
            config.textContent = 'Enter the Outlook Client ID and Tenant ID, then save.';
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

        function syncOutlookTenantInput() {
          const accountType = document.getElementById('outlook-account-type').value;
          const tenant = document.getElementById('outlook-tenant-id');
          if (accountType === 'custom') {
            tenant.disabled = false;
            tenant.placeholder = 'Business tenant ID';
            return;
          }
          tenant.value = accountType;
          tenant.disabled = true;
          tenant.placeholder = accountType;
        }

        document.getElementById('outlook-account-type').addEventListener('change', syncOutlookTenantInput);

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
          if (!status.configured) {
            target.textContent = 'Zoho Books authentication is not configured by the app admin yet.';
            config.textContent = 'Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, and ZOHO_REDIRECT_URI, then restart the server.';
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
          config.textContent = `US Zoho login: ${status.login_url} | Redirect URI: ${status.redirect_uri}`;
          pill.textContent = status.connected ? 'Connected' : 'Configured';
          pill.className = status.connected ? 'pill ok' : 'pill';
          target.textContent = status.connected ? 'Zoho Books is connected' : 'Ready to connect Zoho Books';
        }

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

        refreshConnectionStatus();
      </script>
    """
    return page_shell("Accountant Supporter Connections", body, active="home")


def processing_page() -> str:
    records = storage.list_processed_emails()
    record_items = "\n".join(render_record(record) for record in records)
    body = f"""
      <main>
        <section>
          <form id="email-form">
            <h2>Process Email</h2>
            <label for="subject">Subject</label>
            <input id="subject" name="subject" value="Invoice INV-1042 from Northstar Office Supply" required />
            <label for="sender">Sender</label>
            <input id="sender" name="sender" value="Northstar Office Supply &lt;billing@example.com&gt;" required />
            <label for="body">Email Body</label>
            <textarea id="body" name="body" required>Hello,

Please find invoice INV-1042 for office supplies.
Invoice date: 05/20/2026
Due date: 06/19/2026
Amount due: $842.15

Thank you.</textarea>
            <button type="submit">Process Email</button>
          </form>
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
            <div class="status" id="outlook-status">Checking status...</div>
            <div class="notice" id="queue-status">Queue status will appear here.</div>
            <div id="outlook-messages"></div>
          </div>
        </section>
        <section class="records">
          <h2>Recent Results</h2>
          <div id="records">{record_items or "<p>No processed emails yet.</p>"}</div>
        </section>
      </main>
      <script>
        let outlookMessages = [];
        const escapeHtml = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({{
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
        }}[char]));

        document.getElementById('email-form').addEventListener('submit', async (event) => {{
          event.preventDefault();
          const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
          const response = await fetch('/api/email-samples/process', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload)
          }});
          if (!response.ok) {{
            alert('Processing failed');
            return;
          }}
          window.location.reload();
        }});

        async function outlookStatus() {{
          const response = await fetch('/api/outlook/status');
          const status = await response.json();
          const target = document.getElementById('outlook-status');
          const pill = document.getElementById('outlook-pill');
          pill.textContent = status.connected ? 'Connected' : 'Not connected';
          pill.className = status.connected ? 'pill ok' : 'pill';
          target.textContent = status.connected ? 'Outlook is connected' : 'Connect Outlook on the Connections page first';
        }}

        document.getElementById('outlook-fetch').addEventListener('click', async () => {{
          const response = await fetch('/api/outlook/messages?top=5');
          const result = await response.json();
          if (!response.ok) {{
            document.getElementById('outlook-messages').textContent = result.error || 'Unable to fetch messages';
            return;
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
        }});

        document.getElementById('queue-run').addEventListener('click', async () => {{
          const response = await fetch('/api/jobs/run', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ max_jobs: 10 }})
          }});
          const result = await response.json();
          if (!response.ok) {{
            document.getElementById('queue-status').textContent = result.error || 'Unable to run queue';
            return;
          }}
          document.getElementById('queue-status').textContent =
            `Queue run: claimed ${{result.claimed}}, completed ${{result.completed}}, created processing jobs ${{result.created_processing_jobs}}, skipped ${{result.skipped_irrelevant}}.`;
          if (result.completed > 0) {{
            window.location.reload();
          }}
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

        outlookStatus();
        refreshQueueStatus();
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


def render_record(record: ProcessedEmail) -> str:
    extracted = record.extracted
    review = '<span class="badge">Review required</span>' if extracted.needs_review else ""
    amount = "" if extracted.amount is None else f"{extracted.currency or ''} {extracted.amount:,.2f}"
    return f"""
      <article>
        <strong>{html.escape(record.subject)}</strong>
        {review}
        <div class="meta">{html.escape(record.sender)} | workflow v{record.workflow_version} | {record.zoho_status}</div>
        <p>{html.escape(record.summary)}</p>
        <dl>
          <dt>Category</dt><dd>{html.escape(extracted.category)}</dd>
          <dt>Vendor</dt><dd>{html.escape(extracted.vendor_name or "")}</dd>
          <dt>Invoice #</dt><dd>{html.escape(extracted.invoice_number or "")}</dd>
          <dt>Invoice date</dt><dd>{html.escape(extracted.invoice_date or "")}</dd>
          <dt>Due date</dt><dd>{html.escape(extracted.due_date or "")}</dd>
          <dt>Amount</dt><dd>{html.escape(amount)}</dd>
          <dt>Confidence</dt><dd>{extracted.confidence:.2f}</dd>
        </dl>
      </article>
    """


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
