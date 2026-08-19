---
name: release
description: Ship a new version of Runsheet Pilot — bump, PR, merge, tag, verify assets, write release notes. Use when the user asks to release, ship, or tag a version.
disable-model-invocation: true
---

# Release Runsheet Pilot

Ships a version end to end. **User-invoked only** — this publishes
artefacts the world can download, so it never runs on the model's
initiative.

`RELEASE_CHECKLIST.md` in the repo root is the human-facing companion
(the ~15-minute manual pass). This skill is the mechanical half.

## The two mistakes this skill exists to prevent

1. **Identifying the release run by recency.** `gh run list --workflow
   Release --limit 1` can return a run from a *previous* tag if the new
   tag's run hasn't registered yet. That happened: the watcher returned
   exit 0 for the wrong run and a failed build was reported as shipped.
   **Always match on `headBranch == the tag`.**
2. **Claiming success without looking.** A green workflow conclusion is
   not proof. **List the release assets and see four names** before
   telling the user it shipped.

## Steps

### 1. Version + notes

- `propresenterrunsheet/config.py` → bump `VERSION`.
  Major = breaking for users · Minor = features · Patch = fixes only.
- Rewrite `WHATS_NEW` in the same file. **Max three bullets**
  (`tests/test_whats_new.py` enforces the cap and fails the build
  otherwise). Operator-facing, in the app's voice — read the idle
  greetings in `static/app.js` for the register. These same three lines
  go in the GitHub release body, and the app shows them in the
  what's-new popup on first launch after updating.

### 2. Branch, test, PR

```bash
git checkout -b <type>/<short-name> origin/main
# ... changes ...
pytest -q                      # must be green before pushing
git push -u origin HEAD
gh pr create --title "vX.Y.Z — <what changed>" --body "..."
```

The PR body should explain *why*, not just what — the repo's commit and
PR history is the design record.

### 3. Wait for CI — all of it

```bash
gh pr checks <PR#> --watch
```

Required: pytest on 3.11/3.12/3.13, CodeQL, Analyse (python), Analyse
(javascript). **CodeQL failures are real** — this repo has shipped
genuine ReDoS findings; don't wave them through.

Note the PR runners are **Linux**. The Windows build gate runs only at
release time, and it is the one that catches locale-codec bugs.

### 4. Merge and tag

```bash
gh pr merge <PR#> --squash
git fetch origin
SHA=$(git rev-parse origin/main)
git tag vX.Y.Z "$SHA"
git push origin vX.Y.Z
```

Squash-merge is the house style. Because it rewrites history into one
new commit, **never reuse a feature branch after merging it** — cut a
fresh branch from `origin/main` instead, or the next PR will look like
it re-adds everything.

Re-tagging is safe *only* if the previous tag published no assets:

```bash
git push --delete origin vX.Y.Z && git tag -d vX.Y.Z   # then re-tag
```

### 5. Find the RIGHT release run, then watch it

```bash
gh run list --workflow Release --limit 5 \
  --json databaseId,headBranch,status,conclusion \
  --jq '.[] | select(.headBranch == "vX.Y.Z") | .databaseId'
```

Watch that id. Three jobs must all succeed: **Build macOS .dmg**,
**Build Windows .exe**, **Publish GitHub Release**. If Publish is
`skipped`, a build failed — read `gh run view <id> --log-failed`.

### 6. Verify the assets exist

```bash
gh release view vX.Y.Z --json assets --jq '[.assets[].name]'
```

Expect exactly four: `Runsheet-Pilot-mac.dmg`, `Runsheet-Pilot-mac.zip`,
`Runsheet-Pilot-windows.exe`, `SHA256SUMS.txt`. Anything less is not a
release.

### 7. Release notes

Title is the bare version (`v2.9.0`). Body opens with a greeting in the
app's voice, then the same three `WHATS_NEW` bullets prefixed with ✨,
then a line noting they also appear in-app on first launch.

### 8. Report honestly

Say what was verified, not what was expected. If a step failed, say so
plainly and name the cause.
