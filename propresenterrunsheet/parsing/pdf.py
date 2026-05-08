"""PDF text extraction.

Wraps pdfplumber. The runsheet is expected to be a text PDF (not a scanned
image) — pdfplumber only reads embedded text, so scans return empty
strings and the caller surfaces a helpful error to the user."""


def extract_pdf_text(path: str) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)
