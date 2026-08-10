"""PDF text extraction, with a rasterise-for-OCR fallback.

`extract_pdf_text` wraps pdfplumber and is UNCHANGED — it reads embedded
text, which is what a runsheet exported from Word or Planning Center
has. That is the path essentially every Sunday runsheet takes, and it is
deliberately untouched by the OCR work: pdfplumber already handles
reading order and columns, and the OCR reconstruction in `parsing/ocr.py`
exists only to catch up to what pdfplumber gives us for free.

`pdf_text_or_images` adds the fallback for SCANNED runsheets — PDFs whose
pages are just images, where pdfplumber returns "" and the operator
previously hit a dead end. Only then do we rasterise with pypdfium2 and
hand the pages to OCR. The ordering is the whole safety story: a PDF that
yields text never reaches the rasteriser or the OCR code.
"""

import logging

log = logging.getLogger("pp_runsheet")

# Enough for any real order of service, and low enough that a mistakenly
# uploaded 200-page document cannot hang the app OCR'ing for minutes.
MAX_OCR_PAGES = 5

# Rasterising resolution. pdfium's default 72dpi is too coarse for OCR of
# body text; 200dpi is the usual accuracy plateau for document scans and
# keeps a 5-page render well under a second.
OCR_RENDER_SCALE = 200 / 72


def extract_pdf_text(path: str) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def render_pdf_pages(path: str, max_pages: int = MAX_OCR_PAGES) -> list:
    """Render up to `max_pages` pages to PIL images for OCR."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    try:
        return [doc[i].render(scale=OCR_RENDER_SCALE).to_pil()
                for i in range(min(len(doc), max_pages))]
    finally:
        doc.close()


def pdf_text_or_images(path: str, extract=None, render=None):
    """Return `(text, images)` for a PDF — never both, often neither.

    Text PDFs return `(text, [])` and are never rasterised. Scanned PDFs
    return `("", [page images])` for the caller to OCR. A PDF that is
    neither readable nor renderable returns `("", [])` so the route can
    explain the problem in words instead of leaking a pdfium error.

    `extract` and `render` are injectable so tests can prove the
    never-rasterise-a-text-PDF rule without building real PDFs.
    """
    extract = extract or extract_pdf_text
    render = render or render_pdf_pages

    try:
        text = extract(path)
    except Exception:
        # Some real-world PDFs break pdfplumber but render fine in
        # pdfium. Falling through to OCR beats a 500.
        log.exception("pdfplumber failed on %s — trying rasterise", path)
        text = ""

    if (text or "").strip():
        return text, []

    try:
        return "", render(path, max_pages=MAX_OCR_PAGES)
    except Exception:
        log.exception("could not rasterise %s for OCR", path)
        return "", []
