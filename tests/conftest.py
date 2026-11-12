"""Shared test setup."""
import pytest

from tapwright import repair


@pytest.fixture(autouse=True)
def no_signing_warning(monkeypatch):
    """The expiry warning reads the real ~/.tapwright; keep it out of every answer a test checks.
    Tests of the warning itself set their own."""
    monkeypatch.setattr(repair, "expiry_warning", lambda now=None: None)


@pytest.fixture(autouse=True)
def _no_ocr_extra(monkeypatch):
    """Tests behave the same whether or not the optional OCR extra is installed here: off unless a test fakes a reader."""
    from tapwright import ocr
    monkeypatch.setattr(ocr, "installed", lambda: False)


@pytest.fixture(autouse=True)
def _runner_build_current(monkeypatch):
    """The runner build is read from the real ~/.tapwright; tests see the installed build as the wanted one unless
    they say otherwise."""
    monkeypatch.setattr(repair, "builds", lambda: {"installed_build": "current", "wanted_build": "current"})
