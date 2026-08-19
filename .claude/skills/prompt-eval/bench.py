#!/usr/bin/env python3
"""Benchmark DEFAULT_PROMPT against real runsheets, across models.

Answers the only questions that matter for this app's AI phase:
does the model return EVERY timed row, does it normalise titles to
"Activity: Names", how long does it take, and what does it cost.

Usage:
    python3 .claude/skills/prompt-eval/bench.py [--models a,b] [--runs 2]
                                                [--pdf 14_Aug_2026.pdf]

Reads the OpenRouter key from the app's own settings. Runs are
sequential and slow on purpose — free tiers rate-limit hard.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from propresenterrunsheet.parsing.ai import (            # noqa: E402
    DEFAULT_PROMPT, assemble_prompt, parse_ai_response)
from propresenterrunsheet.parsing.pdf import extract_pdf_text   # noqa: E402
from propresenterrunsheet.parsing.timed_rows import (    # noqa: E402
    extract_timed_rows)
from propresenterrunsheet.settings import load_settings  # noqa: E402

DEFAULT_MODELS = [
    "qwen/qwen3-30b-a3b-instruct-2507",
    "openai/gpt-4.1-mini",
    "anthropic/claude-haiku-4.5",
    "google/gemini-2.5-flash",
]


def score(items, expected_rows):
    """(timed-row recall, titles normalised to 'Activity: Names')."""
    titles = [(i.get("title") or "") for i in items if isinstance(i, dict)]
    folded = [t.casefold() for t in titles]
    hit = sum(1 for r in expected_rows
              if any(r.casefold()[:12] in g or g[:12] in r.casefold()
                     for g in folded))
    # A title naming people should carry a colon before them.
    named = [t for t in titles if " & " in t or " and " in t.lower()]
    normalised = sum(1 for t in named if ":" in t)
    return hit, len(expected_rows), normalised, len(named)


def run_once(model, prompt, key):
    import requests
    t0 = time.time()
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1,
                  "response_format": {"type": "json_object"},
                  "usage": {"include": True}},
            timeout=120)
    except Exception as e:
        return None, f"{type(e).__name__}", time.time() - t0, None
    dt = time.time() - t0
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}", dt, None
    body = r.json()
    cost = (body.get("usage") or {}).get("cost")
    content = body["choices"][0]["message"].get("content") or ""
    try:
        items, _name = parse_ai_response(content)
    except Exception:
        return None, "unparseable", dt, cost
    return items, "", dt, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--pdf", default="14_Aug_2026.pdf")
    args = ap.parse_args()

    pdf = REPO / args.pdf
    if not pdf.exists():
        sys.exit(f"no such runsheet: {pdf}  (sample PDFs are gitignored)")
    key = (load_settings().get("or_key") or "").strip()
    if not key:
        sys.exit("no OpenRouter key in settings")

    raw = extract_pdf_text(str(pdf))
    expected = [r["title"] for r in extract_timed_rows(raw)]
    prompt = assemble_prompt(DEFAULT_PROMPT, raw[:7000], library_names=[])
    print(f"{pdf.name}: {len(expected)} timed rows, {args.runs} run(s) each\n")
    print(f"{'model':46} {'rows':>7} {'titles':>7} {'sec':>6} {'cost':>10}")
    print("-" * 80)

    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        rows, titles, secs, costs, errs = [], [], [], [], []
        for _ in range(args.runs):
            items, err, dt, cost = run_once(model, prompt, key)
            secs.append(dt)
            if err:
                errs.append(err)
                continue
            hit, total, norm, named = score(items, expected)
            rows.append(f"{hit}/{total}")
            titles.append(f"{norm}/{named}" if named else "-")
            if cost is not None:
                costs.append(cost)
            time.sleep(1)
        if errs and not rows:
            print(f"{model:46} {errs[0]:>7}")
            continue
        cost_s = f"${statistics.mean(costs):.5f}" if costs else "?"
        print(f"{model:46} {','.join(rows):>7} {','.join(titles):>7} "
              f"{statistics.mean(secs):>6.1f} {cost_s:>10}"
              + (f"   [{len(errs)} err]" if errs else ""))

    print("\nrows   = timed rows recovered / present in the PDF (higher is better)")
    print("titles = titles with names that use the 'Activity: Names' colon")


if __name__ == "__main__":
    main()
