from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Workflow:
    name: str
    version: int
    require_human_review: bool
    minimum_confidence_for_auto_upload: float
    zoho_mode: str
    raw: dict[str, Any]


def load_workflow(path: str) -> Workflow:
    workflow_path = Path(path)
    with workflow_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    processing = raw.get("processing", {})
    zoho = raw.get("zoho", {})
    return Workflow(
        name=raw["workflow"],
        version=int(raw["version"]),
        require_human_review=bool(processing.get("require_human_review", True)),
        minimum_confidence_for_auto_upload=float(
            processing.get("minimum_confidence_for_auto_upload", 0.9)
        ),
        zoho_mode=str(zoho.get("mode", "dry_run")),
        raw=raw,
    )
