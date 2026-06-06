from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path


MAX_PDF_CONTEXT_CHARS = 12000


@dataclass(frozen=True)
class AttachmentText:
    name: str
    text: str
    readable: bool


def build_attachment_context(attachments: list[object], max_chars: int = MAX_PDF_CONTEXT_CHARS) -> str:
    sections: list[str] = []
    remaining = max_chars
    for attachment in attachments:
        name = str(getattr(attachment, "name", "attachment.bin") or "attachment.bin")
        content = getattr(attachment, "content", b"")
        if not _is_pdf(name, content):
            continue

        text = extract_pdf_text(bytes(content), max_chars=remaining)
        if text:
            section = f"PDF attachment: {name}\n{text}"
        else:
            section = (
                f"PDF attachment: {name}\n"
                "No readable text was extracted. This may be a scanned or image-only PDF."
            )
        sections.append(section)
        remaining -= len(section)
        if remaining <= 0:
            break

    if not sections:
        return ""
    return "\n\n".join(["Attachment context for invoice extraction:", *sections])


def append_attachment_context(body: str, attachment_context: str) -> str:
    body = body.strip()
    attachment_context = attachment_context.strip()
    if not attachment_context:
        return body
    if not body:
        return attachment_context
    return f"{body}\n\n---\n{attachment_context}"


def extract_pdf_text(content: bytes, max_chars: int = MAX_PDF_CONTEXT_CHARS) -> str:
    if not content.lstrip().startswith(b"%PDF"):
        return ""

    fragments: list[str] = []
    for stream in _pdf_streams(content):
        fragments.extend(_extract_text_operators(stream))
        if sum(len(fragment) for fragment in fragments) >= max_chars:
            break

    if not fragments:
        fragments.extend(_extract_text_operators(content))

    text = _normalize_text(" ".join(fragment for fragment in fragments if fragment))
    return text[:max_chars].strip()


def _is_pdf(name: str, content: bytes) -> bool:
    return Path(name).suffix.lower() == ".pdf" or content.lstrip().startswith(b"%PDF")


def _pdf_streams(content: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for match in re.finditer(rb"(<<.*?>>)\s*stream\r?\n(.*?)\r?\nendstream", content, re.S):
        dictionary = match.group(1)
        stream = match.group(2)
        if b"/FlateDecode" in dictionary:
            try:
                stream = zlib.decompress(stream)
            except zlib.error:
                continue
        streams.append(stream)
    return streams


def _extract_text_operators(content: bytes) -> list[str]:
    text = content.decode("latin-1", errors="ignore")
    fragments: list[str] = []
    for literal in re.findall(r"\((?:\\.|[^\\)])*\)\s*Tj", text, flags=re.S):
        fragments.append(_decode_pdf_literal(literal[:-2].strip()))
    for array in re.findall(r"\[(.*?)\]\s*TJ", text, flags=re.S):
        fragments.extend(_decode_pdf_literal(match) for match in re.findall(r"\((?:\\.|[^\\)])*\)", array))
    return fragments


def _decode_pdf_literal(value: str) -> str:
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]

    def replace_octal(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    value = re.sub(r"\\([0-7]{1,3})", replace_octal, value)
    replacements = {
        r"\n": "\n",
        r"\r": "\n",
        r"\t": "\t",
        r"\b": "\b",
        r"\f": "\f",
        r"\(": "(",
        r"\)": ")",
        r"\\": "\\",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def _normalize_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.:;])", r"\1", value)
    return value.strip()
