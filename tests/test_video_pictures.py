"""The video hub's decoded pictures as stream pictures (needs PyAV)."""
import io
import time

import pytest
from PIL import Image

from tapwright.stream import PictureFrame, alike

av = pytest.importorskip("av")

from tapwright import video  # noqa: E402


def decoded(w: int = 360, h: int = 640, square: bool = False, fmt: str = "yuv420p"):
    im = Image.new("RGB", (w, h), (235, 235, 240))
    if square:
        im.paste((10, 10, 10), (w // 2, h // 2, w // 2 + 40, h // 2 + 40))
    return av.VideoFrame.from_image(im).reformat(format=fmt)


def test_a_decoded_frame_gives_a_thumb_and_a_jpeg_of_its_own_size():
    for w, h in ((360, 640), (362, 642)):
        pic = video.picture_of(decoded(w, h))
        assert pic.thumb().size == (-(-w // 8), -(-h // 8)) and pic.thumb().mode == "L"
        assert not pic.encoded
        assert Image.open(io.BytesIO(pic.jpeg())).size == (w, h) and pic.encoded


def test_pictures_compare_by_what_is_drawn_for_any_pixel_format():
    for fmt in ("yuv420p", "yuvj420p", "yuv444p", "rgb24", "nv12"):
        blank = PictureFrame(1, 0, video.picture_of(decoded(fmt=fmt)))
        assert alike(blank, PictureFrame(2, 0, video.picture_of(decoded(fmt=fmt)))), fmt
        assert not alike(blank, PictureFrame(3, 0, video.picture_of(decoded(square=True, fmt=fmt)))), fmt


def test_the_hub_hands_out_the_newest_picture_once_per_decoded_frame():
    hub = video.VideoHub()
    try:
        assert hub.newest() is None
        first, second = decoded(), decoded(square=True)
        hub._last, hub._last_frame_at = first, time.monotonic() - 1.0
        pic, at = hub.newest()
        again, at_again = hub.newest()
        assert pic is again and at == at_again == hub._last_frame_at and not pic.encoded
        hub._last, hub._last_frame_at = second, time.monotonic()
        newer, newer_at = hub.newest()
        assert newer is not pic and newer_at > at
    finally:
        hub.close()
