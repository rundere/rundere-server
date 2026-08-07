"""PDF attachment text extraction — third in the chain (D-27).

4 of 16 emails in the first real corpus carried the actual booking data in a
PDF attachment (concert ticket, invoice, rail eTicket) with only a generic
HTML wrapper. This stage pulls the text out; the pipeline then feeds it
through the same try-order (in practice: to the LLM — PDFs don't carry
JSON-LD). Text extraction only; scanned/image PDFs come back empty and are
reported as such (a vision-model pass is a possible future enhancement —
qwen VL models are already present in the Ollama instance).
"""
from __future__ import annotations

import io


def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 20) -> str:
    """Best-effort text from one PDF. Returns '' when nothing is extractable
    (encrypted, scanned, malformed) — the caller decides what that means."""
    try:
        from pypdf import PdfReader   # lazy: keeps extract/ importable without pypdf
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            try:
                reader.decrypt("")          # many "encrypted" tickets use an empty owner password
            except Exception:
                return ""
        pages = []
        for page in reader.pages[:max_pages]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n\n".join(p for p in pages if p.strip()).strip()
    except Exception:
        return ""


def extract_all_pdfs(attachments: list[tuple[str, bytes]]) -> dict[str, str]:
    """{filename: extracted_text} for every PDF that yielded any text."""
    out = {}
    for filename, data in attachments:
        text = extract_pdf_text(data)
        if text:
            out[filename] = text
    return out
