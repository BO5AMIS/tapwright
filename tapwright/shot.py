"""Screenshots for agents: resize to the width asked for, optionally draw each actionable
element's box and ref (AGENT-TOOLS section 3.5). Pure functions: no phone, no network."""
import io

from PIL import Image, ImageDraw, ImageFont

JPEG_QUALITY = 75
MIN_WIDTH, MAX_WIDTH, DEFAULT_WIDTH = 160, 1320, 600
RED, WHITE = (255, 0, 0), (255, 255, 255)
ORIENTATION_TOLERANCE = 0.02  # how far the picture's aspect ratio may differ from the screen's
LINE = 3  # box edge, in output pixels


def render(source: bytes, screen: tuple[float, float], width: int = DEFAULT_WIDTH,
           boxes: list | None = None) -> tuple[bytes, tuple[int, int], bool]:
    """A JPEG `width` pixels wide from a JPEG or PNG, its (width, height), and whether the boxes were
    skipped because the picture's shape isn't the screen's (the phone turned since the list was read).
    `boxes` are (ref, x, y, w, h) in points; `screen` is the phone's size in points, which
    turns points into pixels. Each box is outlined, with its ref in a chip above its top-left
    corner (inside it when there is no room above)."""
    img = Image.open(io.BytesIO(source))
    skipped = bool(boxes) and abs(img.width / img.height - screen[0] / screen[1]) > ORIENTATION_TOLERANCE
    img.draft("RGB", (width, max(1, width * img.height // img.width)))  # a JPEG decodes at reduced scale; PNG ignores it
    img = img.convert("RGB")
    if img.width != width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    if boxes and not skipped:
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(12)
        scale = img.width / screen[0]
        for ref, x, y, w, h in boxes:
            x0, y0 = round(x * scale), round(y * scale)
            x1, y1 = round((x + w) * scale) - 1, round((y + h) * scale) - 1
            draw.rectangle([x0, y0, x1, y1], outline=RED, width=LINE)
            left, top, right, bottom = draw.textbbox((0, 0), ref, font=font)
            chip_w, chip_h = right - left + 6, bottom - top + 4
            chip_y = y0 - chip_h if y0 - chip_h >= 0 else y0
            draw.rectangle([x0, chip_y, x0 + chip_w, chip_y + chip_h], fill=RED)
            draw.text((x0 + 3 - left, chip_y + 2 - top), ref, fill=WHITE, font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), img.size, skipped
