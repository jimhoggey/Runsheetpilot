# Release checklist

Walk through this before every release. The automated tests + CodeQL +
pip-audit + the build-and-smoke-test in `release.yml` catch a lot, but
**they can't see "the runsheet I uploaded came out wrong"** — the manual
pass below covers that. Takes ~15 minutes.

---

## 1. Bump the version

- [ ] Edit `propresenterrunsheet/config.py` → set `VERSION = "X.Y.Z"`
  - Major (X) — breaking changes for end users
  - Minor (Y) — new features, no breakage
  - Patch (Z) — bug fixes only
- [ ] Commit: `git commit -am "Release vX.Y.Z"`
- [ ] Push to main (via PR if you want a CI gate)

## 2. Local sanity

- [ ] `pytest tests/` → all green
- [ ] `python3 propresenter_app.py` launches cleanly
- [ ] Browser auto-opens at `http://localhost:5757`
- [ ] No errors in `~/Library/Application Support/Runsheet Pilot/app.log`

## 3. End-to-end smoke (with ProPresenter running)

Use a known-good runsheet PDF from a previous service.

- [ ] Sidebar populates: ProPresenter section + Template Playlist dropdown
- [ ] Sidebar footer shows `♪ N items loaded from PP` (or orange `⚠ No library`
      if PP isn't reachable — that's still correct messaging)
- [ ] **Step ⓵ — Upload**: drop the PDF → step turns green ✓; step ⓶ unlocks
- [ ] **Step ⓶ — Parse**: click Parse → spinner badge → green ✓; step ⓷ unlocks
- [ ] Results table populated with items + match indicators
- [ ] **Step ⓷ — Create**: edit service name; click Create → spinner → green ✓
- [ ] New playlist appears in ProPresenter
- [ ] Items are in runsheet order
- [ ] Template sections (Culture / MC Welcome / etc.) are expanded — not
      empty headers

## 4. Template playlist routing (skip if you don't use it)

- [ ] On Auto: a "youth" runsheet picks the Youth library; a "sunday"
      runsheet picks the Sunday library
- [ ] Selecting a specific template from the dropdown locks it (status
      line reads "Locked to …")

## 5. Service Mate (skip if you don't own a GeekMagic clock)

- [ ] On a fresh install, master switch is OFF by default
- [ ] Toggle ON → card auto-expands; daemon log entry appears
- [ ] Add a clock IP → Probe → green ✓
- [ ] Test → coloured test card lands on the clock
- [ ] Cue control Prev/Next steps through the runsheet
- [ ] Toggle OFF → card collapses; `curl http://localhost:5757/api/clocks/standby` → HTTP 409

## 6. Easter egg sanity (won't actually block release)

- [ ] Tap the version pill in the header 7× quickly → confetti + toast
- [ ] Open Settings → footer reads "Developed by **Fynn**"

## 7. Tag the release

- [ ] `git tag -a vX.Y.Z -m "vX.Y.Z"`
- [ ] `git push --tags`
- [ ] Watch the `Release` workflow in the repo's Actions tab — should
      take ~8-12 minutes for both Windows + Mac builds + smoke tests +
      Release upload
- [ ] When the workflow finishes, visit `Releases` → confirm the new
      release has BOTH the `.exe` and `.dmg` attached
- [ ] Release has the self-updater assets: `Runsheet-Pilot-mac.zip`,
      `Runsheet-Pilot-windows.exe` (if the Windows build passed), and
      `SHA256SUMS.txt`
- [ ] **Update-path check (Windows — the production machine):** with the
      PREVIOUS version installed and running, the update banner appears;
      one click downloads, restarts, and lands on the new version; the
      `.exe.old` leftover is gone after the restart

## 8. Cold smoke (the final check)

The most realistic test — on a machine that isn't yours.

- [ ] Download the `.exe` to a clean Windows VM (or `.dmg` to a clean Mac)
- [ ] First launch dismisses SmartScreen / Gatekeeper warning
- [ ] App opens, browser navigates to localhost:5757
- [ ] One real upload → parse → create cycle works end-to-end

If anything in steps 1–7 fails, **don't tag** — fix on `main` first.
If something in step 8 fails, mark the release as **draft** in the
GitHub UI and don't announce until you've fixed + re-tagged.

---

## Notes for non-obvious failure modes

| Symptom in smoke test | Probable cause |
|---|---|
| .exe opens, browser opens, but the page is blank | PyInstaller missed a `templates/` or `static/` `--add-data` flag. Check `release.yml`. |
| Parse step works but matches are all empty | OpenRouter key not set, or no library loaded. Open Settings → check both. |
| Service Mate master switch ON but clock doesn't update | Clock IP is wrong, or the clock is on a different subnet from the build machine. |
| Tag pushed but no GitHub Release created | Workflow failed — check Actions tab for the red job. Common: smoke-test timeout (bump from 30 s to 60 s if the runner is slow). |
| Release created but no files attached | `fail_on_unmatched_files: true` in `release.yml` should have failed the job — but if it didn't, check that the artifact upload paths match the download paths in the `release` job. |
