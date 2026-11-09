"""The HTTP side with a fake session: page, token, API, stream, WebSocket."""
from importlib.resources import files


def test_viewer_page_is_packaged():
    page = files("tapwright").joinpath("viewer.html")
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "<title>Tapwright live view</title>" in text and 'id="screen"' in text and "new WebSocket" in text
    assert "innerHTML" not in text  # server text only ever goes in as textContent
