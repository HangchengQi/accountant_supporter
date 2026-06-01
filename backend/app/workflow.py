from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_AI_INSTRUCTIONS = (
    "You summarize bookkeeping-related emails and extract invoice fields. "
    "Return only values supported by the email. Use null when unknown. "
    "Mark needs_review true when the email is ambiguous or any important "
    "accounting field is missing. JSON output must match the schema."
)


@dataclass(frozen=True)
class Workflow:
    name: str
    version: int
    summary_sentences: int
    require_human_review: bool
    minimum_confidence_for_auto_upload: float
    zoho_mode: str
    ai_instructions: str
    raw: dict[str, Any]

    def public_status(self) -> dict[str, Any]:
        return {
            "workflow": self.name,
            "version": self.version,
            "require_human_review": self.require_human_review,
            "minimum_confidence_for_auto_upload": self.minimum_confidence_for_auto_upload,
            "zoho_mode": self.zoho_mode,
        }


def load_workflow(path: str) -> Workflow:
    workflow_path = Path(path)
    with workflow_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    processing = raw.get("processing", {})
    zoho = raw.get("zoho", {})
    ai_process = raw.get("ai_process", {})
    return Workflow(
        name=raw["workflow"],
        version=int(raw["version"]),
        summary_sentences=int(processing.get("summary_sentences", 3)),
        require_human_review=bool(processing.get("require_human_review", True)),
        minimum_confidence_for_auto_upload=float(
            processing.get("minimum_confidence_for_auto_upload", 0.9)
        ),
        zoho_mode=str(zoho.get("mode", "dry_run")),
        ai_instructions=str(ai_process.get("instructions") or DEFAULT_AI_INSTRUCTIONS).strip(),
        raw=raw,
    )
