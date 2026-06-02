# Service Mate licensing — owner/seller runbook

Internal notes for **you** (the owner). Buyers never see this — their
instructions live in the main README's "Service Mate (paid add-on)" section.

Service Mate is gated by an **offline Ed25519 signed-key** scheme. The app
ships a public key (verify-only). You hold the matching private key and use
it to mint licence keys. No server, no internet, nothing to host.

---

## One-time setup

### 1. Generate your keypair

```bash
python3 tools/generate_keypair.py
```

This writes, under `tools/secrets/` (gitignored — never committed):

- `license_private_key.b64` — **SECRET.** Signs every licence you sell.
- `license_public_key.b64` — safe to share; gets embedded in the app.

> ⚠️ **Back up `license_private_key.b64` somewhere safe** (password manager,
> encrypted drive). If it leaks, anyone can mint free licences. If you lose
> it, you can't issue new keys — though keys you've already sold keep working.

The repo already has a working keypair embedded (generated during the initial
build). You only need to regenerate if you want a key that has never been on a
shared machine — if you do, paste the new public key into
`propresenterrunsheet/licensing.py` (`_PUBLIC_KEY_B64`) and ship a new build.
Regenerating **invalidates every key already sold**, so only do it before you
have real customers.

### 2. Embed the public key (only if you regenerated)

Copy the printed value into `propresenterrunsheet/licensing.py`:

```python
_PUBLIC_KEY_B64 = "…the public key…"
```

Commit that change (public key is safe to commit) and cut a new release.

---

## Fulfilling a sale (manual, for now)

When someone buys:

```bash
python3 tools/issue_license.py --name "Hillsong Brisbane"
```

It prints a key like `RP1.…`. **Paste that key into the email/receipt you
send the buyer.** They activate it in *Settings → Service Mate (paid add-on)*.

- Use the buyer's church/organisation name for `--name`. It's signed into the
  key and shown in-app as *"Licensed to …"*, so it should be recognisable to
  them and discourages key-sharing.
- The tool self-verifies every key it mints, so if it prints a key, that key
  is guaranteed to activate.

---

## Picking a store (the "buy" link)

You deferred this — the in-app buy link and the README currently point at the
GitHub repo. When you're ready, wire up one of:

| Option | Fees | Key delivery |
|---|---|---|
| **Lemon Squeezy** | ~5% + Stripe | Manual today; later: webhook → serverless signer for instant delivery |
| **Gumroad** | ~10% flat | Same — manual now, automatable later |
| **Stripe Payment Link** | ~1.75%+30c (AU) | Cheapest, but you build delivery yourself |

Whichever you pick, update two places:

1. `SERVICE_MATE_BUY_URL` in `static/app.js` (the in-app link).
2. The "Buy a licence" line in the README.

### Automating delivery later

Manual issuing is fine for the first dozens of sales. To automate: host a tiny
serverless function (Cloudflare Worker / AWS Lambda) that holds the private
key, receives the store's purchase webhook, runs the same signing logic as
`tools/issue_license.py`, and emails the key. The signing/format code lives in
`propresenterrunsheet/licensing.py` (`make_license`) so the function and the
app can never drift.

---

## How the gate works (for future-you)

- **Verification:** `propresenterrunsheet/licensing.py` — `verify_license()`
  checks the Ed25519 signature against the embedded public key. Pure offline.
- **Trial:** 14 days (`TRIAL_DAYS`) from first active use. Start date lives in
  `settings.json` as `sm_trial_start`. A user could reset it by editing that
  file — accepted trade-off for a beta paywall, not hard DRM.
- **Two chokepoints** block all privileged Service Mate behaviour when
  expired-and-unlicensed:
  - `routes/clocks.py::_check_sm_enabled` → HTTP 402 on probe/test/standby/preview.
  - `service_mate/daemon.py::_clocks_loop_tick` → daemon stops pushing to clocks.
- **Activation API:** `routes/license.py` — `GET/POST /api/license`.
- **Tests:** `tests/test_licensing.py` (uses an ephemeral keypair; never
  touches your real keys).

## Give yourself a licence

You own Service Mate — issue yourself a key so you're not on a trial clock:

```bash
python3 tools/issue_license.py --name "Fynn (owner)"
```

Paste it into your own app's Settings.
