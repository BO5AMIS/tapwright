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


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """No test opens a real browser tab. One viewer test called the real webbrowser.open, and every run of the suite
    opened http://127.0.0.1:8417/?token=SECRET on the owner's screen (two dozen tabs, 2026-11-12)."""
    import webbrowser

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a real browser tab; fake webbrowser.open in it")
    monkeypatch.setattr(webbrowser, "open", refuse)
    monkeypatch.setattr(webbrowser, "open_new", refuse)
    monkeypatch.setattr(webbrowser, "open_new_tab", refuse)


@pytest.fixture(autouse=True)
def _network_marker_in_tmp(monkeypatch, tmp_path):
    """The daemon notes a `network` condition on disk, so a crashed daemon's is cleared at the next start: never the
    owner's real ~/.tapwright in a test."""
    from tapwright import store
    monkeypatch.setattr(store, "NETWORK_FILE", tmp_path / "network-marker" / "network.json")
