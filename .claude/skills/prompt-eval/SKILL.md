---
name: prompt-eval
description: Benchmark the runsheet-parsing prompt against real PDFs across OpenRouter models — timed-row recall, title format, latency, cost. Use when changing DEFAULT_PROMPT or choosing a model.
disable-model-invocation: true
---

# Evaluate the runsheet prompt

Spends real OpenRouter requests, so it is **user-invoked only**.

```bash
python3 .claude/skills/prompt-eval/bench.py                     # default models, 2 runs
python3 .claude/skills/prompt-eval/bench.py --models "openai/gpt-4.1-mini" --runs 3
python3 .claude/skills/prompt-eval/bench.py --pdf 26_Jul_2026.pdf
```

Reads the key from the app's own settings. Sample PDFs live in the repo
root and are gitignored, so run it on a machine that has them.

## What it measures, and why those two things

- **rows** — timed rows recovered ÷ timed rows actually in the PDF.
  The failure that matters: on 14 Aug 2026 a model dropped three
  pre-service rows including *Youth Arrival + Hangout*, where the
  welcome loop matches. A missing row is a hole in the service.
- **titles** — how many titles naming people use the `Activity: Names`
  colon. ProPresenter headers are read at a glance mid-service; mixed
  formats make the playlist harder to scan.
- **sec** and **cost** — a free model took 230s on one run and 9s on
  the next. Consistency is the product requirement, not peak speed.

## Reading the result

`8/8` rows means the timed-row guard in `parsing/timed_rows.py` never
had to fire. When rows are short, the guard rescues them — the playlist
is still correct, but rescued rows are typed `other` with no notes and
no songs split out, so they arrive as plain grey headers. **Recall is
the metric that decides whether a model is worth paying for.**

## Guidance

- **Always compare against a baseline.** To A/B a prompt change, get the
  old text with `git show <tag>:propresenterrunsheet/parsing/ai.py` and
  run both.
- **Two runs minimum per model** — free-tier routing varies enormously
  between calls on the same model id.
- **Instruct models beat reasoning models here.** This is structured
  extraction against a fixed schema; a reasoning model deliberating over
  a runsheet was both slower and *worse* at following the title rule.
- The same signal arrives from production continuously: the
  `rows_rescued` Aptabase event fires whenever the guard restores a row.
