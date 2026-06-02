#!/usr/bin/env python3
"""Issue a Service Mate licence key for a buyer.

This is the seller-side fulfilment tool. When someone buys, run:

    python3 tools/issue_license.py --name "Hillsong Brisbane"

It prints a licence key (RP1.…) — paste it into the email/receipt you
send the buyer. They paste it into Settings → Licence in the app.

Requires the private key from generate_keypair.py at
tools/secrets/license_private_key.b64 (override with --private-key).
That file is gitignored and must never be committed or shared.

Signing + key format live in propresenterrunsheet/licensing.py so the
issuer and the app's verifier can never drift apart.
"""

import argparse
import base64
import datetime
import sys
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from propresenterrunsheet.licensing import _PRODUCT, make_license, verify_license

DEFAULT_PRIVATE_PATH = Path(__file__).resolve().parent / "secrets" / "license_private_key.b64"


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        sys.exit(f"Private key not found at {path}\n"
                 "Run `python3 tools/generate_keypair.py` first.")
    raw = base64.b64decode(path.read_text().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def main() -> None:
    ap = argparse.ArgumentParser(description="Issue a Service Mate licence key.")
    ap.add_argument("--name", required=True,
                    help='Buyer name / church, e.g. "Hillsong Brisbane". '
                         "Shown in-app as 'Licensed to …' and signed into the key.")
    ap.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_PATH,
                    help="Path to the base64 private key (default: tools/secrets/).")
    args = ap.parse_args()

    priv = _load_private_key(args.private_key)
    payload = {
        "n": args.name.strip(),
        "p": _PRODUCT,
        "iat": datetime.date.today().isoformat(),
    }
    key = make_license(payload, priv)

    # Self-check: the app's own verifier must accept what we just minted.
    if not verify_license(key):
        sys.exit("ERROR: generated key failed self-verification. Does the "
                 "public key in licensing.py match this private key?")

    print(f"\nLicence for: {payload['n']}")
    print(f"Issued:      {payload['iat']}")
    print("\nLicence key (send this to the buyer):\n")
    print(f"  {key}\n")


if __name__ == "__main__":
    main()
