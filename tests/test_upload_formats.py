"""Tests for accepting screenshots, and for the extract-then-review split.

Upload used to be locked to PDF. It now also takes PNG/JPG, and rescues
scanned PDFs — but only ever sends TEXT to OpenRouter, because of the
four free models that accept images, using one would shrink the
operator's model pool to a single provider.

Extraction is therefore its own step:

    POST /api/extract_text   file  -> {text, source, needs_review}
    POST /api/upload_and_parse      accepts `runsheet_text` + `filename`
                                    as an alternative to a file

`needs_review` is the hinge. A text PDF returns False and the UI behaves
exactly as it did before. An image or a scan returns True, and the
operator gets to correct a misread BEFORE spending an OpenRouter request
— which matters because a free account has 50 a day.
"""
import io
from pathlib import Path

import pytest


@pytest.fixture
def extract_client(client, monkeypatch):
    """Client with PDF extraction and OCR both stubbed."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text",
                        lambda _p: "10:00 Welcome\n10:05 Worship")
    monkeypatch.setattr(parse_mod, "render_pdf_pages",
                        lambda _p, **_k: [])
    monkeypatch.setattr(parse_mod, "image_to_text",
                        lambda _p, **_k: "6:30pm    Youth Arrival")
    monkeypatch.setattr(parse_mod, "images_to_text",
                        lambda _p, **_k: "6:30pm    scanned page")
    return client


def _extract(client, filename, data=b"x"):
    return client.post("/api/extract_text",
                       data={"file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


# ── which files are accepted ─────────────────────────────────────────────

def test_text_pdf_extracts_without_review(extract_client):
    r = _extract(extract_client, "service.pdf", b"%PDF-1.4 fake")
    body = r.get_json()
    assert r.status_code == 200
    assert body["source"] == "pdf"
    assert body["needs_review"] is False
    assert "Welcome" in body["text"]


def test_png_is_accepted_and_flagged_for_review(extract_client):
    body = _extract(extract_client, "runsheet.png").get_json()
    assert body["source"] == "ocr"
    assert body["needs_review"] is True
    assert body["text"] == "6:30pm    Youth Arrival"


@pytest.mark.parametrize("name", ["shot.PNG", "photo.jpg", "photo.JPEG"])
def test_image_extensions_are_case_insensitive(extract_client, name):
    assert _extract(extract_client, name).get_json()["source"] == "ocr"


def test_scanned_pdf_falls_back_to_ocr_and_needs_review(extract_client,
                                                        monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text", lambda _p: "   \n ")
    monkeypatch.setattr(parse_mod, "render_pdf_pages",
                        lambda _p, **_k: ["page-0"])
    body = _extract(extract_client, "scan.pdf", b"%PDF-1.4 fake").get_json()
    assert body["source"] == "ocr"
    assert body["needs_review"] is True
    assert body["text"] == "6:30pm    scanned page"


def test_unsupported_extension_is_refused_by_name(extract_client):
    r = _extract(extract_client, "runsheet.docx")
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert ".docx" in err and "PDF" in err


def test_missing_file_is_a_clear_error(extract_client):
    r = extract_client.post("/api/extract_text", data={},
                            content_type="multipart/form-data")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_ocr_unavailable_explains_the_platform_requirement(extract_client,
                                                           monkeypatch):
    """Linux, or an old Windows — there is nothing to install, so the
    message has to carry the whole remedy."""
    import propresenterrunsheet.routes.parse as parse_mod
    from propresenterrunsheet.parsing.ocr import OCRUnavailable

    def unavailable(*_a, **_k):
        raise OCRUnavailable(
            "Reading text from images needs macOS 10.15+ or Windows 10+. "
            "Upload a PDF instead.")
    monkeypatch.setattr(parse_mod, "image_to_text", unavailable)

    r = _extract(extract_client, "runsheet.png")
    assert r.status_code == 400
    assert "macOS" in r.get_json()["error"]


def test_unreadable_image_says_so_plainly(extract_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "image_to_text", lambda _p, **_k: "   ")
    r = _extract(extract_client, "blank.png")
    assert r.status_code == 400
    assert "couldn't read" in r.get_json()["error"].lower()


def test_engine_crash_does_not_leak_a_stack_trace(extract_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod

    def boom(*_a, **_k):
        raise RuntimeError("Vision framework exploded")
    monkeypatch.setattr(parse_mod, "image_to_text", boom)
    r = _extract(extract_client, "runsheet.png")
    assert r.status_code == 400
    assert "Vision framework exploded" not in r.get_json()["error"]


def test_a_library_valueerror_is_not_echoed_to_the_operator(extract_client,
                                                            monkeypatch):
    """ocrmac raises ValueError('Invalid image format…') for input it
    dislikes. Catching bare ValueError would forward that text straight
    to the UI — internals the operator can do nothing with. Only our own
    UploadError messages are passed through."""
    import propresenterrunsheet.routes.parse as parse_mod

    def boom(*_a, **_k):
        raise ValueError("Invalid image format. Image must be a path or PIL")
    monkeypatch.setattr(parse_mod, "image_to_text", boom)
    err = _extract(extract_client, "runsheet.png").get_json()["error"]
    assert "Invalid image format" not in err
    assert "PNG" in err


# ── the filename must never reach the filesystem path ────────────────────

@pytest.mark.parametrize("evil", [
    "../../../../etc/passwd.png",
    "runsheet.png/../../../../tmp/pwned.png",
    "..\\..\\windows\\system32\\evil.png",
    "runsheet\x00.png",
])
def test_a_hostile_filename_cannot_steer_the_temp_path(extract_client,
                                                       monkeypatch, evil):
    """The upload's extension is concatenated into a temp path. Slicing
    it out of the user's string is a path-injection hole, so the value
    used is always a literal from ALLOWED_EXTS."""
    import propresenterrunsheet.routes.parse as parse_mod
    from propresenterrunsheet.config import UPLOAD_FOLDER
    seen = {}

    def capture(path, **_k):
        seen["path"] = path
        return "6:30pm    Youth Arrival"
    monkeypatch.setattr(parse_mod, "image_to_text", capture)

    _extract(extract_client, evil)
    written = Path(seen["path"]).resolve()
    assert written.parent == Path(UPLOAD_FOLDER).resolve()
    assert written.name.startswith("runsheet_")
    assert written.suffix == ".png"


def test_safe_ext_only_ever_returns_a_whitelisted_literal():
    import propresenterrunsheet.routes.parse as parse_mod
    assert parse_mod._safe_ext("a.PNG") == ".png"
    assert parse_mod._safe_ext("../evil.pdf") == ".pdf"
    assert parse_mod._safe_ext("noext") == ""
    assert parse_mod._safe_ext("runsheet.docx") == ""
    assert parse_mod._safe_ext(None) == ""
    for name in ("x.png", "x.jpg", "x.jpeg", "x.pdf"):
        assert parse_mod._safe_ext(name) in parse_mod.ALLOWED_EXTS


def test_the_rejected_extension_is_sanitised_before_being_quoted_back():
    """The error message echoes the extension, so it must not carry
    whatever the caller put in the filename."""
    from propresenterrunsheet.routes.parse import _display_ext
    assert _display_ext("x.docx") == ".docx"
    assert _display_ext("x.<script>alert(1)</script>") == ".scriptalert1scr"[:11]
    assert "/" not in _display_ext("x.pd/../../etc")
    assert _display_ext("noext") == ""


# ── the temp file must not survive ───────────────────────────────────────

def test_upload_temp_file_is_cleaned_up(extract_client, monkeypatch):
    """Uploads land in a temp dir; leaving runsheets there is a privacy
    leak on a shared church machine."""
    import propresenterrunsheet.routes.parse as parse_mod
    seen = {}

    def capture(path, **_k):
        seen["path"] = path
        return "6:30pm    Youth Arrival"
    monkeypatch.setattr(parse_mod, "image_to_text", capture)

    _extract(extract_client, "runsheet.png")
    import os
    assert not os.path.exists(seen["path"])


def test_temp_file_keeps_the_real_extension(extract_client, monkeypatch):
    """Pillow and pdfium both sniff content, but ocrmac opens by path and
    a .pdf suffix on a PNG is a trap for the next person."""
    import propresenterrunsheet.routes.parse as parse_mod
    seen = {}

    def capture(path, **_k):
        seen["path"] = path
        return "text"
    monkeypatch.setattr(parse_mod, "image_to_text", capture)

    _extract(extract_client, "runsheet.png")
    assert seen["path"].lower().endswith(".png")


# ── parse accepts reviewed text instead of a file ────────────────────────

def _parse_with(client, data):
    import requests

    class _Resp:
        status_code = 200
        def json(self):
            return {"model": "test/model:free", "choices": [{"message": {
                "content": '{"service_name":"Youth","items":['
                           '{"title":"Welcome","type":"mc_on_stage"}]}'}}]}
        def raise_for_status(self):
            return None

    sent = {}

    def _fake_post(*_a, **kw):
        sent["payload"] = kw.get("json") or {}
        return _Resp()

    orig = requests.post
    requests.post = _fake_post
    try:
        r = client.post("/api/upload_and_parse", data=data,
                        content_type="multipart/form-data")
        return r, sent
    finally:
        requests.post = orig


def test_parse_accepts_reviewed_text_with_no_file(parse_client):
    r, sent = _parse_with(parse_client, {
        "runsheet_text": "6:30pm  Youth Arrival\n7:00pm  Countdown",
        "filename": "youth.png",
        "or_key": "sk-or-test", "or_model": "test/model:free"})
    assert r.status_code == 200
    assert r.get_json()["items"]
    # The operator's corrected text is what reached the model.
    assert "Youth Arrival" in sent["payload"]["messages"][0]["content"]


def test_edited_text_wins_over_the_uploaded_file(parse_client):
    """If both arrive, the reviewed text is the operator's intent."""
    r, sent = _parse_with(parse_client, {
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "service.pdf"),
        "runsheet_text": "EDITED BY OPERATOR",
        "or_key": "sk-or-test", "or_model": "test/model:free"})
    prompt = sent["payload"]["messages"][0]["content"]
    assert "EDITED BY OPERATOR" in prompt
    assert "10:05 Worship" not in prompt   # the stubbed PDF text


def test_filename_still_drives_the_service_name(parse_client):
    """Template auto-detect and the suggested name both key off it, and
    with no file there is nothing else to derive it from."""
    r, _sent = _parse_with(parse_client, {
        "runsheet_text": "6:30pm  Youth Arrival",
        "filename": "7_Aug_2026 FUN MONTH.png",
        "or_key": "sk-or-test", "or_model": "test/model:free"})
    assert r.get_json()["filename"] == "7_Aug_2026 FUN MONTH.png"


def test_blank_reviewed_text_is_refused_before_spending_a_request(parse_client):
    """The operator cleared the textarea. Burning one of 50 daily free
    requests on an empty runsheet helps nobody."""
    r, _ = _parse_with(parse_client, {
        "runsheet_text": "   ",
        "filename": "x.png",
        "or_key": "sk-or-test", "or_model": "test/model:free"})
    assert r.status_code == 400
