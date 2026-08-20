"""Tests for the model shortlist and key-funding detection.

Two rules carry the design:

  * **Automatic is free-only, always.** It has to work on a fresh
    install with an unfunded key, and it must never start spending
    money without being asked.
  * **Paid models are offered only to a key that can pay.** Offering
    them otherwise produces a 402 on the operator's first parse — a much
    worse first impression than a shorter list. "Couldn't tell" counts
    as can't-pay.
"""
import pytest

from propresenterrunsheet.parsing import models as m


CATALOGUE = {"data": [
    {"id": "free/one:free", "context_length": 100000,
     "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "openai/gpt-4.1-mini", "context_length": 1000000,
     "pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "qwen/qwen3-30b-a3b-instruct-2507", "context_length": 262144,
     "pricing": {"prompt": "0.00000005", "completion": "0.00000019"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "anthropic/claude-haiku-4.5", "context_length": 200000,
     "pricing": {"prompt": "0.000001", "completion": "0.000005"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "openrouter/auto", "context_length": 2000000,
     "pricing": {"prompt": "-1", "completion": "-1"},
     "supported_parameters": ["structured_outputs"]},
]}


# ── automatic never spends ───────────────────────────────────────────────

def test_automatic_picks_a_free_model_even_when_paid_ones_exist():
    """The catalogue above is mostly paid; automatic must still land on
    the free one."""
    assert m.pick_default_model(CATALOGUE) == "free/one:free"


def test_automatic_never_returns_a_router():
    """openrouter/auto dispatches to a different model per request, so
    it can't be the unattended default — that was the original bug."""
    assert m.pick_default_model(CATALOGUE) != "openrouter/auto"


# ── the shortlist ────────────────────────────────────────────────────────

def test_the_shortlist_stays_short():
    """A wall of models is not a choice, it's a research project."""
    assert 2 <= len(m.RECOMMENDED) <= 6


def test_exactly_one_model_is_starred():
    assert sum(1 for r in m.RECOMMENDED if r["starred"]) == 1


def test_every_recommendation_explains_itself():
    for r in m.RECOMMENDED:
        assert r["why"].strip() and r["label"].strip(), r["id"]


def test_recommendations_are_priced_from_the_live_catalogue():
    """Hardcoded prices go stale silently; these are computed."""
    by_id = {r["id"]: r for r in m.recommended_models(CATALOGUE)}
    assert by_id["openai/gpt-4.1-mini"]["cost_per_parse"] == pytest.approx(
        0.0000004 * m._EST_PROMPT_TOKENS + 0.0000016 * m._EST_COMPLETION_TOKENS)
    # Cheapest of the three real models, as observed on a live run.
    assert (by_id["qwen/qwen3-30b-a3b-instruct-2507"]["cost_per_parse"]
            < by_id["openai/gpt-4.1-mini"]["cost_per_parse"])


def test_a_router_has_no_price_of_its_own():
    """Routers carry sentinel pricing (-1); computing with it yields a
    confident, enormous, NEGATIVE number."""
    by_id = {r["id"]: r for r in m.recommended_models(CATALOGUE)}
    assert by_id["openrouter/auto"]["cost_per_parse"] is None


def test_a_recommendation_missing_from_the_catalogue_is_dropped():
    """Offering an id that 404s is the exact failure this module exists
    to prevent."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "anthropic/claude-haiku-4.5"]}
    ids = [r["id"] for r in m.recommended_models(thin)]
    assert "anthropic/claude-haiku-4.5" not in ids
    assert "openai/gpt-4.1-mini" in ids


def test_nothing_survives_a_catalogue_that_omits_it_not_even_the_router():
    """No exceptions. A hardcoded id that OpenRouter has withdrawn is
    exactly how this app once shipped a default that 404'd on every
    install — and an operator cannot diagnose that."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "openrouter/auto"]}
    assert "openrouter/auto" not in [r["id"] for r in m.recommended_models(thin)]


def test_the_star_moves_when_the_starred_model_is_withdrawn():
    """There is always exactly one recommendation, or the group is a
    list with nothing marked."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "openai/gpt-4.1-mini"]}
    out = m.recommended_models(thin)
    assert sum(1 for r in out if r["starred"]) == 1
    assert out[0]["starred"] is True


def test_an_unreachable_catalogue_offers_nothing():
    """Offline must not fall back to a hardcoded list — that IS the
    stale list this guards against."""
    assert m.recommended_models(None) == []
    assert m.recommended_models({}) == []


@pytest.mark.parametrize("model_id", [r["id"] for r in m.RECOMMENDED])
def test_every_recommended_id_exists_on_openrouter_today(model_id):
    """A live check, skipped when offline. This is the test that catches
    a shortlist going stale between releases — the runtime filter hides
    it from operators, but the maintainer should still be told."""
    catalogue = m.fetch_catalogue()
    if not catalogue:
        pytest.skip("OpenRouter unreachable")
    ids = {d.get("id") for d in catalogue.get("data") or []}
    assert model_id in ids, (
        f"{model_id} is no longer on OpenRouter — update RECOMMENDED")


# ── funding detection ────────────────────────────────────────────────────

def _key_response(payload):
    class R:
        status_code = 200
        def json(self): return payload
        def raise_for_status(self): return None
    return lambda *a, **k: R()


def test_a_paid_key_is_detected_as_funded():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response(
        {"data": {"is_free_tier": False, "usage": 0.0033}}))
    assert info["funded"] is True
    assert info["usage"] == pytest.approx(0.0033)


def test_a_free_tier_key_is_detected():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response(
        {"data": {"is_free_tier": True, "usage": 0}}))
    assert info["funded"] is False


def test_no_key_means_unknown_not_funded():
    assert m.fetch_key_info("")["funded"] is None


def test_an_unreachable_openrouter_means_unknown():
    """Offline must degrade to the free list, never to a paid default."""
    def boom(*a, **k):
        raise OSError("no network")
    assert m.fetch_key_info("sk-or-x", http_get=boom)["funded"] is None


def test_a_missing_tier_field_means_unknown():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response({"data": {}}))
    assert info["funded"] is None
