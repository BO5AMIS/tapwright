"""Shared test setup."""
import pytest

from tapwright import repair


@pytest.fixture(autouse=True)
def no_signing_warning(monkeypatch):
    """The expiry warning reads the real ~/.tapwright; keep it out of every answer a test checks.
    Tests of the warning itself set their own."""
    monkeypatch.setattr(repair, "expiry_warning", lambda now=None: None)
