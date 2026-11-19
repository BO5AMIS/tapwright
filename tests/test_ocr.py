"""`look` with `ocr: true` (W4): the text in the picture, refs o1…, a touch by such a ref."""
import asyncio

import pytest

from tapwright import ocr
from tapwright.control import Refused
from fakes import el
from test_daemon import finish, make, started


def run(go):
    asyncio.run(go())


@pytest.fixture
def reading(monkeypatch):
    seen = [ocr.Seen("PLAY", (150.0, 400.0, 100.0, 40.0), 0.98), ocr.Seen("Score", (20.0, 60.0, 80.0, 20.0), 0.9)]
    monkeypatch.setattr(ocr, "reader", lambda image, screen: list(seen))
    return seen


def test_look_with_ocr_lists_the_pictures_text_with_refs_and_a_touch_takes_one(reading):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "text", "Score", rect=(20, 60, 80, 20))]
        r = await d.run_tool("a", "look", {"screenshot": False, "ocr": True})
        lines = r["text"].splitlines()
        assert ocr_head() in lines
        assert lines[lines.index(ocr_head()) + 1] == 'o1 "PLAY" @150,400 100x40'
        assert not any('"Score" @20,60' in line and line.startswith("o") for line in lines)  # an element says it
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (200, 420).")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "o7"}, "look": False})
        assert e.value.code == "not_found" and "isn't in your last OCR reading" in str(e.value)
        await finish(stop, task)
    run(go)


def ocr_head():
    from tapwright.daemon import OCR_HEAD
    return OCR_HEAD


def test_a_look_at_a_screen_with_few_elements_says_ocr_can_read_it(reading):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "other", "", rect=(0, 0, 400, 800))]
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].endswith("look with ocr: true reads the text in its picture.")
        await finish(stop, task)
    run(go)


def test_without_the_extra_ocr_says_how_to_install_it(monkeypatch):
    async def go():
        monkeypatch.setattr(ocr, "reader", None)
        monkeypatch.setattr(ocr, "available", lambda: False)
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:  # (1.9.0 round 2: refused only with --no-hints, 1.8.5's way)
            await d.run_tool("a", "look", {"ocr": True}, hints=False)
        assert e.value.code == "unsupported" and 'pip install "tapwright[ocr]"' in str(e.value)
        r = await d.run_tool("a", "look", {"ocr": True})  # with hints: the look, and the note says how to install it
        assert 'pip install "tapwright[ocr]"' in r["text"]
        await finish(stop, task)
    run(go)


def test_the_status_bar_and_what_elements_say_already_are_left_out(monkeypatch):
    seen = [ocr.Seen("12:31", (42.0, 19.0, 75.0, 19.0), 0.9),
            ocr.Seen("Q Search destinations", (33.0, 157.0, 200.0, 21.0), 0.9),
            ocr.Seen("Kyoto, Japan", (49.0, 406.0, 105.0, 20.0), 0.9)]
    monkeypatch.setattr(ocr, "reader", lambda image, screen: list(seen))

    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "input", "Search destinations", "search", rect=(65, 143, 330, 51)),
                          el("e2", "button", "Kyoto", "kyoto", rect=(20, 205, 390, 236))]
        r = await d.run_tool("a", "look", {"screenshot": False, "ocr": True})
        ocr_lines = [line for line in r["text"].splitlines() if line.startswith("o")]
        assert ocr_lines == ['o1 "Kyoto, Japan" @49,406 105x20']
        await finish(stop, task)
    run(go)


def test_a_half_installed_extra_counts_as_not_installed(monkeypatch):
    """CI, 2026-11-19: rapidocr and onnxruntime were found but numpy wasn't, so `look ocr` crashed instead of answering
    with the install note."""
    import importlib.util as iu
    from tapwright import ocr
    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec", lambda name, *a: object() if name in ("rapidocr", "onnxruntime", "PIL")
                        else (None if name == "numpy" else real(name, *a)))
    assert ocr.installed() is False


def test_without_the_extra_the_look_never_reads_the_picture(monkeypatch):
    """CI, 2026-11-19: with no OCR the look added its note and then still called ocr.read (numpy missing: a crash)."""
    def boom(*a):
        raise AssertionError("ocr.read was called without the extra")

    async def go():
        monkeypatch.setattr(ocr, "reader", None)
        monkeypatch.setattr(ocr, "available", lambda: False)
        monkeypatch.setattr(ocr, "read", boom)
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"ocr": True})
        assert 'pip install "tapwright[ocr]"' in r["text"]
        await finish(stop, task)
    run(go)
