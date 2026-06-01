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

Open `http://localhost:8080`, use the Mail Authentication panel to connect Outlook, then use the Processing page to fetch recent inbox messages.

## Zoho Books Connector

The Zoho Books connector uses Zoho OAuth redirect sign-in. Add this redirect URI in the Zoho API Console:

```text
http://127.0.0.1:8080/auth/zoho/callback
```

Set these environment values before starting the local server:

```powershell
$env:ZOHO_CLIENT_ID="your-client-id"
$env:ZOHO_CLIENT_SECRET="your-client-secret"
$env:ZOHO_SCOPES="ZohoBooks.fullaccess.all"
$env:ZOHO_REDIRECT_URI="http://127.0.0.1:8080/auth/zoho/callback"
```

This MVP assumes US Zoho accounts and redirects users to `https://accounts.zoho.com`.

## ChatGPT Processing

By default, the app uses a local heuristic processor. To use ChatGPT/OpenAI for email summaries and structured invoice extraction, open the Connections page and save:

- AI Provider: `openai`
- OpenAI Model, for example `gpt-5.2`
- OpenAI API Key

Environment variables are still supported for scripted local installs:

```powershell
$env:AI_PROVIDER="openai"
$env:OPENAI_API_KEY="your-openai-api-key"
$env:OPENAI_MODEL="gpt-5.2"
```

The Connections page shows whether the active processor is local or OpenAI.

## Internal Workflow Process

AI behavior is controlled by the local workflow package at `workflows/vendor_invoice.v1.json`. The private `ai_process.instructions` value is used as the OpenAI system instructions and is not shown in the app UI or returned by the workflow status endpoint.

To change the AI workflow, ship a patch or remote git update that edits the workflow JSON, then restart the local server. The `WORKFLOW_PATH` environment variable can point an installation to a different versioned workflow file.

## Next Milestones

1. Create real Zoho Books draft bills/invoices from approved payloads.
2. Add tenant/user isolation for multi-user cloud mode.
3. Add Gmail as a second mail connector.
4. Add signed workflow package updates.
5. Add review/approval states before Zoho writes.
