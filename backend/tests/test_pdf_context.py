import unittest
import zlib

from app.billing import BillAttachment
from app.main import _email_with_attachment_context
from app.pdf_context import build_attachment_context, extract_pdf_text
from app.schemas import EmailSampleIn


def text_pdf_bytes(text: str) -> bytes:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1")
    )
    stream = b"BT /F1 12 Tf 72 720 Td (" + escaped + b") Tj ET"
    compressed = zlib.compress(stream)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Length "
        + str(len(compressed)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n%%EOF\n"
    )


class PdfContextTest(unittest.TestCase):
    def test_extracts_text_from_text_pdf(self) -> None:
        content = text_pdf_bytes(
            "Vendor: Home Depot Invoice #: 0137396431 Invoice date: 2026-05-20 Amount: $154.20"
        )

        text = extract_pdf_text(content)

        self.assertIn("Home Depot", text)
        self.assertIn("0137396431", text)
        self.assertIn("$154.20", text)

    def test_builds_attachment_context_for_pdf(self) -> None:
        content = text_pdf_bytes("Invoice #: INV-100 Amount: $12.00")

        context = build_attachment_context(
            [BillAttachment(name="invoice.pdf", content=content)]
        )

        self.assertIn("Attachment context for invoice extraction", context)
        self.assertIn("PDF attachment: invoice.pdf", context)
        self.assertIn("INV-100", context)

    def test_appends_attachment_context_before_processing(self) -> None:
        email = EmailSampleIn(
            subject="Bill",
            sender="Vendor <billing@example.com>",
            body="Please see attached.",
        )
        content = text_pdf_bytes("Invoice date: 2026-06-05 Amount due: $99.00")

        enriched = _email_with_attachment_context(
            email,
            [BillAttachment(name="bill.pdf", content=content)],
        )

        self.assertIn("Please see attached.", enriched.body)
        self.assertIn("Attachment context", enriched.body)
        self.assertIn("Amount due: $99.00", enriched.body)


if __name__ == "__main__":
    unittest.main()
