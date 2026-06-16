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

## Portable Windows Build

For customer installs, build a portable folder that includes an embedded Python runtime. Customers do not need to install Python separately.

```powershell
cd accountant_supporter
.\scripts\build_portable_windows.ps1
```

The build output is:

```text
dist\AccountantSupporterPortable\
  Accountant Supporter.exe
  Stop Accountant Supporter.exe
  Start Accountant Supporter.vbs     fallback launcher
  Stop Accountant Supporter.bat      fallback stopper
  runtime\                 embedded Python
  app\                     app code and workflows
  data\                    local database, bills, logs
  README_PORTABLE.txt
```

To install on another Windows PC, copy the whole `AccountantSupporterPortable` folder and double-click `Accountant Supporter.exe`. The app starts hidden and opens the browser. To stop it, double-click `Stop Accountant Supporter.exe`.

If Windows policy blocks the EXE launchers, use `Start Accountant Supporter.vbs` and `Stop Accountant Supporter.bat` as a fallback.

If the build machine cannot download Python automatically, download the Python embeddable Windows ZIP first and pass it in:

```powershell
.\scripts\build_portable_windows.ps1 -PythonEmbedZipPath C:\path\python-3.12.10-embed-amd64.zip
```

Each customer install should keep its own `data` folder. Do not reuse another customer's database or OAuth token files.

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
$env:OUTLOOK_SCOPES="offline_access User.Read Mail.Read Mail.Send"
$env:OUTLOOK_REDIRECT_URI="http://127.0.0.1:8080/auth/outlook/callback"
```

Then run:

```powershell
cd accountant_supporter\backend
python -m app.main
```

Open `http://localhost:8080`, use the Mail Authentication panel to connect Outlook, then use the Processing page to fetch recent inbox messages. The primary Connect Outlook action uses Microsoft device-code sign-in, which does not require a reply URL. A redirect sign-in fallback is also available for app registrations with a working Web redirect URI.

The `Mail.Send` scope is needed only for emailing the daily billing log. If this scope is added after a user already connected Outlook, reconnect Outlook so Microsoft grants the new permission.

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
- OpenAI Model, for example `gpt-5.5`
- Classification Model, a cheaper/faster model for `classify_email` jobs
- OpenAI API Key

Environment variables are still supported for scripted local installs:

```powershell
$env:AI_PROVIDER="openai"
$env:OPENAI_API_KEY="your-openai-api-key"
$env:OPENAI_MODEL="gpt-5.5"
$env:OPENAI_CLASSIFICATION_MODEL="gpt-5.4-mini"
```

The Connections page shows whether the active processor is local or OpenAI.

## Internal Workflow Process

AI behavior is controlled by the local workflow package at `workflows/vendor_invoice.v1.json`. The private `ai_process.instructions` value is used as the OpenAI system instructions and is not shown in the app UI or returned by the workflow status endpoint.

To change the AI workflow, ship a patch or remote git update that edits the workflow JSON, then restart the local server. The `WORKFLOW_PATH` environment variable can point an installation to a different versioned workflow file.

## Local Job Queue

Mail ingestion now stores raw messages in SQLite before processing. Outlook fetches are deduplicated by provider message ID, then each new message gets a `classify_email` job. Bill-relevant messages (`invoice`, `receipt`, and `statement`) are promoted to `process_email` jobs; irrelevant messages are marked skipped.

The Processing page can fetch Outlook messages into the queue and run a local queue pass. Internal API endpoints are also available for an MCP poller or scheduler:

- `GET /api/jobs/status`
- `POST /api/jobs/run` with `{"max_jobs": 10}`

When a bill-relevant Outlook email is processed, the app downloads file attachments, saves them under `data/bills/Billing{Email date}`, renames each bill file as `Vendor_InvoiceDate_InvoiceNumber.ext`, appends a daily markdown log under `data/logs`, and sends that log to the Log Receiver configured on the Connections page.

## Next Milestones

1. Create real Zoho Books draft bills/invoices from approved payloads.
2. Add tenant/user isolation for multi-user cloud mode.
3. Add Gmail as a second mail connector.
4. Add signed workflow package updates.
5. Add review/approval states before Zoho writes.
