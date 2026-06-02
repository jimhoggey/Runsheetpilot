"""Tests for the Service Mate paid add-on: Ed25519 licence verification and
the 14-day trial state machine.

These use an EPHEMERAL keypair generated per-test and monkeypatched into
licensing._PUBLIC_KEY_B64, so they never touch the real embedded key or the
private signing key under tools/secrets/."""

import base64
import datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from propresenterrunsheet import licensing
from propresenterrunsheet import settings as pp_settings


@pytest.fixture
def lic_env(tmp_path, monkeypatch):
    """Isolated settings.json + an ephemeral signing key wired into the
    verifier. Returns the private key so tests can mint keys."""
    monkeypatch.setattr(pp_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_B64",
                        base64.b64encode(pub_raw).decode("ascii"))
    return priv


def _key(priv, name="Test Church", product="service_mate", iat=None):
    payload = {"n": name, "p": product,
               "iat": iat or datetime.date.today().isoformat()}
    return licensing.make_license(payload, priv)


# ── Verification ────────────────────────────────────────────────────────────
def test_valid_key_is_licensed(lic_env):
    pp_settings.save_settings({"license_key": _key(lic_env, "Hillsong Brisbane")})
    st = licensing.service_mate_status()
    assert st["state"] == "licensed"
    assert st["licensed_to"] == "Hillsong Brisbane"
    assert licensing.service_mate_allowed() is True


def test_tampered_name_breaks_signature(lic_env):
    key = _key(lic_env, "Real Church")
    parts = key.split(".")
    parts[1] = parts[1][:-2] + ("AA" if not parts[1].endswith("AA") else "BB")
    assert licensing.verify_license(".".join(parts)) is None


def test_wrong_product_is_not_service_mate(lic_env):
    pp_settings.save_settings(
        {"license_key": _key(lic_env, product="some_other_feature")})
    st = licensing.service_mate_status()
    assert st["state"] != "licensed"
    assert st["key_invalid"] is True


@pytest.mark.parametrize("bad", ["", "garbage", "RP1.onlytwo", "XX9.a.b", None, 123])
def test_garbage_keys_rejected(lic_env, bad):
    assert licensing.verify_license(bad) is None


def test_key_signed_by_other_keypair_rejected(lic_env):
    other = Ed25519PrivateKey.generate()  # not the embedded public key
    assert licensing.verify_license(_key(other)) is None


# ── Trial state machine ─────────────────────────────────────────────────────
def test_trial_unstarted_then_started(lic_env):
    assert licensing.service_mate_status()["state"] == "trial_unstarted"
    assert licensing.service_mate_allowed() is True  # generous before start
    licensing.start_trial_if_needed()
    st = licensing.service_mate_status()
    assert st["state"] == "trial"
    assert st["days_left"] == licensing.TRIAL_DAYS


def test_trial_counts_down_then_expires(lic_env):
    today = datetime.date.today()
    pp_settings.save_settings(
        {"sm_trial_start": (today - datetime.timedelta(days=5)).isoformat()})
    st = licensing.service_mate_status()
    assert st["state"] == "trial"
    assert st["days_left"] == licensing.TRIAL_DAYS - 5
    assert licensing.service_mate_allowed() is True

    pp_settings.save_settings(
        {"sm_trial_start": (today - datetime.timedelta(days=15)).isoformat()})
    st = licensing.service_mate_status()
    assert st["state"] == "expired"
    assert st["days_left"] == 0
    assert licensing.service_mate_allowed() is False


def test_valid_licence_overrides_expired_trial(lic_env):
    old = (datetime.date.today() - datetime.timedelta(days=99)).isoformat()
    pp_settings.save_settings(
        {"sm_trial_start": old, "license_key": _key(lic_env, "Paid Church")})
    assert licensing.service_mate_status()["state"] == "licensed"
    assert licensing.service_mate_allowed() is True


def test_corrupt_trial_date_is_lenient(lic_env):
    pp_settings.save_settings({"sm_trial_start": "not-a-date"})
    assert licensing.service_mate_status()["state"] == "trial_unstarted"


# ── start_trial_if_needed ───────────────────────────────────────────────────
def test_start_trial_is_idempotent(lic_env):
    licensing.start_trial_if_needed()
    first = pp_settings.load_settings()["sm_trial_start"]
    assert first  # stamped
    licensing.start_trial_if_needed()
    assert pp_settings.load_settings()["sm_trial_start"] == first


def test_start_trial_skipped_when_licensed(lic_env):
    pp_settings.save_settings({"license_key": _key(lic_env)})
    licensing.start_trial_if_needed()
    assert pp_settings.load_settings().get("sm_trial_start", "") == ""


# ── Route gate ──────────────────────────────────────────────────────────────
def test_clocks_action_402_when_trial_expired(client):
    """Privileged Service Mate routes return 402 once the trial has expired
    and no licence is present. Uses the shared `client` fixture whose
    isolated_state now redirects settings.json too."""
    client.post("/api/clocks", json={"enabled": True})
    pp_settings.save_settings(
        {"sm_trial_start":
         (datetime.date.today() - datetime.timedelta(days=99)).isoformat()})
    r = client.post("/api/clocks/standby")
    assert r.status_code == 402
    assert r.get_json()["license_state"] == "expired"
