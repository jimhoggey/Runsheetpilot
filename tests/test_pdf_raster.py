"""Tests for rasterising image-only PDFs so OCR can read them.

A scanned runsheet is a PDF whose pages carry no embedded text, so
`extract_pdf_text` returns "" and, before this, the operator hit a dead
end ("make sure it is a text-based PDF").

The contract these tests protect is a narrow one, and it is the whole
safety story for the feature: **rasterising is reachable only when
pdfplumber found nothing.** A PDF that yields text never touches
pypdfium2 or the OCR reconstruction code, so neither can regress the
Sunday-morning path.
"""
import pytest

from propresenterrunsheet.parsing.pdf import (
    extract_pdf_text, pdf_text_or_images, MAX_OCR_PAGES,
)


def _fake_render(pages):
    """Stand in for pypdfium2 — returns `pages` opaque page objects."""
    def render(path, max_pages=MAX_OCR_PAGES):
        return [f"page-{i}" for i in range(min(pages, max_pages))]
    return render


def test_text_pdf_returns_its_text_and_never_rasterises():
    def exploding_render(path, max_pages=MAX_OCR_PAGES):
        raise AssertionError("a text PDF must never be rasterised")

    text, images = pdf_text_or_images(
        "/tmp/x.pdf",
        extract=lambda p: "6:30pm  Youth Arrival",
        render=exploding_render)
    assert text == "6:30pm  Youth Arrival"
    assert images == []


def test_empty_pdf_falls_back_to_rendered_pages():
    text, images = pdf_text_or_images(
        "/tmp/x.pdf", extract=lambda p: "", render=_fake_render(3))
    assert text == ""
    assert images == ["page-0", "page-1", "page-2"]


def test_whitespace_only_pdf_counts_as_empty():
    """pdfplumber returns '\\n\\n' for a scan, not '' — the old check for
    truthiness would have called that a text PDF."""
    text, images = pdf_text_or_images(
        "/tmp/x.pdf", extract=lambda p: "  \n \n ", render=_fake_render(1))
    assert images == ["page-0"]


def test_page_count_is_capped():
    """A 40-page scan would take minutes to OCR and blow the prompt
    budget; the cap keeps a mis-drop from hanging the app."""
    _text, images = pdf_text_or_images(
        "/tmp/x.pdf", extract=lambda p: "", render=_fake_render(40))
    assert len(images) == MAX_OCR_PAGES


def test_extraction_failure_still_falls_back_to_rasterising():
    """A malformed-but-renderable PDF should reach OCR rather than 500.

    pdfplumber raises on some real-world PDFs that pdfium renders fine.
    """
    def boom(path):
        raise ValueError("pdfplumber choked")

    text, images = pdf_text_or_images(
        "/tmp/x.pdf", extract=boom, render=_fake_render(2))
    assert text == ""
    assert images == ["page-0", "page-1"]


def test_render_failure_surfaces_as_empty_not_an_exception():
    """If neither path works the route needs to say so in words, so this
    returns empty rather than raising a pdfium error at the operator."""
    def boom(path, max_pages=MAX_OCR_PAGES):
        raise RuntimeError("pdfium failed")

    text, images = pdf_text_or_images(
        "/tmp/x.pdf", extract=lambda p: "", render=boom)
    assert (text, images) == ("", [])


def test_extract_pdf_text_signature_is_unchanged():
    """The text-PDF path is the regression guard: same name, one arg."""
    import inspect
    assert list(inspect.signature(extract_pdf_text).parameters) == ["path"]
