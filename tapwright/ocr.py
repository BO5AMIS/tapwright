"""Text read from the screen's picture (`look` with `ocr: true`): for screens whose elements say little, such as a
game, a canvas or a web view. It runs RapidOCR (ONNX, on the CPU) on this computer, so nothing is added to the
phone; it is the optional extra `pip install "tapwright[ocr]"`. What it reads are no elements: they get refs
`o1`, `o2`… that a touch turns into a touch at their centre."""
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


def available() -> bool:
    return reader is not None or (importlib.util.find_spec("rapidocr") is not None
                                  and importlib.util.find_spec("onnxruntime") is not None)


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
