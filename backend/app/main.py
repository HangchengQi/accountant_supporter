from __future__ import annotations

import html
import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .ai import LocalHeuristicProcessor
from .outlook import DeviceCodeSession, OutlookConfig, OutlookGraphClient
from .schemas import EmailSampleIn, ProcessedEmail
from .storage import SQLiteStorage
from .workflow import Workflow, load_workflow
from .zoho import DryRunZohoBooksClient


def get_database_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/accountant_support.db")


def get_workflow_path() -> str:
    return os.getenv("WORKFLOW_PATH", "../workflows/vendor_invoice.v1.json")


storage = SQLiteStorage(get_database_path())
ai_processor = LocalHeuristicProcessor()
zoho_client = DryRunZohoBooksClient()
pending_outlook_auth: DeviceCodeSession | None = None


def health() -> dict[str, str]:
    return {"status": "ok"}


def active_workflow() -> Workflow:
    return load_workflow(get_workflow_path())


def process_email_sample(email: EmailSampleIn) -> ProcessedEmail:
    workflow = load_workflow(get_workflow_path())
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


def list_processed_emails() -> list[ProcessedEmail]:
    return storage.list_processed_emails()


def get_outlook_client() -> OutlookGraphClient:
    return OutlookGraphClient(
        OutlookConfig.from_settings(storage.get_connector_settings("outlook"))
    )


def outlook_status() -> dict[str, Any]:
    settings = storage.get_connector_settings("outlook") or {}
    client = get_outlook_client()
    status = client.configured_status(has_token=storage.get_oauth_token("outlook") is not None)
    status["settings"] = {
        "client_id": client.config.client_id,
        "tenant_id": client.config.tenant_id,
        "scopes": client.config.scopes,
        "saved_locally": bool(settings),
    }
    return status


def save_outlook_settings(data: dict[str, Any]) -> dict[str, Any]:
    client_id = str(data.get("client_id", "")).strip()
    tenant_id = str(data.get("tenant_id", "common")).strip() or "common"
    scopes = str(data.get("scopes", "offline_access User.Read Mail.Read")).strip()
    if not client_id:
        raise ValueError("client_id is required")
    if not scopes:
        raise ValueError("scopes are required")
    storage.save_connector_settings(
        "outlook",
        {
            "client_id": client_id,
            "tenant_id": tenant_id,
            "scopes": scopes,
        },
    )
    return outlook_status()


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
        outlook_client = get_outlook_client()
        token = outlook_client.refresh_token(token)
        storage.save_oauth_token("outlook", token)
    return token


def list_outlook_messages(top: int = 10) -> list[dict[str, Any]]:
    token = get_outlook_token()
    outlook_client = get_outlook_client()
    return [
        message.to_dict()
        for message in outlook_client.list_inbox_messages(token=token, top=top)
    ]


def index() -> str:
    records = storage.list_processed_emails()
    record_items = "\n".join(_render_record(record) for record in records)
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Accountant Support</title>
        <style>
          :root {{
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
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background: #ffffff;
          }}
          header {{
            border-bottom: 1px solid var(--line);
            padding: 22px clamp(18px, 4vw, 52px);
            background: #fbfcfd;
          }}
          main {{
            display: grid;
            grid-template-columns: minmax(360px, 0.92fr) minmax(420px, 1.08fr);
            gap: 28px;
            padding: 28px clamp(18px, 4vw, 52px);
          }}
          h1 {{ margin: 0; font-size: 24px; }}
          h2 {{ margin: 0 0 14px; font-size: 18px; }}
          h3 {{ margin: 18px 0 10px; font-size: 14px; color: #31404a; }}
          p {{ color: var(--muted); line-height: 1.5; }}
          form, .records {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--panel);
            padding: 18px;
          }}
          .connector {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #ffffff;
            margin-top: 18px;
            padding: 18px;
          }}
          .connector-head {{
            display: flex;
            align-items: start;
            justify-content: space-between;
            gap: 14px;
          }}
          .connector-head p {{ margin: 2px 0 0; font-size: 13px; }}
          label {{
            display: block;
            margin: 14px 0 6px;
            color: #31404a;
            font-weight: 650;
            font-size: 14px;
          }}
          input, textarea {{
            width: 100%;
            border: 1px solid #bdc9d1;
            border-radius: 6px;
            padding: 10px 12px;
            font: inherit;
            background: #ffffff;
          }}
          .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
          }}
          textarea {{
            min-height: 240px;
            resize: vertical;
          }}
          button {{
            margin-top: 16px;
            border: 0;
            border-radius: 6px;
            padding: 10px 14px;
            background: var(--accent);
            color: white;
            font-weight: 700;
            cursor: pointer;
          }}
          button:hover {{ background: var(--accent-dark); }}
          button.secondary {{
            background: #eef3f5;
            color: var(--ink);
            border: 1px solid #c9d4db;
          }}
          button.secondary:hover {{ background: #e0e9ed; }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 10px 0;
          }}
          .status {{
            color: var(--muted);
            font-size: 13px;
            min-height: 20px;
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            border: 1px solid var(--line);
            padding: 3px 9px;
            font-size: 12px;
            font-weight: 700;
            color: var(--muted);
            white-space: nowrap;
          }}
          .pill.ok {{
            color: var(--ok);
            border-color: #a6d4ba;
            background: #eef8f2;
          }}
          .notice {{
            border: 1px solid #cddbd8;
            border-radius: 8px;
            background: var(--soft);
            padding: 10px 12px;
            margin-top: 12px;
            font-size: 13px;
            color: #31404a;
          }}
          article {{
            border-top: 1px solid var(--line);
            padding: 14px 0;
          }}
          article:first-child {{ border-top: 0; padding-top: 0; }}
          .meta {{
            color: var(--muted);
            font-size: 13px;
            margin: 4px 0 8px;
          }}
          .badge {{
            display: inline-block;
            border: 1px solid #d6bd7c;
            color: var(--warn);
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 12px;
            font-weight: 700;
          }}
          dl {{
            display: grid;
            grid-template-columns: 130px 1fr;
            gap: 5px 12px;
            margin: 10px 0 0;
            font-size: 14px;
          }}
          dt {{ color: var(--muted); }}
          dd {{ margin: 0; }}
          @media (max-width: 840px) {{
            main {{ grid-template-columns: 1fr; }}
            .grid-2 {{ grid-template-columns: 1fr; }}
          }}
        </style>
      </head>
      <body>
        <header>
          <h1>Accountant Support</h1>
          <p>Local email processing MVP. Data stays in this local app unless a connector is explicitly enabled.</p>
        </header>
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
                  <h2>Outlook</h2>
                  <p>Microsoft Graph device-code sign-in with local token storage.</p>
                </div>
                <span class="pill" id="outlook-pill">Checking</span>
              </div>
              <h3>Connection Settings</h3>
              <div class="grid-2">
                <div>
                  <label for="outlook-client-id">Client ID</label>
                  <input id="outlook-client-id" autocomplete="off" placeholder="Application client ID" />
                </div>
                <div>
                  <label for="outlook-tenant-id">Tenant ID</label>
                  <input id="outlook-tenant-id" autocomplete="off" placeholder="common or tenant ID" />
                </div>
              </div>
              <label for="outlook-scopes">Scopes</label>
              <input id="outlook-scopes" autocomplete="off" value="offline_access User.Read Mail.Read" />
              <div class="toolbar">
                <button type="button" id="outlook-save">Save settings</button>
                <button class="secondary" type="button" id="outlook-start">Start sign-in</button>
                <button class="secondary" type="button" id="outlook-poll">Check sign-in</button>
                <button class="secondary" type="button" id="outlook-fetch">Fetch inbox</button>
              </div>
              <div class="status" id="outlook-status">Checking status...</div>
              <div id="outlook-auth"></div>
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

          const form = document.getElementById('email-form');
          form.addEventListener('submit', async (event) => {{
            event.preventDefault();
            const payload = Object.fromEntries(new FormData(form).entries());
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
            document.getElementById('outlook-client-id').value = status.settings?.client_id || '';
            document.getElementById('outlook-tenant-id').value = status.settings?.tenant_id || 'common';
            document.getElementById('outlook-scopes').value = status.settings?.scopes || 'offline_access User.Read Mail.Read';
            if (!status.configured) {{
              target.textContent = 'Save the Microsoft app client ID before signing in.';
              pill.textContent = 'Not configured';
              pill.className = 'pill';
              return;
            }}
            pill.textContent = status.connected ? 'Connected' : 'Configured';
            pill.className = status.connected ? 'pill ok' : 'pill';
            target.textContent = status.connected ? 'Connected' : 'Ready to sign in';
          }}

          document.getElementById('outlook-save').addEventListener('click', async () => {{
            const payload = {{
              client_id: document.getElementById('outlook-client-id').value,
              tenant_id: document.getElementById('outlook-tenant-id').value,
              scopes: document.getElementById('outlook-scopes').value
            }};
            const response = await fetch('/api/outlook/settings', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify(payload)
            }});
            const result = await response.json();
            const target = document.getElementById('outlook-status');
            if (!response.ok) {{
              target.textContent = result.error || 'Unable to save settings';
              return;
            }}
            target.textContent = 'Settings saved locally';
            await outlookStatus();
          }});

          document.getElementById('outlook-start').addEventListener('click', async () => {{
            const response = await fetch('/api/outlook/auth/start', {{ method: 'POST' }});
            const result = await response.json();
            if (!response.ok) {{
              document.getElementById('outlook-auth').textContent = result.error || 'Unable to start sign-in';
              return;
            }}
            document.getElementById('outlook-auth').innerHTML = `
              <div class="notice">${{escapeHtml(result.message || 'Open Microsoft sign-in and enter the code.')}}</div>
              <dl>
                <dt>Code</dt><dd>${{escapeHtml(result.user_code)}}</dd>
                <dt>URL</dt><dd><a href="${{escapeHtml(result.verification_uri)}}" target="_blank">${{escapeHtml(result.verification_uri)}}</a></dd>
              </dl>
            `;
          }});

          document.getElementById('outlook-poll').addEventListener('click', async () => {{
            const response = await fetch('/api/outlook/auth/poll', {{ method: 'POST' }});
            const result = await response.json();
            document.getElementById('outlook-auth').textContent = result.status || result.error || 'No status';
            await outlookStatus();
          }});

          document.getElementById('outlook-fetch').addEventListener('click', async () => {{
            const response = await fetch('/api/outlook/messages?top=5');
            const result = await response.json();
            if (!response.ok) {{
              document.getElementById('outlook-messages').textContent = result.error || 'Unable to fetch messages';
              return;
            }}
            outlookMessages = result.messages || [];
            document.getElementById('outlook-messages').innerHTML = outlookMessages.map((message, index) => `
              <article>
                <strong>${{escapeHtml(message.subject)}}</strong>
                <div class="meta">${{escapeHtml(message.sender)}} | ${{escapeHtml(message.received_at || '')}}</div>
                <p>${{escapeHtml(message.body_preview)}}</p>
                <button class="secondary" type="button" onclick="processOutlookMessage(${{index}})">Process</button>
              </article>
            `).join('') || '<p>No messages found.</p>';
          }});

          async function processOutlookMessage(index) {{
            const message = outlookMessages[index];
            const response = await fetch('/api/email-samples/process', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{
                subject: message.subject,
                sender: message.sender,
                body: message.body || message.body_preview
              }})
            }});
            if (!response.ok) {{
              alert('Processing failed');
              return;
            }}
            window.location.reload();
          }}

          outlookStatus();
        </script>
      </body>
    </html>
    """


class AccountantSupportHandler(BaseHTTPRequestHandler):
    server_version = "AccountantSupport/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(index())
            return
        if path == "/health":
            self._send_json(health())
            return
        if path == "/api/workflow":
            workflow = active_workflow()
            self._send_json(
                {
                    "workflow": workflow.name,
                    "version": workflow.version,
                    "require_human_review": workflow.require_human_review,
                    "minimum_confidence_for_auto_upload": workflow.minimum_confidence_for_auto_upload,
                    "zoho_mode": workflow.zoho_mode,
                    "raw": workflow.raw,
                }
            )
            return
        if path == "/api/processed-emails":
            self._send_json([record.to_dict() for record in list_processed_emails()])
            return
        if path == "/api/outlook/status":
            self._send_json(outlook_status())
            return
        if path == "/api/outlook/messages":
            query = urlparse(self.path).query
            top = 10
            if query.startswith("top="):
                try:
                    top = int(query.split("=", 1)[1])
                except ValueError:
                    top = 10
            try:
                self._send_json({"messages": list_outlook_messages(top=top)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/outlook/settings":
            try:
                self._send_json(save_outlook_settings(self._read_json()))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
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


def _render_record(record: ProcessedEmail) -> str:
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


def run() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), AccountantSupportHandler)
    print(f"Accountant Support running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
