# Screenshot upload via local OCR, and a "Populate with media from PP" toggle

Date: 2026-08-10
Status: approved

## Why

Two unrelated gaps, shipped together because both touch the upload → parse path.

**Screenshots.** Upload is locked to PDF. Operators often have the runsheet as a
screenshot, or as a scanned PDF that pdfplumber cannot read — today that is a
dead end with "make sure it is a text-based PDF".

**Matching that isn't wanted.** For a brand-new event there is no template and
no reusable media, so every match attempt is noise. The only thing the app is
being asked for is coloured headers in ProPresenter. There is currently no way
to ask for just that.

## Principle: OpenRouter only ever receives text

Images are converted to text *locally* and the text is sent. This is deliberate,
not incidental:

- The free-model pool stays at its full size. Of the free models that pass the
  current filters, exactly **one** accepts image input
  (`google/gemma-4-26b-a4b-it:free`); relaxing the structured-output filter
  raises that to about four. Text keeps every free model available.
- No per-parse cost, and no change to the 90s timeout, the provider-failover
  logic, or the prompt.
- The operator can see and correct what was read before spending a request.

## Behaviour

| Input | Today | After |
|---|---|---|
| Text PDF | pdfplumber → AI | **unchanged** |
| Scanned / image-only PDF | error | rasterise → OCR → review → AI |
| PNG / JPG | rejected | OCR → review → AI |
| Matching | always on | toggle in sidebar, on by default, resets each new file |

Out of scope: `.docx`, `.doc`, HEIC, and multi-image uploads (one image per
upload).

## OCR

### Engine

OS-native, behind one interface in `parsing/ocr.py`:

- **Mac** — `ocrmac` (Apple Vision), macOS 10.15+
- **Windows** — `winocr` (Windows.Media.Ocr), Windows 10+
- **Neither** — raise `OCRUnavailable`; the route turns it into "Screenshots
  need macOS 10.15+ or Windows 10+. Upload a PDF instead." This is also the
  Linux/CI path, so tests inject a fake backend and never touch a real engine.

Chosen over RapidOCR (one engine everywhere, but 60–100MB — on the Windows
`--onefile` build that reverses the launch-time work) and bundled Tesseract
(ship and sign the binary yourself; weakest on screenshots).

Measured footprint: the whole Windows `winrt` OCR stack is **1.0MB** —
`winocr` 0.0.15 depends on granular `winrt-*` packages, not the 12MB monolithic
`winsdk`. On Mac, `pywebview` already pulls pyobjc for WebKit, so the Vision
wrapper adds ~0.1MB. Plus `pypdfium2` at 5.2MB for rasterising. **~6MB total.**

### Layout reconstruction — the part that decides quality

Verified against Apple Vision on a synthetic runsheet: Vision returns
**per-cell** observations in **column-major order** — every time value first,
then every item name. Passed to the model raw it is unusable. Reconstruction is
therefore the feature, not a refinement.

Normalised to `(text, x, y, w, h)` with a top-left origin (Vision's origin is
bottom-left and is flipped by the Mac backend; Windows is already top-left),
then:

1. Cluster observations into rows by vertical-centre proximity, with a tolerance
   derived from **median glyph height** — not a fixed pixel value, so it
   survives any screenshot resolution.
2. Sort left-to-right within each row.
3. Join cells with a separator wide enough to read as columns.

Confirmed output on the test image:

```
6:30pm    Youth Arrival + Hangout         30   PreLoop Youth
7:11pm    Praise and Worship              25   Songs: Great Are You Lord, ...
```

This is exactly the shape `DEFAULT_PROMPT` already handles, including songs
carried in a notes column.

Pure geometry over plain tuples, so it is the most testable piece here and gets
the heaviest test coverage.

### Scanned PDFs

`extract_pdf_text` is **untouched**. Only when it returns empty — today's dead
end — do we rasterise with `pypdfium2` and OCR the pages. A PDF that yields text
never reaches OCR code, so reconstruction cannot regress existing PDF parsing.

## Review-and-edit step

Extraction splits out of parsing:

- **New** `POST /api/extract_text` — file in, `{text, source, needs_review}` out.
- **`/api/upload_and_parse`** gains an alternative input: `runsheet_text` plus
  `filename` (the filename still feeds template auto-detect and the service
  name). The existing file-upload path keeps working unchanged.

Flow: choose a file → `/api/extract_text` runs immediately. A text PDF returns
`needs_review: false` and the UI behaves exactly as today. An image or scanned
PDF shows an editable textarea in Step 2 above Parse; the operator fixes any
misread and Parse sends *their* text. A bad read costs a keystroke, not an
OpenRouter request.

## The toggle

Sidebar, **Template Playlist** section, directly under *↻ Refresh playlists from
PP*, labelled **"Populate with media from PP"**. On by default. When off, the
template dropdown and refresh button above it grey out, so the section reads as
one inert block.

Off means **headers only**, at both ends:

- **Parse** skips template auto-detect, template fetch, the library-names prompt
  addendum, `link_items_to_template`, and song fuzzy matching — **zero
  ProPresenter calls during parse**, so it is faster and works with PP closed.
- **Create** skips the `link_items_to_template` rescue and the media-bin relink,
  and builds one coloured header per item.
- **Timers are still created** — they come from the runsheet's own durations,
  not from ProPresenter.

Re-match is itself an explicit "do the matching now" action, so it stays
available and switches the toggle back on when used.

**Deliberate inconsistency:** every other sidebar control persists to
`settings.json`. This one does not — it resets to on whenever a new file is
chosen and on Start Over, with a visible "Resets on each new runsheet" hint. A
matching switch that silently stayed off until next Sunday is the worse failure.

## Files

| File | Change |
|---|---|
| `propresenterrunsheet/parsing/ocr.py` | **new** — backends, `OCRUnavailable`, reconstruction |
| `propresenterrunsheet/parsing/pdf.py` | add page rasteriser; `extract_pdf_text` unchanged |
| `propresenterrunsheet/routes/parse.py` | accept images; `/api/extract_text`; `runsheet_text`; honour toggle |
| `propresenterrunsheet/routes/playlist.py` | honour toggle at create |
| `templates/index.html` | accept attr, copy, toggle, review panel |
| `static/app.js` | file validation, review panel, toggle state + reset |
| `requirements.txt` | `ocrmac` (darwin), `winocr` (win32), `pypdfium2` |
| `build_mac.sh`, `build_win.bat` | hidden imports / collect flags |

## Testing

- Reconstruction: fake observations → expected rows. Column-major input,
  ragged rows, blank cells, varying glyph heights, single-column text.
- Backend selection: fake backends; `OCRUnavailable` on an unsupported platform.
- Routes: `/api/extract_text` for each input kind; `runsheet_text` path;
  toggle off makes **zero** ProPresenter calls at parse.
- Create with toggle off: header-per-item payload, timers still present.
- Existing PDF tests must pass untouched — that is the regression guard.

## Risks

- Real OCR engines are unavailable in CI; all tests use injected fakes, so
  engine bugs surface only on real hardware. Mitigated by the spike above,
  which exercised the real Vision path end to end.
- `winocr` is unverified on real Windows hardware from this machine; the
  Windows CI smoke test is the gate.
