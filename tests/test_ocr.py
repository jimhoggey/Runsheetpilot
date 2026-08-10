"""Tests for local OCR of screenshots and scanned runsheets.

The load-bearing fact these tests encode (established by running Apple
Vision against a synthetic runsheet during design): **the OS OCR engines
return one observation per table CELL, in COLUMN-MAJOR order** — every
time value first, then every item name, then every duration. Real output
from Vision on a runsheet screenshot began:

    '6:30pm'  '7:00pm'  '7:05pm' ... 'Youth Arrival + Hangout'  'Countdown' ...

Handed to the model in that order it is unusable. So the value of this
module is not "call the OCR engine" — it is `observations_to_text`,
which rebuilds rows from the bounding boxes. That function is pure
geometry over plain dicts, which is why it carries most of the tests
here and why the real engines are never touched by the suite.

Coordinates are always TOP-LEFT origin by the time they reach
`observations_to_text`. Apple Vision reports bottom-left, so the Mac
backend flips; Windows.Media.Ocr is already top-left and passes through.
"""
import pytest

from propresenterrunsheet.parsing.ocr import (
    OCRUnavailable, observations_to_text, vision_to_observations,
    winocr_to_observations, image_to_text, images_to_text, pick_backend,
)


def obs(text, x, y, w=10.0, h=10.0):
    return {"text": text, "x": x, "y": y, "w": w, "h": h}


# ── observations_to_text: the part that decides quality ──────────────────

def test_column_major_cells_are_rebuilt_into_rows():
    """The real Vision failure mode: all of column 1, then all of column 2."""
    cells = [
        obs("6:30pm", 0, 100), obs("7:00pm", 0, 200), obs("7:05pm", 0, 300),
        obs("Youth Arrival", 200, 100), obs("Countdown", 200, 200),
        obs("Welcome", 200, 300),
    ]
    assert observations_to_text(cells).splitlines() == [
        "6:30pm    Youth Arrival",
        "7:00pm    Countdown",
        "7:05pm    Welcome",
    ]


def test_rows_are_ordered_top_to_bottom_regardless_of_input_order():
    cells = [obs("last", 0, 500), obs("first", 0, 100), obs("middle", 0, 300)]
    assert observations_to_text(cells).splitlines() == ["first", "middle", "last"]


def test_cells_within_a_row_are_ordered_left_to_right():
    cells = [obs("third", 400, 100), obs("first", 0, 100), obs("second", 200, 100)]
    assert observations_to_text(cells) == "first    second    third"


def test_rows_tolerate_slight_vertical_misalignment():
    """OCR never reports cells in a row at pixel-identical y."""
    cells = [obs("6:30pm", 0, 100, h=10), obs("Youth Arrival", 200, 103, h=10),
             obs("30", 400, 98, h=10)]
    assert observations_to_text(cells) == "6:30pm    Youth Arrival    30"


def test_a_genuinely_lower_line_starts_a_new_row():
    cells = [obs("6:30pm", 0, 100, h=10), obs("7:00pm", 0, 140, h=10)]
    assert observations_to_text(cells).splitlines() == ["6:30pm", "7:00pm"]


def test_ragged_rows_keep_only_the_cells_they_have():
    """A blank Notes cell must not shift the next row's text into it."""
    cells = [
        obs("7:36pm", 0, 100), obs("Culture Moment", 200, 100),
        obs("7:40pm", 0, 200), obs("Message", 200, 200), obs("Ps Jim", 400, 200),
    ]
    assert observations_to_text(cells).splitlines() == [
        "7:36pm    Culture Moment",
        "7:40pm    Message    Ps Jim",
    ]


def test_row_tolerance_scales_with_glyph_height_not_fixed_pixels():
    """Same layout at 10x resolution must produce identical text.

    A fixed pixel tolerance would merge every row of a small screenshot
    or split every row of a retina one. Tolerance derives from median
    glyph height instead.
    """
    small = [obs("a", 0, 10, w=5, h=5), obs("b", 20, 11, w=5, h=5),
             obs("c", 0, 30, w=5, h=5)]
    large = [obs("a", 0, 100, w=50, h=50), obs("b", 200, 110, w=50, h=50),
             obs("c", 0, 300, w=50, h=50)]
    assert observations_to_text(small) == observations_to_text(large) == "a    b\nc"


def test_a_tall_heading_above_the_table_stays_on_its_own_line():
    cells = [obs("YOUTH SERVICE", 0, 10, w=300, h=30),
             obs("TIME", 0, 100, h=10), obs("ITEM", 200, 100, h=10)]
    assert observations_to_text(cells).splitlines() == [
        "YOUTH SERVICE", "TIME    ITEM"]


def test_blank_and_whitespace_only_observations_are_dropped():
    cells = [obs("6:30pm", 0, 100), obs("   ", 200, 100), obs("", 400, 100)]
    assert observations_to_text(cells) == "6:30pm"


def test_no_observations_gives_empty_string():
    assert observations_to_text([]) == ""


def test_all_blank_observations_give_empty_string():
    """Must not raise on the median-height calculation with nothing left."""
    assert observations_to_text([obs(" ", 0, 0), obs("", 10, 0)]) == ""


def test_cell_text_is_stripped_but_inner_spacing_kept():
    cells = [obs("  Praise and Worship  ", 0, 100)]
    assert observations_to_text(cells) == "Praise and Worship"


def test_songs_in_a_notes_column_survive_reconstruction():
    """The layout DEFAULT_PROMPT already knows how to read."""
    cells = [
        obs("7:11pm", 0, 100), obs("Praise and Worship", 200, 100),
        obs("25", 600, 100),
        obs("Songs: Great Are You Lord, Build My Life", 700, 100),
    ]
    assert observations_to_text(cells) == (
        "7:11pm    Praise and Worship    25    "
        "Songs: Great Are You Lord, Build My Life")


# ── backend coordinate normalisation ─────────────────────────────────────

def test_vision_observations_are_flipped_to_top_left_origin():
    """Apple Vision reports bottom-left origin, normalised 0..1.

    A box at y=0.9 with height 0.05 is near the TOP of the page, so it
    must come out with a small top-left y.
    """
    raw = [("TITLE", 1.0, (0.05, 0.90, 0.30, 0.05)),
           ("bottom row", 1.0, (0.05, 0.10, 0.30, 0.05))]
    out = vision_to_observations(raw)
    assert out[0]["text"] == "TITLE"
    assert out[0]["y"] == pytest.approx(0.05)
    assert out[1]["y"] == pytest.approx(0.85)


def test_vision_flip_then_reconstruct_puts_the_title_first():
    """End-to-end guard on the flip: get it backwards and the runsheet
    comes out upside down, which is silent and very hard to spot."""
    raw = [("7:00pm", 1.0, (0.04, 0.60, 0.06, 0.04)),
           ("HEADING", 1.0, (0.04, 0.90, 0.30, 0.04)),
           ("6:30pm", 1.0, (0.04, 0.70, 0.06, 0.04))]
    assert observations_to_text(vision_to_observations(raw)).splitlines() == [
        "HEADING", "6:30pm", "7:00pm"]


def test_winocr_lines_pass_through_as_top_left_boxes():
    """Windows.Media.Ocr is already top-left, in pixels."""
    result = {"lines": [
        {"text": "6:30pm", "words": [
            {"bounding_rect": {"x": 10, "y": 100, "width": 60, "height": 20}}]},
        {"text": "Youth Arrival", "words": [
            {"bounding_rect": {"x": 200, "y": 102, "width": 180, "height": 20}}]},
    ]}
    out = winocr_to_observations(result)
    assert out[0] == {"text": "6:30pm", "x": 10.0, "y": 100.0,
                      "w": 60.0, "h": 20.0}
    assert observations_to_text(out) == "6:30pm    Youth Arrival"


def test_winocr_line_box_spans_all_its_words():
    """A multi-word line's box must cover the whole line, not word one."""
    result = {"lines": [{"text": "Youth Arrival Hangout", "words": [
        {"bounding_rect": {"x": 200, "y": 100, "width": 50, "height": 20}},
        {"bounding_rect": {"x": 260, "y": 100, "width": 60, "height": 20}},
        {"bounding_rect": {"x": 330, "y": 100, "width": 70, "height": 20}}]}]}
    out = winocr_to_observations(result)
    assert out[0]["x"] == 200.0
    assert out[0]["w"] == 200.0   # 330 + 70 - 200


def test_winocr_line_without_words_is_skipped_not_fatal():
    result = {"lines": [{"text": "orphan", "words": []},
                        {"text": "ok", "words": [
                            {"bounding_rect": {"x": 0, "y": 0,
                                               "width": 10, "height": 10}}]}]}
    assert [o["text"] for o in winocr_to_observations(result)] == ["ok"]


# ── backend selection ────────────────────────────────────────────────────

def test_unsupported_platform_raises_ocr_unavailable():
    with pytest.raises(OCRUnavailable):
        pick_backend(platform="linux")


def test_ocr_unavailable_message_names_both_supported_platforms():
    """The operator has to know what to do — the message is the whole
    remedy, since there is nothing to install."""
    with pytest.raises(OCRUnavailable) as e:
        pick_backend(platform="linux")
    msg = str(e.value)
    assert "macOS" in msg and "Windows" in msg and "PDF" in msg


def test_image_to_text_uses_the_injected_backend():
    def fake(path):
        assert path == "/tmp/shot.png"
        return [obs("6:30pm", 0, 100), obs("Welcome", 200, 100)]
    assert image_to_text("/tmp/shot.png", backend=fake) == "6:30pm    Welcome"


def test_image_to_text_propagates_ocr_unavailable():
    def fake(path):
        raise OCRUnavailable("nope")
    with pytest.raises(OCRUnavailable):
        image_to_text("/tmp/shot.png", backend=fake)


# ── multi-page (scanned PDF) ─────────────────────────────────────────────

def test_pages_are_joined_with_a_blank_line():
    pages = {"p1": [obs("6:30pm", 0, 100)], "p2": [obs("8:10pm", 0, 100)]}
    assert images_to_text(["p1", "p2"], backend=pages.get) == "6:30pm\n\n8:10pm"


def test_a_blank_page_does_not_leave_a_gap():
    """A scan often has an empty trailing page; it must not read as a
    paragraph break the model might treat as a section boundary."""
    pages = {"p1": [obs("6:30pm", 0, 100)], "p2": [], "p3": [obs("8:10pm", 0, 100)]}
    assert images_to_text(["p1", "p2", "p3"],
                          backend=pages.get) == "6:30pm\n\n8:10pm"


def test_no_pages_gives_empty_string():
    assert images_to_text([], backend=lambda s: []) == ""
