"""OpenRouter prompt template + JSON-cleanup helpers for runsheet parsing.

The default prompt sent to the model lives here as a constant; the user
can override it via the UI ("Edit AI Prompt" modal) and their version is
persisted in settings. The route handler still drives the actual HTTP
call — this module owns the template + the response-cleanup that
extracts JSON from whatever the model returns (some models wrap output
in markdown fences, some emit a bare array, some emit a full object)."""

import json
import re
from typing import Optional


# The fixed list of runsheet item types. The model is TOLD to use exactly
# these (see the TYPES section of DEFAULT_PROMPT), but prompt wording is
# not enforcement — a live parse once came back with the literal type
# "worship and ministry time" (the whole item title), which broke the tag
# colours and the Service Mate cue lookup. canonicalize_item_type() is the
# enforcement: whatever the model emits, only these six survive.
ALLOWED_ITEM_TYPES = ("song", "mc_on_stage", "announcement", "sermon",
                      "prayer_and_ministry", "other")

# Word → canonical type. Applied token-by-token to whatever the model
# invented, most-specific first (a "song" token wins over a "ministry"
# token so "worship songs" stays a song). "worship" maps to
# prayer_and_ministry because the operator's definition folds ministry
# time — including the worship-block header — into that type; actual
# songs are tagged "song" individually.
_TYPE_SYNONYM_TOKENS = (
    ("song",                ("song", "songs")),
    ("mc_on_stage",         ("mc", "host", "emcee")),
    ("sermon",              ("sermon", "preach", "preaching", "message")),
    ("announcement",        ("announcement", "announcements", "notice",
                             "notices")),
    ("prayer_and_ministry", ("prayer", "ministry", "altar", "worship")),
)


def canonicalize_item_type(t) -> str:
    """Clamp a model-emitted type to ALLOWED_ITEM_TYPES.

    Exact members pass through (after trimming/casing/space→underscore
    normalisation, so "MC on stage" works). Everything else is matched by
    token against the synonym table above; anything unrecognised —
    including the retired scripture/offering/video types — becomes
    "other". Never raises: this runs on model output.
    """
    if not isinstance(t, str):
        return "other"
    norm = re.sub(r"[^\w]+", "_", t.strip().lower()).strip("_")
    if norm in ALLOWED_ITEM_TYPES:
        return norm
    tokens = set(norm.split("_"))
    for canonical, words in _TYPE_SYNONYM_TOKENS:
        if tokens & set(words):
            return canonical
    return "other"


# Fed to the model with `{RUNSHEET}` replaced by the extracted PDF text.
DEFAULT_PROMPT = """\
You are analysing a church service runsheet (order of service).

## WHAT TO SKIP
Most runsheets have a "rostering" section at the very top that lists who
is doing each role (lines like "Pre Service Prayer: Taylor, Jordan",
"Worship Leader: Rivera, Sam", "Speaker: Walker, Alex", "ML Open: ...").
This is just credits — IGNORE IT COMPLETELY.

The actual service begins at the FIRST item that has a specific time-of-day
(e.g. "9:24 AM"). Start extracting from THAT item onward.

Also IGNORE any footer sections that come AFTER the last service item —
typically things like "Rehearsal Times", "Songs", "Tech Notes", lists of
upcoming dates that aren't part of the service flow, etc.

## RETURN FORMAT — JSON object only, no markdown:

{
  "service_name": "<short name combining the service title and date,
                    e.g. 'Sunday Service — 3 May 2026'>",
  "items": [
    {"type":         "<see TYPES below>",
     "title":        "...",
     "notes":        "...",
     "duration_min": <integer minutes, or 0 if not specified>}
  ]
}

## DURATION_MIN
Most runsheets list a duration next to each item (e.g. "9:30 AM 20 Worship
and Ministry Time" — the 20 is duration in minutes; "10:14 AM 30 Preach Title"
— 30 minutes). Always extract this as an integer in `duration_min`.
Use 0 if there's no explicit duration. This field drives countdown-timer
creation in ProPresenter.

## TYPES — a FIXED list. `type` MUST be EXACTLY one of these six strings:
## song, mc_on_stage, announcement, sermon, prayer_and_ministry, other
## NEVER invent a new type. NEVER use the item's title as its type.

- song          ONLY actual sung worship songs the band/team performs.
                Examples: "Amazing Grace", "Alleluia", "The King Is In The
                Room". Often listed back-to-back with short or zero duration.
                ⚠ DO NOT use "song" for items that mention a person's name —
                those are MC moments, not songs.

- mc_on_stage   An MC / host on stage: landing worship, welcome and
                connection cards, culture moments, interviews, transitions.
                Often has a person's name with a dash.
                Examples: "Land Worship - Priya", "Welcome and Connection
                Cards", "Culture Moment - Generosity - Ps Sarah",
                "Meeting Land and Recap - Chris", an interview segment.

- announcement  Information given to the congregation.
                Examples: "Junior Youth Out", "Upcoming Dates",
                "Celebrations", "Whats Your Next Step Moment".

- sermon        The main preaching / message slot. Look for "Preach Title",
                "Message", or a minister's name with a sermon topic.

- prayer_and_ministry
                The altar call / ministry moment (commonly right after the
                sermon), a prayer time, or a ministry time — including the
                "Worship and Ministry Time" block near the top of many
                runsheets.

- other         Anything that fits none of the above: go live / streaming,
                countdowns, music beds, section dividers, logistics,
                scripture readings, offering, videos.

## NOTES FIELD
Include any time-of-day (e.g. "9:30 AM") and speaker names in the notes
field. The duration goes in `duration_min`, NOT in notes.
Use empty string ("") if there is no extra info.

## EXAMPLE
{"service_name":"Sunday Service — 3 May 2026",
 "items":[
   {"type":"other","title":"Go live - online streaming","notes":"9:24 AM","duration_min":1},
   {"type":"other","title":"Countdown - Start 9:27am","notes":"9:25 AM","duration_min":5},
   {"type":"prayer_and_ministry","title":"Worship and Ministry Time","notes":"9:30 AM","duration_min":20},
   {"type":"song","title":"Alleluia","notes":"9:50 AM","duration_min":0},
   {"type":"song","title":"The King Is In The Room","notes":"","duration_min":0},
   {"type":"song","title":"Jesus Be The Name","notes":"","duration_min":0},
   {"type":"mc_on_stage","title":"Land Worship - Priya","notes":"9:50 AM","duration_min":5},
   {"type":"other","title":"Genesis 1:23-28","notes":"9:55 AM","duration_min":2},
   {"type":"mc_on_stage","title":"Welcome and Connection Cards","notes":"9:55 AM","duration_min":5},
   {"type":"mc_on_stage","title":"Culture Moment - Generosity - Ps Sarah","notes":"10:00 AM","duration_min":10},
   {"type":"announcement","title":"Junior Youth Out","notes":"10:10 AM","duration_min":1},
   {"type":"sermon","title":"Preach: King Jesus - Ps David","notes":"10:14 AM","duration_min":30},
   {"type":"prayer_and_ministry","title":"Altar Call/Ministry Moment","notes":"10:44 AM","duration_min":5},
   {"type":"mc_on_stage","title":"Meeting Land and Recap - Chris","notes":"10:49 AM","duration_min":2},
   {"type":"announcement","title":"Upcoming Dates","notes":"10:53 AM","duration_min":5}
 ]}

RUNSHEET:
---
{RUNSHEET}
---
"""


# Append to the prompt to ask the model for per-role cue lines (used by the
# Service Mate clocks). Kept separate so user-customised prompts in the UI
# don't accidentally lose this — the route always glues this on at send.
SERVICE_MATE_CUE_ADDENDUM = (
    "\n\nADDITIONAL FIELD — `cues`:\n"
    "For EACH item, also include a `cues` object with three short "
    "imperative phrases (≤ 40 chars each) telling the operator at "
    "that station what to do when this item is current:\n"
    "  - cues.screen  — what the SCREEN/lyric op should cue next\n"
    "  - cues.sound   — what the SOUND op should do (which mics on/off)\n"
    "  - cues.lights  — what the LIGHTS op should do\n"
    "Use the title, speaker names, and notes for specificity. "
    "Examples:\n"
    "  cues.screen = \"Slide — Build My Life\"\n"
    "  cues.sound  = \"Mic on for Ps David\"\n"
    "  cues.lights = \"Spot — preacher\"\n"
    "If you can't tell, leave the field as an empty string."
)


# Appended to the prompt when the operator has a TEMPLATE PLAYLIST set up
# in ProPresenter — a reusable playlist with section headers like "Culture",
# "Welcome", "Worship", each followed by the slides used every week. The
# model picks an EXACT section name; the route resolves the name to a
# section and the playlist builder expands the runsheet item into the
# section's slides. Operators stop recreating the same content weekly.
LIBRARY_CONTEXT_ADDENDUM = (
    "\n\nADDITIONAL FIELD — `library_match`:\n"
    "Below is a list of SECTION names from the operator's template playlist\n"
    "in ProPresenter. Each section already contains the slides we want to\n"
    "show for that part of the service — we will paste them into the new\n"
    "playlist whenever a runsheet item clearly corresponds to one.\n\n"
    "For EACH runsheet item, decide whether it CLEARLY corresponds to one\n"
    "of these sections (e.g. runsheet 'Culture Moment - Generosity - Ps\n"
    "Sarah' → section 'Culture'; runsheet 'Welcome and Connection Cards'\n"
    "→ section 'Welcome'). If yes, set `library_match` to the EXACT section\n"
    "name from the list below (copy-paste it). If no clear match exists,\n"
    "set `library_match` to an empty string.\n\n"
    "Be conservative — only match when the runsheet item is clearly the\n"
    "same content the section was built for. Don't try to match every item;\n"
    "songs, action items, scripture readings rarely have a section match.\n\n"
    "SECTIONS:\n{LIBRARY_NAMES}\n"
)

# Cap how many library names we send to the model — most runsheets only
# need a handful, and a 2k-item library would balloon the prompt.
LIBRARY_NAMES_MAX = 200


def assemble_prompt(template: str, runsheet_text: str,
                    library_names: Optional[list] = None) -> str:
    """Substitute the runsheet text into the user's (or default) template,
    appending the Service Mate cue addendum so the model also emits
    per-role cue lines. If `library_names` is provided, also append the
    library-context addendum so the model can tag items with the
    name of an existing presentation it should reuse."""
    if "{RUNSHEET}" in template:
        prompt = template.replace("{RUNSHEET}", runsheet_text)
    else:
        prompt = f"{template}\n\nRUNSHEET:\n---\n{runsheet_text}\n---"
    if library_names:
        # Sorted + capped so the prompt is stable across runs (helps caching
        # on the OpenRouter side) and never blows the context window.
        names = sorted({(n or "").strip() for n in library_names if n})
        names = [n for n in names if n][:LIBRARY_NAMES_MAX]
        if names:
            block = "\n".join(f"- {n}" for n in names)
            prompt += LIBRARY_CONTEXT_ADDENDUM.replace("{LIBRARY_NAMES}", block)
    return prompt + SERVICE_MATE_CUE_ADDENDUM


def parse_ai_response(content: str):
    """Pull the JSON out of an OpenRouter response. Handles markdown fences
    and either a `{service_name, items}` object or a bare items array.
    Returns (items, service_name) or raises a json error."""
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    m_obj = re.search(r"\{.*\}", content, re.DOTALL)
    m_arr = re.search(r"\[.*\]", content, re.DOTALL)
    if m_obj:
        data = json.loads(m_obj.group())
    elif m_arr:
        data = json.loads(m_arr.group())
    else:
        data = json.loads(content)
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        return data.get("items", []), (data.get("service_name") or "").strip()
    return [], ""
