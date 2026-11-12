"""The `filmstrip` tool's picture (AGENT-TOOLS §14.4): several frames in one numbered image, laid out left to right
and top to bottom, three to a row. Pure functions: no phone, no network."""
import io

from PIL import Image, ImageDraw, ImageFont

FRAMES = (2, 12, 6)  # lowest, highest, default
INTERVAL_MS = (100, 2000, 250)
FRAME_WIDTH = (80, 400, 200)
COLUMNS = 3
GAP = 8  # pixels between frames and around the strip
JPEG_QUALITY = 80
LARGE_FRAME = 160  # a frame this wide or wider gets the larger number
LABEL_BOX, WHITE = (20, 20, 20), (255, 255, 255)


def compose(frames: list[bytes], frame_width: int) -> tuple[bytes, tuple[int, int]]:
    """A JPEG of the frames (each a JPEG or PNG), `frame_width` pixels wide each, numbered from 1, and its (width,
    height). A frame whose shape differs (the phone turned meanwhile) keeps its own height."""
    if not frames:
        raise ValueError("there are no frames to put in a filmstrip")
    pictures = []
    for data in frames:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (frame_width, max(1, frame_width * img.height // img.width)))
        img = img.convert("RGB")
        pictures.append(img.resize((frame_width, max(1, round(img.height * frame_width / img.width))), Image.LANCZOS))
    columns = min(COLUMNS, len(pictures))
    rows = [pictures[i:i + columns] for i in range(0, len(pictures), columns)]
    width = columns * frame_width + (columns + 1) * GAP
    height = sum(max(p.height for p in row) for row in rows) + (len(rows) + 1) * GAP
    strip = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(strip)
    font = ImageFont.load_default(22 if frame_width >= LARGE_FRAME else 14)
    top, number = GAP, 1
    for row in rows:
        for c, picture in enumerate(row):
            left = GAP + c * (frame_width + GAP)
            strip.paste(picture, (left, top))
            text = str(number)
            x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
            box_w, box_h = x1 - x0 + 8, y1 - y0 + 8
            draw.rectangle([left, top, left + box_w, top + box_h], fill=LABEL_BOX)
            draw.text((left + 4 - x0, top + 4 - y0), text, fill=WHITE, font=font)
            number += 1
        top += max(p.height for p in row) + GAP
    buf = io.BytesIO()
    strip.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), strip.size
