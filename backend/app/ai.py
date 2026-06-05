from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .schemas import EmailSampleIn, ExtractedFields
from .workflow import Workflow


@dataclass(frozen=True)
class AIResult:
    summary: str
    extracted: ExtractedFields


@dataclass(frozen=True)
class ClassificationResult:
    category: str
    confidence: float
    needs_review: bool
    mode: str = "rules"

    @property
    def is_bill_relevant(self) -> bool:
        return self.category in {"invoice", "receipt", "statement"}


class AIProcessor:
    def process(self, email: EmailSampleIn, workflow: Workflow) -> AIResult:
        raise NotImplementedError


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> "OpenAIConfig":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            model=os.getenv("OPENAI_MODEL", "gpt-5.2").strip() or "gpt-5.2",
        )

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any] | None,
        model_key: str = "openai_model",
        env_model_key: str = "OPENAI_MODEL",
    ) -> "OpenAIConfig":
        env_config = cls.from_env()
        env_model = os.getenv(env_model_key, "").strip()
        if not settings:
            return cls(api_key=env_config.api_key, model=env_model or env_config.model)
        return cls(
            api_key=str(settings.get("openai_api_key") or env_config.api_key).strip(),
            model=str(settings.get(model_key) or env_model or env_config.model).strip() or "gpt-5.2",
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


class OpenAIProcessor(AIProcessor):
    def __init__(self, config: OpenAIConfig | None = None) -> None:
        self.config = config or OpenAIConfig.from_env()

    def process(self, email: EmailSampleIn, workflow: Workflow) -> AIResult:
        if not self.config.is_configured:
            raise ValueError("OPENAI_API_KEY is required")

        payload = {
            "model": self.config.model,
            "instructions": workflow.ai_instructions,
            "input": [
                {
                    "role": "user",
                    "content": (
                        f"Subject: {email.subject}\n"
                        f"Sender: {email.sender}\n\n"
                        f"Email body:\n{email.body}"
                    ),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "bookkeeping_email_extraction",
                    "strict": True,
                    "schema": self._schema(),
                }
            },
        }

        response = self._post_response(payload)
        result = self._extract_json(response)
        extracted = ExtractedFields(
            category=str(result["category"]),
            vendor_name=result["vendor_name"],
            invoice_number=result["invoice_number"],
            invoice_date=result["invoice_date"],
            due_date=result["due_date"],
            amount=result["amount"],
            currency=result["currency"],
            confidence=float(result["confidence"]),
            needs_review=(
                bool(result["needs_review"])
                or float(result["confidence"]) < workflow.minimum_confidence_for_auto_upload
                or workflow.require_human_review
            ),
        )
        return AIResult(summary=str(result["summary"]), extracted=extracted)

    def _post_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise ValueError(f"OpenAI request failed: {body or exc}") from exc

    def _extract_json(self, response: dict[str, Any]) -> dict[str, Any]:
        if isinstance(response.get("output_text"), str):
            return json.loads(response["output_text"])

        for item in response.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    return json.loads(text)
        raise ValueError("OpenAI response did not include JSON text")

    def _schema(self) -> dict[str, Any]:
        nullable_string = {"type": ["string", "null"]}
        nullable_number = {"type": ["number", "null"]}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "category",
                "vendor_name",
                "invoice_number",
                "invoice_date",
                "due_date",
                "amount",
                "currency",
                "confidence",
                "needs_review",
            ],
            "properties": {
                "summary": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": [
                        "invoice",
                        "receipt",
                        "statement",
                        "bookkeeping_question",
                        "irrelevant",
                    ],
                },
                "vendor_name": nullable_string,
                "invoice_number": nullable_string,
                "invoice_date": nullable_string,
                "due_date": nullable_string,
                "amount": nullable_number,
                "currency": nullable_string,
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "needs_review": {"type": "boolean"},
            },
        }


class OpenAIClassifier:
    def __init__(self, config: OpenAIConfig | None = None) -> None:
        self.config = config or OpenAIConfig.from_env()

    def classify(self, email: EmailSampleIn) -> ClassificationResult:
        if not self.config.is_configured:
            raise ValueError("OPENAI_API_KEY is required")

        payload = {
            "model": self.config.model,
            "instructions": (
                "Classify bookkeeping email relevance. Use the cheapest reliable reasoning. "
                "Return invoice, receipt, statement, bookkeeping_question, or irrelevant. "
                "Mark needs_review true only when the email may need human triage."
            ),
            "input": [
                {
                    "role": "user",
                    "content": (
                        f"Subject: {email.subject}\n"
                        f"Sender: {email.sender}\n\n"
                        f"Email body:\n{email.body}"
                    ),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "email_classification",
                    "strict": True,
                    "schema": self._schema(),
                }
            },
        }

        response = self._post_response(payload)
        result = self._extract_json(response)
        return ClassificationResult(
            category=str(result["category"]),
            confidence=float(result["confidence"]),
            needs_review=bool(result["needs_review"]),
            mode=f"openai:{self.config.model}",
        )

    def _post_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise ValueError(f"OpenAI classification failed: {body or exc}") from exc

    def _extract_json(self, response: dict[str, Any]) -> dict[str, Any]:
        if isinstance(response.get("output_text"), str):
            return json.loads(response["output_text"])

        for item in response.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    return json.loads(text)
        raise ValueError("OpenAI classification response did not include JSON text")

    def _schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["category", "confidence", "needs_review"],
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "invoice",
                        "receipt",
                        "statement",
                        "bookkeeping_question",
                        "irrelevant",
                    ],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "needs_review": {"type": "boolean"},
            },
        }


def create_ai_processor(settings: dict[str, Any] | None = None) -> AIProcessor:
    provider = _provider(settings)
    if provider in {"openai", "chatgpt"}:
        config = OpenAIConfig.from_settings(settings)
        if config.is_configured:
            return OpenAIProcessor(config)
    return LocalHeuristicProcessor()


def ai_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    config = OpenAIConfig.from_settings(settings)
    classification_config = OpenAIConfig.from_settings(
        settings,
        model_key="openai_classification_model",
        env_model_key="OPENAI_CLASSIFICATION_MODEL",
    )
    provider = _provider(settings)
    active_provider = "openai" if provider in {"openai", "chatgpt"} and config.is_configured else "local"
    return {
        "configured": config.is_configured,
        "requested_provider": provider,
        "active_provider": active_provider,
        "model": config.model if config.is_configured else None,
        "job_models": {
            "classify_email": classification_config.model if classification_config.is_configured else "rules",
            "process_email": config.model if config.is_configured else "local",
        },
        "settings": {
            "provider": provider,
            "openai_model": config.model,
            "openai_classification_model": classification_config.model,
            "has_openai_api_key": config.is_configured,
            "saved_locally": bool(settings),
        },
    }


def _provider(settings: dict[str, Any] | None = None) -> str:
    if settings and settings.get("provider"):
        return str(settings["provider"]).strip().lower()
    return os.getenv("AI_PROVIDER", "local").strip().lower()


def classify_email(email: EmailSampleIn, settings: dict[str, Any] | None = None) -> ClassificationResult:
    rule_result = classify_email_with_rules(email)
    if rule_result.confidence >= 0.85 or _provider(settings) not in {"openai", "chatgpt"}:
        return rule_result

    config = OpenAIConfig.from_settings(
        settings,
        model_key="openai_classification_model",
        env_model_key="OPENAI_CLASSIFICATION_MODEL",
    )
    if not config.is_configured:
        return rule_result
    return OpenAIClassifier(config).classify(email)


def classify_email_with_rules(email: EmailSampleIn) -> ClassificationResult:
    text = f"{email.subject}\n{email.body}".lower()
    if any(word in text for word in ["invoice", "amount due", "bill #", "bill number"]):
        return ClassificationResult("invoice", 0.86, False)
    if any(word in text for word in ["receipt", "paid", "payment received"]):
        return ClassificationResult("receipt", 0.82, False)
    if "statement" in text:
        return ClassificationResult("statement", 0.8, False)
    if any(word in text for word in ["bookkeeping", "accounting", "1099", "w-9", "tax"]):
        return ClassificationResult("bookkeeping_question", 0.62, True)
    return ClassificationResult("irrelevant", 0.72, False)


class LocalHeuristicProcessor(AIProcessor):
    """Safe local placeholder until an OpenAI/Azure/local-model adapter is enabled."""

    def process(self, email: EmailSampleIn, workflow: Workflow) -> AIResult:
        text = f"{email.subject}\n{email.body}"
        amount = self._find_amount(text)
        invoice_number = self._find_invoice_number(text)
        invoice_date = self._find_labeled_date(text, ["invoice date", "date"])
        due_date = self._find_labeled_date(text, ["due date", "due"])
        category = self._classify(text)
        confidence = self._confidence(category, amount, invoice_number)
        needs_review = (
            workflow.require_human_review
            or confidence < workflow.minimum_confidence_for_auto_upload
        )

        extracted = ExtractedFields(
            category=category,
            vendor_name=self._guess_vendor(email.sender),
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            due_date=due_date,
            amount=amount,
            currency="USD" if amount is not None else None,
            confidence=confidence,
            needs_review=needs_review,
        )
        return AIResult(summary=self._summarize(email, workflow), extracted=extracted)

    def _summarize(self, email: EmailSampleIn, workflow: Workflow) -> str:
        cleaned = re.sub(r"\s+", " ", email.body).strip()
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        first_sentences = [s for s in sentences if s][: workflow.summary_sentences]
        if first_sentences:
            return " ".join(first_sentences)
        return cleaned[:280]

    def _classify(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ["invoice", "bill", "amount due"]):
            return "invoice"
        if any(word in lowered for word in ["receipt", "paid", "payment received"]):
            return "receipt"
        if "statement" in lowered:
            return "statement"
        return "bookkeeping_question"

    def _find_amount(self, text: str) -> float | None:
        match = re.search(r"(?:total|amount due|amount|balance)\D{0,20}\$?([0-9][0-9,]*(?:\.[0-9]{2})?)", text, re.I)
        if not match:
            match = re.search(r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)", text)
        if not match:
            return None
        return float(match.group(1).replace(",", ""))

    def _find_invoice_number(self, text: str) -> str | None:
        match = re.search(r"(?:invoice|inv)[\s#:.-]*([A-Z0-9-]{3,})", text, re.I)
        return match.group(1) if match else None

    def _find_labeled_date(self, text: str, labels: list[str]) -> str | None:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:{label_pattern})\D{{0,15}}([0-9]{{1,2}}[/-][0-9]{{1,2}}[/-][0-9]{{2,4}})",
            text,
            re.I,
        )
        return match.group(1) if match else None

    def _guess_vendor(self, sender: str) -> str | None:
        name = sender.split("<", 1)[0].strip().strip('"')
        return name or None

    def _confidence(self, category: str, amount: float | None, invoice_number: str | None) -> float:
        score = 0.35
        if category in {"invoice", "receipt", "statement"}:
            score += 0.25
        if amount is not None:
            score += 0.25
        if invoice_number:
            score += 0.15
        return min(score, 0.98)
