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

## Next Milestones

1. Add OAuth connectors for Microsoft Graph and Gmail.
2. Add Zoho Books OAuth and create draft bills/invoices.
3. Add OpenAI structured extraction behind the `AIProcessor` adapter.
4. Add signed workflow package updates.
5. Add review/approval states before Zoho writes.
