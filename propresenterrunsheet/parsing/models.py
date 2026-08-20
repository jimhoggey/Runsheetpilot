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
KEY_URL = "https://openrouter.ai/api/v1/key"

# A curated shortlist for THIS workload, offered only when the key is
# funded. Deliberately short: a wall of 300 models is not a choice, it is
# a research project, and the operator is trying to build a runsheet.
#
# The workload is structured extraction from ~30 lines of text against a
# fixed schema — which small INSTRUCT models do reliably and cheaply.
# Reasoning models are the wrong tool and measurably worse here: the one
# tested (gpt-oss-120b) took 230s on one run and forgot the title rule.
RECOMMENDED = [
    {
        "id": "openai/gpt-4.1-mini",
        "label": "GPT-4.1 mini",
        "why": "Built for structured extraction. Consistent run to run — "
               "the reason to pay is repeatability, not raw ability.",
        "starred": True,
    },
    {
        "id": "qwen/qwen3-30b-a3b-instruct-2507",
        "label": "Qwen3 30B Instruct",
        "why": "Cheapest of these and the only one measured on a real "
               "runsheet here: every timed row, titles normalised, twice.",
        "starred": False,
    },
    {
        "id": "anthropic/claude-haiku-4.5",
        "label": "Claude Haiku 4.5",
        "why": "Strong instruction-following. Dearer than the others, "
               "still fractions of a cent per runsheet.",
        "starred": False,
    },
    {
        "id": "openrouter/auto",
        "label": "OpenRouter Auto",
        "why": "Lets OpenRouter choose per request. Convenient, but it "
               "picks a DIFFERENT model each time — so a good parse and a "
               "bad one can't be told apart, or reproduced.",
        "starred": False,
    },
]

# Token counts for a representative parse — a runsheet plus the prompt in,
# the JSON items out. Used only to show an order-of-magnitude price, so
# nobody has to open a pricing page to answer "will this cost me anything
# real?". Measured against an actual run: Qwen billed $0.000295, this
# estimates $0.00039.
_EST_PROMPT_TOKENS = 3500
_EST_COMPLETION_TOKENS = 800

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


def next_usable_model(current: str, catalogue: dict):
    """The best usable model id that isn't `current`, or None.

    Exists for one specific moment: OpenRouter relayed a provider-side
    failure for `current` (see the parse route), so retrying the same id
    would likely land on the same broken provider. The pick is the model
    ranked just below `current` — or the top pick when `current` isn't in
    the free ranking at all (a paid or hand-typed id). During the 2026-08-03
    Darkbloom outage several free models broke at once while other
    providers' models kept working, so the neighbour is a genuine second
    chance, not a superstition.

    None means there is nothing sane to retry with — catalogue missing, or
    it offers nothing usable besides `current` itself.
    """
    ids = [m["id"] for m in usable_models(catalogue)]
    if current in ids:
        ids = ids[ids.index(current) + 1:]
    return next((i for i in ids if i != current), None)


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
                    str(configured).replace("\r", " ").replace("\n", " ")[:100])
    return pick_default_model(catalogue)


def estimate_cost(model: dict):
    """Rough cost of one parse, in dollars, or None if not priced."""
    pricing = (model or {}).get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return None
    if prompt < 0 or completion < 0:
        # Routers carry sentinel pricing (-1) because the real price is
        # whatever the model they pick charges. Computing with it yields
        # a confident, enormous, negative number.
        return None
    if prompt == 0 and completion == 0:
        return 0.0
    return (prompt * _EST_PROMPT_TOKENS
            + completion * _EST_COMPLETION_TOKENS)


def recommended_models(catalogue: dict) -> list:
    """The curated shortlist, filtered against the LIVE catalogue.

    NOTHING is offered unless OpenRouter currently lists it — no
    exceptions, not even the router. A shortlist is a hardcoded list of
    ids, and hardcoded ids are precisely how this app once shipped a
    default that 404'd on every install until a new release. An operator
    cannot diagnose "that model was withdrawn last week"; the only
    honest behaviour is for it to disappear.

    The star follows availability too: if the starred model is gone, the
    next surviving entry is starred, so there is always exactly one
    recommendation rather than a list with nothing marked.
    """
    known = {m.get("id"): m for m in (catalogue or {}).get("data") or []
             if isinstance(m, dict)}
    out = []
    for entry in RECOMMENDED:
        model = known.get(entry["id"])
        if model is None:
            log.info("Recommended model %r is not in OpenRouter's catalogue "
                     "— withdrawn or renamed; leaving it out", entry["id"])
            continue
        out.append({**entry,
                    "context_length": model.get("context_length") or 0,
                    # A router's price is whatever it dispatches to, so
                    # it has none of its own to show.
                    "cost_per_parse": (None if is_router(entry["id"])
                                       else estimate_cost(model))})
    if out and not any(r["starred"] for r in out):
        out[0] = {**out[0], "starred": True}
    return out


def fetch_key_info(api_key: str, http_get=None, timeout=8) -> dict:
    """Is this key funded? `{"funded": bool|None, "usage": float}`.

    funded=None means "couldn't tell" (offline, or the key is bad), and
    the caller treats that as free-tier — showing paid models to someone
    who can't use them produces a 402 on their first parse, which is a
    much worse failure than a shorter list.
    """
    if not (api_key or "").strip():
        return {"funded": None, "usage": 0.0}
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        resp = http_get(KEY_URL, timeout=timeout,
                        headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
    except Exception as e:
        log.info("Could not read OpenRouter key info (%s)", type(e).__name__)
        return {"funded": None, "usage": 0.0}
    free_tier = data.get("is_free_tier")
    return {"funded": (None if free_tier is None else not free_tier),
            "usage": float(data.get("usage") or 0.0)}


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
