"""Every URL the front end calls must exist as a Flask route.

The front end is 2,259 lines of JavaScript that reaches the backend through
hand-written URL strings. Nothing connected the two: rename a route and the
suite stays green, because no Python test imports app.js and no browser runs
during CI. The failure surfaces as a 404 in front of an operator.

This is the cheap half of that problem — it proves the paths line up. It
cannot prove the payloads do.
"""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
ROUTES_DIR = ROOT / "propresenterrunsheet" / "routes"

# fetch('/api/x')  fetch("/api/x")  fetch(`/api/x/${id}/y`)
_CALL = re.compile(r"""fetch\(\s*['"`](/api/[^'"`]*)['"`]""")
_ROUTE = re.compile(r"""@bp\.route\(\s*['"](/api/[^'"]*)['"]""")
_JS_INTERP = re.compile(r"\$\{[^}]*\}")
_FLASK_PARAM = re.compile(r"<[^>]+>")


def _norm(path: str) -> str:
    """Reduce both notations to a comparable shape.

    `/api/clocks/${id}/probe` and `/api/clocks/<clock_id>/probe` both become
    `/api/clocks/*/probe`. Query strings are dropped — routes never carry one.
    """
    path = path.split("?")[0].rstrip("/")
    path = _JS_INTERP.sub("*", path)
    path = _FLASK_PARAM.sub("*", path)
    return path


def _frontend_calls() -> set:
    if not APP_JS.exists():
        pytest.skip("static/app.js not present")
    js = APP_JS.read_text(encoding="utf-8")
    return {_norm(m) for m in _CALL.findall(js)}


def _flask_routes() -> set:
    out = set()
    for py in ROUTES_DIR.glob("*.py"):
        out |= {_norm(m)
                for m in _ROUTE.findall(py.read_text(encoding="utf-8"))}
    return out


def test_every_frontend_call_hits_a_real_route():
    calls, routes = _frontend_calls(), _flask_routes()
    assert calls, "parsed no fetch() calls out of app.js — the regex broke"
    assert routes, "parsed no @bp.route decorators — the regex broke"

    missing = sorted(calls - routes)
    assert not missing, (
        "app.js calls URLs with no matching Flask route:\n  "
        + "\n  ".join(missing)
        + "\n\nEither the route was renamed without updating the front end, "
          "or the call is a typo. Both are a 404 in front of an operator.")


def test_the_parser_still_recognises_both_notations():
    """A guard on the guard: if either regex silently stops matching, the
    test above passes vacuously and protects nothing."""
    assert _norm("/api/clocks/${c.id}/probe") == "/api/clocks/*/probe"
    assert _norm("/api/clocks/<clock_id>/probe") == "/api/clocks/*/probe"
    assert _norm("/api/settings/") == "/api/settings"
    assert len(_frontend_calls()) >= 15, "suspiciously few fetch() calls found"
    assert len(_flask_routes()) >= 15, "suspiciously few routes found"
