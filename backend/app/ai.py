from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import EmailSampleIn, ExtractedFields
from .workflow import Workflow


@dataclass(frozen=True)
class AIResult:
    summary: str
    extracted: ExtractedFields


class AIProcessor:
    def process(self, email: EmailSampleIn, workflow: Workflow) -> AIResult:
        raise NotImplementedError


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
        return AIResult(summary=self._summarize(email), extracted=extracted)

    def _summarize(self, email: EmailSampleIn) -> str:
        cleaned = re.sub(r"\s+", " ", email.body).strip()
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        first_sentences = [s for s in sentences if s][:3]
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
