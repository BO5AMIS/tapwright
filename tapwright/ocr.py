"""Text read from the screen's picture (`look` with `ocr: true`, and every look whose screen doesn't answer the whole
accessibility read): for screens whose elements say little, such as a game, a canvas, a video feed or a web view.
Tapwright 2.0's runner (route version 9) reads it on the phone with Apple's Vision (`of_runner` parses its answer);
otherwise RapidOCR (ONNX, on the CPU) reads it on this computer, the optional extra `pip install "tapwright[ocr]"`.
What it reads are no elements: they get refs `o1`, `o2`… that a touch turns into a touch at their centre."""
from __future__ import annotations

import importlib.util
import io
from dataclasses import dataclass
from typing import Callable

MIN_SCORE = 0.6  # RapidOCR's own confidence below which a reading is left out
INSTALL = 'OCR isn\'t installed: pip install "tapwright[ocr]" (about 300 MB), then restart tapwright serve.'


@dataclass(frozen=True)
class Seen:
    text: str
    rect: tuple[float, float, float, float]  # x, y, width, height in points
    score: float

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2

    def line(self, ref: str) -> str:
        x, y, w, h = (round(v) for v in self.rect)
        return f'{ref} "{self.text}" @{x},{y} {w}x{h}'


_engine = None  # RapidOCR, made once (it loads its models)
reader: Callable[[bytes, tuple[float, float]], list[Seen]] | None = None  # tests put a fake here


def installed() -> bool:
    """Is the optional extra here (`pip install "tapwright[ocr]"`)?"""
    # every module read() imports: a half-installed extra (rapidocr without numpy, seen on CI) is not installed
    return all(importlib.util.find_spec(m) is not None for m in ("rapidocr", "onnxruntime", "numpy", "PIL"))


def available() -> bool:
    return reader is not None or installed()


RUNNER_MIN = 0.3  # Vision's own confidence below which a reading is left out (it says 0.3 to 1.0 for real text)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{value!r} isn't a number")
    return float(value)


def of_runner(value: object, screen: tuple[float, float]) -> list[Seen]:
    """The runner's OCR answer (GET /wda/tw/ocr, route version 9: {"screen": [w, h], "lines": [{"text", "rect": [x, y,
    w, h] in points, "confidence"}]}) as Seen lines in `screen`'s points (the runner's screen may be the other way up
    in landscape), in reading order as `read` gives them. Raises ValueError for an answer that isn't one."""
    if not isinstance(value, dict) or not isinstance(value.get("lines"), list):
        raise ValueError("no lines")
    sx = sy = 1.0
    size = value.get("screen")
    if isinstance(size, list) and len(size) == 2:
        w, h = _number(size[0]), _number(size[1])
        if w > 0 and h > 0:
            sx, sy = screen[0] / w, screen[1] / h
    found = []
    for line in value["lines"]:
        if not isinstance(line, dict):
            raise ValueError("a line isn't an object")
        text, rect = line.get("text"), line.get("rect")
        if not isinstance(text, str) or not isinstance(rect, list) or len(rect) != 4:
            raise ValueError("a line has no text or no rect")
        x, y, w, h = (_number(v) for v in rect)
        score = _number(line.get("confidence", 1.0))
        if score < RUNNER_MIN or not text.strip() or w <= 0 or h <= 0:
            continue
        found.append(Seen(text.strip(), (x * sx, y * sy, w * sx, h * sy), score))
    return sorted(found, key=lambda s: (round(s.rect[1] / 8), s.rect[0]))


def read(image: bytes, screen: tuple[float, float]) -> list[Seen]:
    """The text lines in a picture of the screen, in reading order, placed in points. Slow (about 1.5 s on a
    laptop's CPU): call it off the event loop."""
    if reader is not None:
        return reader(image, screen)
    global _engine
    import numpy
    from PIL import Image
    from rapidocr import RapidOCR
    if _engine is None:
        _engine = RapidOCR()
    picture = Image.open(io.BytesIO(image)).convert("RGB")
    out = _engine(numpy.asarray(picture))
    sx, sy = screen[0] / picture.width, screen[1] / picture.height
    found = []
    for box, text, score in zip(out.boxes if out.boxes is not None else [], out.txts or (), out.scores or ()):
        if score < MIN_SCORE or not str(text).strip():
            continue
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        found.append(Seen(str(text).strip(), (min(xs) * sx, min(ys) * sy, (max(xs) - min(xs)) * sx,
                                              (max(ys) - min(ys)) * sy), float(score)))
    return sorted(found, key=lambda s: (round(s.rect[1] / 8), s.rect[0]))
