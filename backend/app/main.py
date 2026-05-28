from __future__ import annotations

import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .ai import LocalHeuristicProcessor
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
            --accent: #176b5d;
            --accent-dark: #0f4d43;
            --warn: #8a5a00;
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
            padding: 20px clamp(18px, 4vw, 52px);
          }}
          main {{
            display: grid;
            grid-template-columns: minmax(320px, 0.9fr) minmax(360px, 1.1fr);
            gap: 28px;
            padding: 28px clamp(18px, 4vw, 52px);
          }}
          h1 {{ margin: 0; font-size: 24px; }}
          h2 {{ margin: 0 0 14px; font-size: 18px; }}
          p {{ color: var(--muted); line-height: 1.5; }}
          form, .records {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--panel);
            padding: 18px;
          }}
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
          </section>
          <section class="records">
            <h2>Recent Results</h2>
            <div id="records">{record_items or "<p>No processed emails yet.</p>"}</div>
          </section>
        </main>
        <script>
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
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
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
