#!/usr/bin/env python3
"""Generate the Ed25519 keypair used to sign Service Mate licence keys.

RUN THIS ONCE. It creates a private signing key (kept secret, on your
machine only) and a public verification key (embedded in the app).

  python3 tools/generate_keypair.py

Outputs (under tools/secrets/, which is gitignored):
  license_private_key.b64   ← SECRET. Back this up somewhere safe. If it
                              leaks, anyone can mint free licences. If you
                              lose it, you can't issue new keys (existing
                              ones keep working).
  license_public_key.b64    ← Safe to share. Paste its contents into
                              propresenterrunsheet/licensing.py as
                              _PUBLIC_KEY_B64.

The script refuses to overwrite an existing private key — regenerating
would invalidate every licence you've already sold. Delete the file by
hand if you really mean to start over.
"""

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SECRETS_DIR = Path(__file__).resolve().parent / "secrets"
PRIVATE_PATH = SECRETS_DIR / "license_private_key.b64"
PUBLIC_PATH = SECRETS_DIR / "license_public_key.b64"


def _raw_b64(key_bytes: bytes) -> str:
    return base64.b64encode(key_bytes).decode("ascii")


def main() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_PATH.exists():
        print(f"Refusing to overwrite existing private key:\n  {PRIVATE_PATH}\n"
              "Regenerating would invalidate every licence you've issued.\n"
              "Delete the file by hand first if you really want a new keypair.",
              file=sys.stderr)
        sys.exit(1)

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    priv_b64 = _raw_b64(priv_raw)
    pub_b64 = _raw_b64(pub_raw)

    PRIVATE_PATH.write_text(priv_b64 + "\n")
    PRIVATE_PATH.chmod(0o600)  # owner read/write only
    PUBLIC_PATH.write_text(pub_b64 + "\n")

    print("Keypair generated.\n")
    print(f"  Private key (SECRET) → {PRIVATE_PATH}")
    print(f"  Public key           → {PUBLIC_PATH}\n")
    print("Next: paste this public key into propresenterrunsheet/licensing.py")
    print("as the value of _PUBLIC_KEY_B64:\n")
    print(f'    _PUBLIC_KEY_B64 = "{pub_b64}"\n')
    print("Then issue licences with:")
    print('    python3 tools/issue_license.py --name "Church Name"')


if __name__ == "__main__":
    main()
