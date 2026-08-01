"""Tests for deployers.verify_signature."""
from __future__ import annotations

import hashlib
import hmac
import importlib
import sys

import pytest

MOD = "deployers"


@pytest.fixture
def deployers(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret-test")
    for m in [MOD, "config", "events", "scheduler", "main"]:
        sys.modules.pop(m, None)
    return importlib.import_module(MOD)


def make_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted(deployers):
    body = b'{"hello":"world"}'
    sig = make_sig("s3cret-test", body)
    assert deployers.verify_signature(body, sig) is True


def test_bad_signature_rejected(deployers):
    body = b'{"hello":"world"}'
    sig = "sha256=" + "0" * 64
    assert deployers.verify_signature(body, sig) is False


def test_missing_signature_rejected(deployers):
    body = b'{"hello":"world"}'
    assert deployers.verify_signature(body, None) is False


def test_signature_without_prefix_rejected(deployers):
    body = b'{"hello":"world"}'
    sig = make_sig("s3cret-test", body).removeprefix("sha256=")
    assert deployers.verify_signature(body, sig) is False


def test_no_secret_configured_rejects_all(deployers, monkeypatch):
    monkeypatch.setattr(deployers, "SECRET", b"")
    body = b'{"hello":"world"}'
    sig = make_sig("s3cret-test", body)
    assert deployers.verify_signature(body, sig) is False


def test_different_body_fails(deployers):
    sig = make_sig("s3cret-test", b"original")
    assert deployers.verify_signature(b"tampered", sig) is False
