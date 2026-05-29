# Accountant Supporter

Local-first MVP for processing bookkeeping emails before sending structured drafts to Zoho Books.

## What this MVP does

- Runs locally with Docker Compose or Python.
- Stores email processing results in a local SQLite database.
- Lets you paste a sample email into a small admin UI.
- Summarizes the email and extracts invoice-like fields.
- Loads workflow behavior from a local versioned JSON file.
- Uses adapter boundaries for mail, AI processing, storage, workflow updates, and Zoho Books.
- Simulates Zoho upload as a safe local draft action.

## Quick Start

```powershell
cd accountant_supporter
docker compose up --build
```

Then open:

```text
http://localhost:8080
```

Without Docker:

```powershell
cd accountant_supporter\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m app.main
```

## Project Shape

```text
accountant_supporter/
  backend/
    app/
      ai.py              local and future OpenAI/Azure/local-model processors
      main.py            local HTTP app and admin UI
      schemas.py         API models
      storage.py         local SQLite storage
      workflow.py        signed/versioned workflow loading foundation
      zoho.py            Zoho Books adapter boundary
    tests/
  workflows/
    vendor_invoice.v1.json
  docker-compose.yml
```

## Privacy Model

The default mode is local-first. Email text, summaries, extracted fields, workflow logs, and draft upload records stay in the local SQLite database at `data/accountant_support.db`.

Cloud mode can be added later by swapping adapters, not by rewriting the workflow engine.

## Outlook Connector

The Outlook connector uses Microsoft Graph OAuth redirect sign-in. The local app stores OAuth tokens in the local SQLite database.

Create a Microsoft Entra app registration and add this redirect URI:

```text
http://127.0.0.1:8080/auth/outlook/callback
```

Then set these environment values before starting the local server:

```powershell
$env:OUTLOOK_CLIENT_ID="your-client-id"
$env:OUTLOOK_CLIENT_SECRET="your-client-secret"
$env:OUTLOOK_TENANT_ID="common"
$env:OUTLOOK_SCOPES="offline_access User.Read Mail.Read"
$env:OUTLOOK_REDIRECT_URI="http://127.0.0.1:8080/auth/outlook/callback"
```

Then run:

```powershell
cd accountant_supporter\backend
python -m app.main
```

Open `http://localhost:8080`, use the Email Summary panel to connect Outlook, then fetch recent inbox messages.

## Next Milestones

1. Add Zoho Books OAuth and create draft bills/invoices.
2. Add OpenAI structured extraction behind the `AIProcessor` adapter.
3. Add Gmail as a second mail connector.
4. Add signed workflow package updates.
5. Add review/approval states before Zoho writes.
