"""Tests for OpenRouter model auto-selection.

Background — why this module exists at all: the app used to ship a hardcoded
default model id (`google/gemini-2.0-flash-exp:free`). OpenRouter retired it,
so every fresh install started 404ing with no way to fix it short of a new
release. The user's workaround — typing `openrouter/free` — made things worse,
because that id is a *router* that picks a free model at random per request,
and roughly one pick in eight is `nvidia/nemotron-3.5-content-safety:free`,
a safety classifier that answers "User Safety: safe" instead of a runsheet.

So we resolve the model from OpenRouter's live catalogue instead, and the
filtering rules below are the ones that would have prevented both failures.
"""
import json

import pytest

from propresenterrunsheet.parsing.models import (
    next_usable_model, pick_default_model, resolve_model, usable_models,
)


def _model(mid, *, ctx=128000, structured=True, free=True, image=False):
    """Build one entry shaped like OpenRouter's /api/v1/models payload."""
    params = ["max_tokens", "temperature", "seed"]
    if structured:
        params += ["response_format", "structured_outputs"]
    mods = ["text"] + (["image"] if image else [])
    price = "0" if free else "0.0000012"
    return {
        "id": mid,
        "name": mid,
        "context_length": ctx,
        "architecture": {"input_modalities": mods, "output_modalities": ["text"]},
        "pricing": {"prompt": price, "completion": price},
        "supported_parameters": params,
    }


def _catalogue(*models):
    return {"data": list(models)}


# ── filtering ────────────────────────────────────────────────────────────────

def test_excludes_models_that_cannot_do_structured_output():
    """The real failure: nemotron-3.5-content-safety advertises no
    structured-output support and replies with a safety verdict, not JSON."""
    cat = _catalogue(
        _model("nvidia/nemotron-3.5-content-safety:free", structured=False),
        _model("google/gemma-4-26b-a4b-it:free", structured=True),
    )
    ids = [m["id"] for m in usable_models(cat)]
    assert "nvidia/nemotron-3.5-content-safety:free" not in ids
    assert "google/gemma-4-26b-a4b-it:free" in ids


def test_excludes_random_routers():
    """`openrouter/free` and `openrouter/auto` advertise structured output but
    delegate to a different model every call — including ones that can't parse
    a runsheet. Auto-selecting a router would reintroduce the original bug."""
    cat = _catalogue(
        _model("openrouter/free"),
        _model("openrouter/auto"),
        _model("google/gemma-4-26b-a4b-it:free"),
    )
    ids = [m["id"] for m in usable_models(cat)]
    assert ids == ["google/gemma-4-26b-a4b-it:free"]


def test_excludes_paid_models():
    """Auto-selection must never silently start spending the user's credit."""
    cat = _catalogue(
        _model("anthropic/claude-sonnet-5", free=False, ctx=999999),
        _model("google/gemma-4-26b-a4b-it:free"),
    )
    ids = [m["id"] for m in usable_models(cat)]
    assert ids == ["google/gemma-4-26b-a4b-it:free"]


def test_ranks_larger_context_first():
    """Runsheet PDFs plus the template-section addendum can run long, so a
    bigger context window is the better default."""
    cat = _catalogue(
        _model("small:free", ctx=8000),
        _model("huge:free", ctx=262144),
        _model("medium:free", ctx=128000),
    )
    assert [m["id"] for m in usable_models(cat)] == [
        "huge:free", "medium:free", "small:free"]


def test_tolerates_missing_and_malformed_entries():
    """The catalogue is third-party JSON fetched at runtime; one weird entry
    must not take out model selection for everyone."""
    cat = {"data": [
        {"id": "broken"},                       # no pricing/params/context
        None,                                   # not even a dict
        {"pricing": {"prompt": "0"}},           # no id
        _model("google/gemma-4-26b-a4b-it:free"),
    ]}
    assert [m["id"] for m in usable_models(cat)] == [
        "google/gemma-4-26b-a4b-it:free"]


def test_empty_catalogue_yields_nothing():
    assert usable_models({"data": []}) == []
    assert usable_models({}) == []


# ── default pick ─────────────────────────────────────────────────────────────

def test_pick_default_returns_best_usable_id():
    cat = _catalogue(
        _model("nvidia/nemotron-3.5-content-safety:free", structured=False,
               ctx=999999),
        _model("small:free", ctx=8000),
        _model("best:free", ctx=262144),
    )
    assert pick_default_model(cat) == "best:free"


def test_pick_default_returns_none_when_nothing_usable():
    """Caller must be able to tell 'no free JSON-capable model exists right
    now' apart from 'here is one', rather than getting a bogus id."""
    cat = _catalogue(
        _model("nvidia/nemotron-3.5-content-safety:free", structured=False))
    assert pick_default_model(cat) is None


# ── resolving what to actually send ──────────────────────────────────────────

def test_unset_model_resolves_to_the_auto_pick():
    """Blank means 'choose for me' — a fresh install should just work without
    the operator knowing any model ids."""
    cat = _catalogue(_model("best:free", ctx=262144), _model("ok:free", ctx=8000))
    assert resolve_model("", cat) == "best:free"


def test_explicit_choice_wins_even_when_not_in_the_free_pool():
    """A deliberately configured model must never be silently overridden — a
    paid model is filtered out of auto-selection but is a legitimate choice."""
    cat = _catalogue(
        _model("anthropic/claude-sonnet-5", free=False),
        _model("best:free", ctx=262144),
    )
    assert resolve_model("anthropic/claude-sonnet-5", cat) == \
        "anthropic/claude-sonnet-5"


def test_model_that_no_longer_exists_falls_back_to_the_auto_pick():
    """The original bug: settings.json still holds the retired
    `google/gemini-2.0-flash-exp:free`, so every parse 404s. If the configured
    id is absent from the live catalogue entirely, it is dead — recover rather
    than fail forever."""
    cat = _catalogue(_model("best:free", ctx=262144))
    assert resolve_model("google/gemini-2.0-flash-exp:free", cat) == "best:free"


def test_explicit_choice_is_kept_when_catalogue_is_unavailable():
    """No catalogue (offline, or the fetch failed) must not wipe out a working
    configured model."""
    assert resolve_model("some/model:free", None) == "some/model:free"


def test_returns_none_when_unset_and_no_catalogue():
    """Nothing configured and nothing to choose from — the caller has to say
    so rather than send an empty model id to OpenRouter."""
    assert resolve_model("", None) is None


# ── the backup pick after a provider-side failure ────────────────────────────
#
# When OpenRouter relays an upstream provider's failure (see the parse-route
# tests), retrying the same id would land on the same broken provider. The
# 2026-08-03 Darkbloom outage broke several free models at once while other
# providers' models kept working — so the neighbour in the ranking is a
# genuine second chance.

def test_next_usable_model_is_the_one_ranked_below_current():
    cat = _catalogue(
        _model("big:free", ctx=262144),
        _model("medium:free", ctx=128000),
        _model("small:free", ctx=8000),
    )
    assert next_usable_model("big:free", cat) == "medium:free"
    assert next_usable_model("medium:free", cat) == "small:free"


def test_next_usable_model_for_an_unranked_id_is_the_top_pick():
    """A paid or hand-typed model isn't in the free ranking at all; the sanest
    backup is simply the best free model."""
    cat = _catalogue(
        _model("anthropic/claude-sonnet-5", free=False, ctx=999999),
        _model("best:free", ctx=262144),
    )
    assert next_usable_model("anthropic/claude-sonnet-5", cat) == "best:free"


def test_next_usable_model_none_when_nothing_else_qualifies():
    """Only the failing model itself is usable — a retry would re-ask the
    exact same thing, so the caller must surface the error instead."""
    cat = _catalogue(_model("only:free"))
    assert next_usable_model("only:free", cat) is None


def test_next_usable_model_none_without_catalogue():
    assert next_usable_model("some/model:free", None) is None


# ── /api/models, which populates the Settings dropdown ───────────────────────

def test_models_route_lists_choices_and_marks_the_automatic_one(
        client, monkeypatch):
    """The Settings UI needs both the pickable models and which one "Automatic"
    would resolve to, so it can label it without duplicating the ranking."""
    import propresenterrunsheet.routes.settings as settings_mod

    monkeypatch.setattr(settings_mod, "fetch_catalogue", lambda *_a, **_k:
                        _catalogue(_model("best:free", ctx=262144),
                                   _model("ok:free", ctx=8000),
                                   _model("nvidia/x-content-safety:free",
                                          structured=False)))
    body = client.get("/api/models").get_json()
    assert body["auto"] == "best:free"
    ids = [m["id"] for m in body["models"]]
    assert ids == ["best:free", "ok:free"]
    assert all("name" in m and "context_length" in m for m in body["models"])


def test_models_route_degrades_gracefully_when_openrouter_is_unreachable(
        client, monkeypatch):
    """Settings must still open offline — an empty list is fine, a 500 is not."""
    import propresenterrunsheet.routes.settings as settings_mod

    monkeypatch.setattr(settings_mod, "fetch_catalogue", lambda *_a, **_k: None)
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.get_json()
    assert body["models"] == []
    assert body["auto"] is None
