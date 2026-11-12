"""Where the WebDriverAgent runner comes from: Appium's build by default, Tapwright's own with TAPWRIGHT_WDA=fork."""
import hashlib
import io
import zipfile

import pytest

from tapwright import store


def _zip_with_app() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("WebDriverAgentRunner-Runner.app/Info.plist", "x")
    return buf.getvalue()


def test_default_is_tapwrights_own_build_and_appium_is_one_variable_away(monkeypatch):
    monkeypatch.delenv("TAPWRIGHT_WDA", raising=False)
    repo, tag, sha, folder = store.wda_source()
    assert (repo, tag) == ("bo5amis/WebDriverAgent", "tw-16.15.3.3")
    assert sha == store.WDA_SHA256 and folder == store.WDA_VERSION
    monkeypatch.setenv("TAPWRIGHT_WDA", "appium")
    assert store.wda_source()[:2] == ("appium/WebDriverAgent", "v16.15.3")


def test_fork_selected_by_env(monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_WDA", "fork")
    repo, tag, _, folder = store.wda_source()
    assert repo == "bo5amis/WebDriverAgent" and tag.startswith("tw-")
    assert folder != store.WDA_SOURCES["appium"][3]  # cached apart from Appium's


def test_unknown_source_refused(monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_WDA", "nope")
    with pytest.raises(RuntimeError, match="TAPWRIGHT_WDA"):
        store.wda_source()


def test_download_checked_and_cached_per_source(monkeypatch, tmp_path):
    data = _zip_with_app()
    monkeypatch.setattr(store, "HOME", tmp_path)
    monkeypatch.setitem(store.WDA_SOURCES, "fork", ("o/r", "tw-1", hashlib.sha256(data).hexdigest(), "fork-x"))
    calls = []
    monkeypatch.setattr(store, "_release_file", lambda repo, tag, name: calls.append((repo, tag, name)) or data)
    monkeypatch.setenv("TAPWRIGHT_WDA", "fork")
    app = store.wda_app()
    assert app == tmp_path / "wda" / "fork-x" / "WebDriverAgentRunner-Runner.app" and app.exists()
    assert store.wda_app() == app and calls == [("o/r", "tw-1", "WebDriverAgentRunner-Runner.zip")]


def test_bad_hash_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HOME", tmp_path)
    monkeypatch.setattr(store, "_release_file", lambda *a: _zip_with_app())
    monkeypatch.delenv("TAPWRIGHT_WDA", raising=False)
    with pytest.raises(RuntimeError, match="SHA-256"):
        store.wda_app()
