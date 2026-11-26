"""Reviewer N: in landscape (turn 90) this computer's OCR reads the newest WebDriverAgent stream frame as captured
(portrait) and scales its boxes by the landscape screen: the o refs (and a {text} touch by picture) are placed
elsewhere. Every other user of a stream frame turns it first (_upright)."""
import io

from PIL import Image

from test_daemon import finish, make, started
from test_vision_200 import feed_session, run

from tapwright import ocr


def portrait_jpeg(w=430, h=932) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG")
    return buf.getvalue()


def test_local_ocr_in_landscape_reads_a_picture_the_way_up_the_screen_stands(monkeypatch):
    seen_sizes = []

    def reader(image, screen):
        picture = Image.open(io.BytesIO(image))
        seen_sizes.append((picture.width, picture.height, screen))
        return []

    monkeypatch.setattr(ocr, "reader", reader)
    monkeypatch.setattr(ocr, "available", lambda: True)

    async def go():
        s = feed_session(text=None)  # the runner can't read text (older than route version 9, or its OCR failed)
        d = make(s)
        stop, task = await started(d)
        s.screen, s.turn = (932.0, 430.0), 90  # landscape, side known
        d.hub.publish(portrait_jpeg())  # a WebDriverAgent frame: captured portrait
        await d._screen_text("accurate", implicit=True)
        w, h, screen = seen_sizes[-1]
        assert (w > h) == (screen[0] > screen[1]), f"a {w}x{h} picture placed on a {screen} screen"
        await finish(stop, task)
    run(go)
