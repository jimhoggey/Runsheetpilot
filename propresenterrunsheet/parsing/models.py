"""Resolve which OpenRouter model to use, from their live catalogue.

The app used to ship a hardcoded default model id. OpenRouter retired it,
which meant every fresh install started failing with a 404 that only a new
release could fix. Hardcoding a *list* would have the same problem, just
spread over more ids.

So we ask OpenRouter what exists right now. `GET /api/v1/models` needs no API
key and doesn't count against the free-tier inference quota (20 requests per
minute / 50 per day), which matters — probing candidate models with real
completions would burn that quota on every launch.

The filtering rules below each correspond to a failure actually observed:

  - **must advertise structured output** — `nvidia/nemotron-3.5-content-safety:free`
    is a safety classifier. Asked for a runsheet it replies "User Safety: safe".
    It is the one free model that does *not* list `structured_outputs`, so that
    flag cleanly separates it from the models that work.
  - **no routers** — `openrouter/free` and `openrouter/auto` do advertise
    structured output, but they delegate to a randomly chosen model per
    request, and roughly one pick in eight was the classifier above. Picking a
    router as our default would reintroduce the exact bug we're fixing.
  - **free only** — auto-selection must never quietly start spending credit.
  - **largest context first** — a runsheet plus the template-section addendum
    can run long, so the roomiest window is the safer default.

Note the app sends *text* (pdfplumber extracts it), so image-input support is
not required and is deliberately not filtered on — requiring it would shrink
the pool to a single model for no present benefit.
"""

import logging
import time

log = logging.getLogger("pp_runsheet")

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"

# Ids that dispatch to some other model per-request. Matched as a prefix so a
# future `openrouter/free-v2` is caught too. These are excluded from
# auto-selection but NOT rejected if a user types one deliberately.
_ROUTER_PREFIXES = ("openrouter/",)

# How long a fetched catalogue stays fresh. Models are retired on the order of
# months, so this only needs to be short enough that a long-running install
# notices eventually.
CACHE_TTL_S = 6 * 3600

_cache = {"at": 0.0, "catalogue": None}


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    for field in ("prompt", "completion"):
        try:
            if float(pricing.get(field, "1")) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _supports_structured_output(model: dict) -> bool:
    params = model.get("supported_parameters") or []
    return "structured_outputs" in params


def is_router(model_id: str) -> bool:
    """True for ids that pick a different underlying model per request."""
    return (model_id or "").startswith(_ROUTER_PREFIXES)


def usable_models(catalogue: dict) -> list:
    """Free, non-router models that can return structured JSON, best first.

    `catalogue` is a parsed `/api/v1/models` payload. Entries that are
    malformed (missing keys, not even a dict) are skipped rather than raising —
    this is third-party JSON fetched at runtime, and one bad entry must not
    take out model selection for everyone.
    """
    out = []
    for model in (catalogue or {}).get("data") or []:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not model_id or is_router(model_id):
            continue
        if not _is_free(model) or not _supports_structured_output(model):
            continue
        out.append(model)
    out.sort(key=lambda m: -(m.get("context_length") or 0))
    return out


def pick_default_model(catalogue: dict):
    """The single best free model id, or None if the catalogue offers none.

    Returning None rather than a fallback string is deliberate: the caller has
    to decide what to tell the user, and inventing an id we haven't verified
    exists is how the original hardcoded-default bug happened.
    """
    best = usable_models(catalogue)
    return best[0]["id"] if best else None


def resolve_model(configured: str, catalogue: dict):
    """Decide which model id to send, or None if there's nothing to send.

    An explicitly configured model always wins — including paid ones, which
    auto-selection filters out but which are a perfectly legitimate choice.
    The one exception is a model that has vanished from the catalogue
    entirely: that is the retired-default failure (`gemini-2.0-flash-exp:free`
    404ing forever on installs that saved it), and recovering beats failing on
    every parse until someone edits a setting they don't know exists.

    With no catalogue at all — offline, or the fetch failed — we keep whatever
    is configured rather than second-guessing it.
    """
    configured = (configured or "").strip()
    if catalogue is None:
        return configured or None
    if configured:
        known = {m.get("id") for m in (catalogue.get("data") or [])
                 if isinstance(m, dict)}
        if configured in known:
            return configured
        log.warning("Configured model %r is not in OpenRouter's catalogue "
                    "(retired?) — falling back to automatic selection",
                    configured)
    return pick_default_model(catalogue)


def fetch_catalogue(http_get=None, timeout=10, force=False):
    """Fetch (and cache) the live catalogue. Returns the payload or None.

    Never raises: model discovery is a convenience, and a network blip must not
    stop someone parsing a runsheet with a model they already have configured.
    """
    now = time.time()
    if not force and _cache["catalogue"] is not None and \
            now - _cache["at"] < CACHE_TTL_S:
        return _cache["catalogue"]
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        resp = http_get(CATALOGUE_URL, timeout=timeout)
        resp.raise_for_status()
        catalogue = resp.json()
    except Exception as e:
        log.warning("Could not fetch OpenRouter model catalogue (%s: %s)",
                    type(e).__name__, e)
        return _cache["catalogue"]
    _cache.update(at=now, catalogue=catalogue)
    return catalogue


def reset_cache():
    """Drop the cached catalogue (tests, and the Settings 'refresh' action)."""
    _cache.update(at=0.0, catalogue=None)
