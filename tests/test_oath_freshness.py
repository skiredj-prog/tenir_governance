"""Oath freshness for the hardened KeyCeremony (OATH-01).

A valid HMAC and an unused nonce are not enough. The signed timestamp has
to be an aware UTC instant inside the configured age and future-skew window.
The nonce is stored only after both checks succeed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

R5_ROOT = Path(__file__).resolve().parents[1] / "r5_hardened" / "IRON_OMEGA_R5"
sys.path.insert(0, str(R5_ROOT))
try:
    from r5_distributed_crypto.merkle.distributed_ledger import (
        KeyCeremony,
        parse_oath_freshness_limits,
    )
finally:
    sys.path.remove(str(R5_ROOT))

SECRET = "oath-freshness-test-secret"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
OPERATOR = "operator-1"


def _ceremony(**kwargs) -> KeyCeremony:
    kwargs.setdefault("max_age_seconds", KeyCeremony.DEFAULT_MAX_AGE_SECONDS)
    kwargs.setdefault("future_skew_seconds", KeyCeremony.DEFAULT_FUTURE_SKEW_SECONDS)
    kwargs.setdefault("now", lambda: NOW)
    return KeyCeremony(SECRET, **kwargs)


def _signed(ceremony: KeyCeremony, when: datetime, nonce: str = "nonce-1", epoch: int = 0) -> dict:
    timestamp = when.astimezone(timezone.utc).isoformat()
    payload = f"{ceremony.OATH_TEXT}|{epoch}|{OPERATOR}|{nonce}|{timestamp}"
    signature = hmac.new(
        ceremony._epoch_key(epoch),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "oath_text": ceremony.OATH_TEXT,
        "signature": signature,
        "epoch_id": epoch,
        "operator_id": OPERATOR,
        "nonce": nonce,
        "timestamp": timestamp,
    }


def _verify(ceremony: KeyCeremony, signed: dict) -> bool:
    return ceremony.verify_oath(**signed)


def test_defaults_are_five_minutes_and_thirty_seconds(monkeypatch):
    monkeypatch.delenv("TENIR_OATH_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("TENIR_OATH_FUTURE_SKEW_SECONDS", raising=False)
    assert parse_oath_freshness_limits() == (300.0, 30.0)
    ceremony = KeyCeremony(SECRET)
    assert ceremony.max_age_seconds == 300.0
    assert ceremony.future_skew_seconds == 30.0


def test_within_window_is_accepted():
    ceremony = _ceremony()
    signed = _signed(ceremony, NOW - timedelta(seconds=10))
    assert _verify(ceremony, signed) is True


def test_exact_max_age_is_accepted_and_one_microsecond_past_is_rejected():
    ceremony = _ceremony(max_age_seconds=300, future_skew_seconds=30)
    on_edge = _signed(ceremony, NOW - timedelta(seconds=300), nonce="edge-old")
    assert _verify(ceremony, on_edge) is True
    past = _signed(
        ceremony,
        NOW - timedelta(seconds=300, microseconds=1),
        nonce="past-old",
    )
    other = _ceremony(max_age_seconds=300, future_skew_seconds=30)
    assert _verify(other, past) is False
    assert "past-old" not in other._used_nonces


def test_exact_future_skew_is_accepted_and_beyond_is_rejected():
    ceremony = _ceremony(max_age_seconds=300, future_skew_seconds=30)
    on_edge = _signed(ceremony, NOW + timedelta(seconds=30), nonce="edge-future")
    assert _verify(ceremony, on_edge) is True
    beyond = _signed(
        ceremony,
        NOW + timedelta(seconds=30, microseconds=1),
        nonce="beyond-future",
    )
    other = _ceremony(max_age_seconds=300, future_skew_seconds=30)
    assert _verify(other, beyond) is False
    assert "beyond-future" not in other._used_nonces


def test_expired_timestamp_is_rejected_without_consuming_the_nonce():
    ceremony = _ceremony(max_age_seconds=300, future_skew_seconds=30)
    signed = _signed(ceremony, NOW - timedelta(seconds=301), nonce="stale")
    assert _verify(ceremony, signed) is False
    assert "stale" not in ceremony._used_nonces
    fresh = _signed(ceremony, NOW - timedelta(seconds=1), nonce="stale")
    assert _verify(ceremony, fresh) is True


def test_replay_of_an_accepted_oath_is_rejected():
    ceremony = _ceremony()
    signed = _signed(ceremony, NOW - timedelta(seconds=5), nonce="once")
    assert _verify(ceremony, signed) is True
    assert _verify(ceremony, signed) is False
    assert list(ceremony._used_nonces).count("once") == 1


@pytest.mark.parametrize(
    "timestamp",
    ["", "   ", "not-a-timestamp", "2026-11-25T12:00:00", "2026-11-25"],
)
def test_malformed_or_naive_timestamp_is_rejected_without_consuming_nonce(timestamp):
    ceremony = _ceremony()
    payload = f"{ceremony.OATH_TEXT}|0|{OPERATOR}|nonce-bad|{timestamp}"
    signature = hmac.new(
        ceremony._epoch_key(0),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert ceremony.verify_oath(
        ceremony.OATH_TEXT, signature, 0, OPERATOR, "nonce-bad", timestamp
    ) is False
    assert "nonce-bad" not in ceremony._used_nonces


def test_zulu_suffix_is_accepted_as_utc():
    ceremony = _ceremony()
    signed = _signed(ceremony, NOW - timedelta(seconds=1))
    signed["timestamp"] = "2026-11-25T11:59:59Z"
    payload = (
        f"{ceremony.OATH_TEXT}|0|{OPERATOR}|{signed['nonce']}|{signed['timestamp']}"
    )
    signed["signature"] = hmac.new(
        ceremony._epoch_key(0),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert _verify(ceremony, signed) is True


@pytest.mark.parametrize(
    "max_age, skew",
    [("0", "30"), ("-5", "30"), ("nan", "30"), ("inf", "30"), ("300", "-1"), ("300", "nan")],
)
def test_unsafe_limits_are_rejected(max_age, skew):
    with pytest.raises(ValueError):
        parse_oath_freshness_limits(max_age, skew)


def test_blank_limits_use_the_defaults():
    assert parse_oath_freshness_limits("  ", "") == (300.0, 30.0)


def test_environment_limits_are_applied(monkeypatch):
    monkeypatch.setenv("TENIR_OATH_MAX_AGE_SECONDS", "120")
    monkeypatch.setenv("TENIR_OATH_FUTURE_SKEW_SECONDS", "5")
    ceremony = KeyCeremony(SECRET, now=lambda: NOW)
    assert (ceremony.max_age_seconds, ceremony.future_skew_seconds) == (120.0, 5.0)
    assert _verify(ceremony, _signed(ceremony, NOW - timedelta(seconds=120))) is True
    other = KeyCeremony(SECRET, now=lambda: NOW)
    assert _verify(other, _signed(other, NOW - timedelta(seconds=121), nonce="too-old")) is False
    assert os.environ["TENIR_OATH_MAX_AGE_SECONDS"] == "120"
