"""Visual regression (docs/AGENT-TOOLS.md §14, v1.9): the screen against a reference picture saved earlier.

This module is the pixel work and the files, with no phone and no network: the diff, the picture that shows it, the
words for a region, the checks of the parameters, and where references live. The daemon takes the screenshot and
the element list and asks the questions (Daemon._compare).

Pillow only (no numpy), and each step is one C call over the whole picture, so a 600x1304 screen takes a few
milliseconds: the largest channel of the difference, a cut at 0.1 of 255 (Tapline's tolerance, which is what keeps
JPEG noise from counting as a change), a histogram for the count, and a grid of 16 px cells that the Python code joins."""
from __future__ import annotations

import contextlib
import errno
import io
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, NamedTuple

from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError

from . import ui
from .control import Refused

NAME = re.compile(r"^[A-Za-z0-9][\w.-]{0,60}$")
DEFAULT_THRESHOLD = 0.5  # percent of the screen that may change and still match
TOLERANCE = 0.1  # of 255: how far the largest channel of a pixel must move to count as changed
CUT = int(TOLERANCE * 255) + 1  # 26: the first channel difference that counts (> 25.5)
CELL = 16  # px: changed pixels are joined by the cell they fall in, and cells by their 8 neighbours
MAX_LINES = 8  # region lines in an answer
PAINT = (128, 128, 128)  # what an ignored rect and the status bar are painted with, on both pictures
FADE, TINT, OUTLINE = 0.6, 0.55, 2  # the picture: towards white; red over a change; a region's outline in px
RED, WHITE = (255, 0, 0), (255, 255, 255)
JPEG_QUALITY = 80
MAX_PIXELS = 4000 * 4000  # a reference larger than this is no screen (the largest is 1320x2868 at full width)
MAX_POINTS = 100_000  # an ignored rect's numbers, in points, beyond any screen
_TABLE = [0] * CUT + [255] * (256 - CUT)


class Region(NamedTuple):
    x: int
    y: int
    width: int
    height: int


class Result:
    """What a comparison found. `regions` are in pixels, the largest first; `picture` draws them."""

    def __init__(self, after: Image.Image, mask: Image.Image, changed: int, regions: list[Region]) -> None:
        self.after, self.mask, self.changed, self.regions = after, mask, changed, regions
        self.total = after.width * after.height

    @property
    def percent(self) -> float:
        return self.changed * 100 / self.total if self.total else 0.0

    def picture(self, fmt: str = "JPEG") -> bytes:
        """The screen now, faded towards white, with the changed pixels tinted red and each region outlined."""
        size = self.after.size
        out = Image.blend(self.after, Image.new("RGB", size, WHITE), FADE)
        out.paste(Image.blend(self.after, Image.new("RGB", size, RED), TINT), mask=self.mask)
        draw = ImageDraw.Draw(out)
        for r in self.regions:
            draw.rectangle([r.x, r.y, r.x + r.width - 1, r.y + r.height - 1], outline=RED, width=OUTLINE)
        buf = io.BytesIO()
        out.save(buf, fmt, **({"quality": JPEG_QUALITY} if fmt == "JPEG" else {}))
        return buf.getvalue()


def shown(percent: float, changed: int = 1, threshold: float | None = None) -> str:
    """A share as an answer says it: two decimals at most, `0%` only when nothing changed. Next to a pass bar it
    never reads as the bar itself when it is above it (0.504% against 0.5% says 0.504%)."""
    value = round(percent, 2)
    if changed and value == 0:
        value = 0.01  # a changed pixel is never reported as none
    if threshold is not None and percent > threshold and value <= threshold:
        value = round(percent, 3) if round(percent, 3) > threshold else percent
    return f"{value:g}%"


def paint(image: Image.Image, boxes: list[tuple[float, float, float, float]]) -> None:
    """Fill rects (x, y, width, height in pixels) with the neutral colour, in place. A rect is cut to the picture
    first, so one far larger than the screen paints all of it."""
    draw = ImageDraw.Draw(image)
    for x, y, w, h in boxes:
        x0, y0 = max(0, round(x)), max(0, round(y))
        x1, y1 = min(image.width, round(x + w)), min(image.height, round(y + h))
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=PAINT)


def compare(before: Image.Image, after: Image.Image, painted: list[tuple[float, float, float, float]] = ()) -> Result:
    """Compare two RGB pictures of one size, with `painted` (pixel rects: ignored elements and the status bar)
    filled on copies of both first. Pictures of another size are the caller's to refuse (`ValueError` here)."""
    if before.size != after.size:
        raise ValueError(f"the pictures differ in size ({before.width}x{before.height} and {after.width}x{after.height})")
    a, b = before.copy(), after.copy()
    if painted:
        paint(a, list(painted))
        paint(b, list(painted))
    red, green, blue = ImageChops.difference(a, b).split()
    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(_TABLE)
    changed = mask.histogram()[255]
    regions = _regions(mask) if changed else []
    return Result(b, mask, changed, regions)


def _regions(mask: Image.Image) -> list[Region]:
    """Joined cells of changed pixels, as boxes, the largest first. Inverted, a cell is 255 only when none of its
    pixels changed (the average of 255s is exactly 255), so one changed pixel marks its cell without any rounding."""
    width, height = mask.size
    grid = ImageChops.invert(mask).reduce(CELL)
    cols, rows = grid.size
    marked = [v != 255 for v in grid.tobytes()]
    seen = [False] * len(marked)
    found: list[Region] = []
    for start, hit in enumerate(marked):
        if not hit or seen[start]:
            continue
        seen[start] = True
        stack, lo_c, hi_c, lo_r, hi_r = [start], cols, -1, rows, -1
        while stack:
            cell = stack.pop()
            r, c = divmod(cell, cols)
            lo_c, hi_c, lo_r, hi_r = min(lo_c, c), max(hi_c, c), min(lo_r, r), max(hi_r, r)
            for rr in range(max(r - 1, 0), min(r + 2, rows)):
                for cc in range(max(c - 1, 0), min(c + 2, cols)):
                    n = rr * cols + cc
                    if marked[n] and not seen[n]:
                        seen[n] = True
                        stack.append(n)
        x, y = lo_c * CELL, lo_r * CELL
        found.append(Region(x, y, min(width, (hi_c + 1) * CELL) - x, min(height, (hi_r + 1) * CELL) - y))
    found.sort(key=lambda r: r.width * r.height, reverse=True)
    return found


def fit(png: bytes, width: int) -> Image.Image:
    """A screenshot as an RGB picture `width` px wide. `bad_params` for a picture larger than any screen, or one that
    would be scaled to such a size (a very tall, narrow one), before anything is decoded or allocated."""
    img = Image.open(io.BytesIO(png))
    if img.width * img.height > MAX_PIXELS:
        raise Refused("bad_params", f"the screenshot is {img.width}x{img.height} px, larger than any screen")
    height = max(1, round(img.height * width / img.width))
    if img.width != width and width * height > MAX_PIXELS:
        raise Refused("bad_params", f"the screenshot ({img.width}x{img.height} px) would be {width}x{height} px at "
                                    f"width {width}, larger than any screen")
    img = img.convert("RGB")
    if img.width != width:
        img = img.resize((width, height), Image.LANCZOS)
    return img


def read(path: Path) -> tuple[Image.Image, bytes]:
    """A saved reference as an RGB picture, and the exact bytes it was decoded from (the file is read once, so the
    two can't differ); `bad_params` when the file isn't a picture, or is far larger than any phone screen (a script's
    references come from anyone, so a huge one is refused before it is decoded)."""
    unreadable = Refused("bad_params", f"the reference file {path} can't be read as a picture; save the reference "
                                       "again with update: true.")
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise Refused("bad_params", f"the reference file {path} is {img.width}x{img.height} px, larger than "
                                            "any screen; save the reference again with update: true.")
            return img.convert("RGB"), data
    except Refused:
        raise
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        raise unreadable from None


def load(path: Path) -> Image.Image:
    """A saved reference as an RGB picture (see `read`)."""
    return read(path)[0]


# Directory file descriptors (POSIX): the walk, the temporary file and the move all go through a folder that is held
# open, so nothing is looked up by path after a folder was checked. Windows has none of this (`dir_fd` is not there).
ANCHORED = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
            and {os.open, os.mkdir, os.stat, os.unlink, os.rename} <= os.supports_dir_fd)  # (os.replace goes with rename)


def _after_check(folder: Path) -> None:
    """A seam for tests, called once the folder is checked and before the file is written: the moment in which a
    second process could swap a folder for a link. Does nothing."""


def real_folder(folder: Path, root: Path) -> Path:
    """`folder` made (as far as it isn't there), and checked: every folder from `root` down to it must be a real
    folder, not a symlink or a junction to somewhere else (`OSError` otherwise, before anything is made behind the
    link). `root` is trusted, even when it is a link itself; it is made when it isn't there. The folder's real path.
    By path, so only the fallback for systems without directory handles (see `write_file`)."""
    root = Path(os.path.abspath(root))
    try:
        parts = Path(os.path.abspath(folder)).relative_to(root).parts
    except ValueError:
        raise OSError(errno.EPERM, f"{folder} is outside {root}; nothing is written there") from None
    root.mkdir(parents=True, exist_ok=True)
    here = Path(os.path.realpath(root))
    for part in parts:
        step = here / part
        if os.path.normcase(os.path.realpath(here)) != os.path.normcase(str(here)):  # (looked at again just before)
            raise OSError(errno.EPERM, f"{here} is a link to another folder; nothing is written through it")
        step.mkdir(exist_ok=True)
        if os.path.normcase(os.path.realpath(step)) != os.path.normcase(str(step)):
            raise OSError(errno.EPERM, f"{step} is a link to another folder; nothing is written through it")
        here = step
    return here


def _same_folder(path: Path | str, folder: Path) -> bool:
    """Is `path`'s real folder still the checked `folder`?"""
    return os.path.normcase(os.path.dirname(os.path.realpath(path))) == os.path.normcase(str(folder))


def _write_by_path(dest: Path, write: Callable[[BinaryIO], object], root: Path) -> None:
    """The fallback for Windows. The folders are checked, then the temporary file is written, and its real folder is
    resolved again before it is moved: when it is no longer the checked one the file is deleted and the write is
    refused; the destination's folder is looked at once more after the move. This narrows the window in which a
    folder can be swapped for a link, but can't close it: between a check and the next call there is still a moment
    (Windows offers no way to hold a folder open and have a file made and moved through it)."""
    folder = real_folder(dest.parent, root)
    dest = folder / dest.name
    _after_check(folder)
    fd, name = tempfile.mkstemp(dir=folder, prefix=dest.name + ".", suffix=".part")
    swapped = OSError(errno.EPERM, f"{folder} changed into a link to another folder while the file was written; "
                                   "nothing is written through it")
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
        if not _same_folder(name, folder):
            raise swapped
        os.replace(name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise
    if not _same_folder(dest, folder):  # (too late to undo; said, so that nobody trusts the file)
        raise swapped


def _open_folder(parent_fd: int, part: str, step: Path) -> int:
    """`part` of the folder `parent_fd`, made when it isn't there, opened without following a link (the handle is
    what the rest goes through)."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(part, 0o777, dir_fd=parent_fd)
    try:
        return os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR) and stat.S_ISLNK(os.stat(part, dir_fd=parent_fd, follow_symlinks=False).st_mode):
            raise OSError(errno.EPERM, f"{step} is a link to another folder; nothing is written through it") from None
        raise


def _write_anchored(dest: Path, write: Callable[[BinaryIO], object], root: Path) -> None:
    """POSIX: from `root` (trusted, opened as it is) down, each folder is made and opened with `dir_fd` and
    `O_NOFOLLOW`, the temporary file is made in the last one with `O_EXCL | O_NOFOLLOW`, and moved with
    `os.replace(src_dir_fd, dst_dir_fd)`: no name is resolved by path after it was checked, so a folder swapped for a
    link in the meantime changes nothing (the file lands in the folder that was checked, wherever it is by then)."""
    root = Path(os.path.abspath(root))
    try:
        parts = Path(os.path.abspath(dest.parent)).relative_to(root).parts
    except ValueError:
        raise OSError(errno.EPERM, f"{dest.parent} is outside {root}; nothing is written there") from None
    root.mkdir(parents=True, exist_ok=True)
    opened = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        here = root
        for part in parts:
            here = here / part
            opened.append(_open_folder(opened[-1], part, here))
        folder_fd = opened[-1]
        _after_check(here)
        name = f"{dest.name}.{secrets.token_hex(8)}.part"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder_fd)
        try:
            with os.fdopen(fd, "wb") as out:
                write(out)
            os.replace(name, dest.name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=folder_fd)
            raise
    finally:
        for held in opened:
            os.close(held)


def write_file(dest: Path, write: Callable[[BinaryIO], object], root: Path | None = None) -> None:
    """Write a file whole or not at all, and never through a link. Every folder from `root` (the trusted tree the
    file belongs in: the references store, the script's folder; by default the folder above `dest`'s) down to
    `dest`'s must be a real folder (a symlink or a junction to somewhere else is refused: `OSError`), the bytes go
    to a new file with its own name beside `dest` (made exclusively, so it can't be a link someone planted), and
    that is moved over `dest`. A link at `dest` is replaced, not followed. A failed write leaves no temporary file.

    The threat: a second process of the same user that swaps a folder for a link between the check and the write.
    This is a local tool writing into the user's own folders (`~/.tapwright`, the folders beside their scripts), so
    that takes a hostile process running as the user at that very moment; a script file alone can't do it. Still it
    is closed as far as the system allows: on POSIX with directory handles (nothing is resolved by path after the
    check, `_write_anchored`), on Windows by looking again before and after the move (`_write_by_path`), which narrows
    the window and can't close it."""
    root = dest.parent.parent if root is None else root
    (_write_anchored if ANCHORED else _write_by_path)(dest, write, root)


def encode(image: Image.Image) -> bytes:
    """A reference's PNG bytes (lossless: a reference must not drift)."""
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def save(path: Path, image: Image.Image, root: Path | None = None) -> bytes:
    """Write a reference as PNG, whole or not at all (see `write_file`). The bytes that were written."""
    data = encode(image)
    write_file(path, lambda out: out.write(data), root)
    return data


# ---- the words ----
def point_regions(regions: list[Region], scale: float) -> list[tuple[int, int, int, int]]:
    """Regions in points, whole numbers (x, y, width, height)."""
    return [(round(r.x / scale), round(r.y / scale), round(r.width / scale), round(r.height / scale)) for r in regions]


def named_near(box: tuple[int, int, int, int], items: list[ui.Element]) -> list[str]:
    """The two smallest named elements a region (points) touches, as element lines without the ref. A page-sized
    container touches every change, so one far larger than the region is left out (Tapline's rule)."""
    x, y, w, h = box
    limit = max(w * h * 4, 2500)
    near = []
    for e in items:
        ex, ey, ew, eh = e.rect
        if not (e.text or e.identifier):
            continue
        if ex < x + w and ex + ew > x and ey < y + h and ey + eh > y and ew * eh <= limit:
            near.append((ew * eh, e))
    near.sort(key=lambda pair: pair[0])
    return [e.line(ref=False, actions=False, place=False) for _, e in near[:2]]


def region_lines(regions: list[Region], scale: float, items: list[ui.Element]) -> list[str]:
    """`- @16,329 193x48: button "Save" id=lab-save` per region, at most MAX_LINES, then `(+N more regions)`."""
    boxes = point_regions(regions, scale)
    lines = []
    for box in boxes[:MAX_LINES]:
        named = named_near(box, items)
        lines.append(f"- @{box[0]},{box[1]} {box[2]}x{box[3]}" + (f": {', '.join(named)}" if named else ""))
    if len(boxes) > MAX_LINES:
        lines.append(f"(+{len(boxes) - MAX_LINES} more regions)")
    return lines


def failure(name: str, result: Result, threshold: float, scale: float) -> str:
    """Why a check didn't hold (§14.2): the share, the pass bar and where, without element names."""
    boxes = point_regions(result.regions, scale)
    places = ", ".join(f"@{x},{y} {w}x{h}" for x, y, w, h in boxes[:MAX_LINES])
    if len(boxes) > MAX_LINES:
        places += f" (+{len(boxes) - MAX_LINES} more regions)"
    n = len(boxes)
    return (f'{shown(result.percent, result.changed, threshold)} changed from the reference "{name}" (threshold {threshold:g}%), '
            f"in {n} region{'s' if n != 1 else ''} (points): {places}.")


def missing(name: str) -> str:
    return f'no reference "{name}" yet; save one with compare_screen {{ name: "{name}" }}.'


# ---- the parameters ----
def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def check_ignore(ignore: object, label: str = "") -> None:
    """`ignore` as given: a list of targets, or of rects {x, y, width, height} in points (`bad_params` otherwise)."""
    if not isinstance(ignore, list) or not ignore:
        raise Refused("bad_params", f"{label}ignore must be a non-empty list of targets, like "
                                    '[{"id": "clock"}, {"x": 0, "y": 0, "width": 100, "height": 40}]')
    for entry in ignore:
        if not isinstance(entry, dict):
            raise Refused("bad_params", f"{label}each ignore entry must be an object, not {entry!r}")
        if any(entry.get(k) is not None for k in ("width", "height")):
            if not all(_number(entry.get(k)) and abs(entry[k]) <= MAX_POINTS for k in ("x", "y", "width", "height")) \
                    or entry["width"] <= 0 or entry["height"] <= 0:
                raise Refused("bad_params", f"{label}an ignored rect needs x, y, width and height as numbers in "
                                            "points, the last two above 0")
        else:
            if ui.parse_target(entry).kind == "point":  # (bad_params, with an example, for anything else wrong)
                raise Refused("bad_params", f"{label}an ignored point has no size; give x, y, width and height, or "
                                            "an element's id, label or text")


def check_params(args: dict, label: str = "") -> None:
    """The parameters `compare_screen` and `expect.matches` share, checked before anything runs. `label` starts
    each message (`step 3: `) inside a batch."""
    name = args.get("name", args.get("matches"))
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused("bad_params", f"{label}name the reference with letters, digits, '.', '_' and '-' (at most 61 "
                                    f"characters, starting with a letter or digit), not {name!r}")
    if args.get("update") is not None and not isinstance(args["update"], bool):
        raise Refused("bad_params", f"{label}update must be true or false, not {args['update']!r}")
    threshold = args.get("threshold")
    if threshold is not None and (not _number(threshold) or not 0 <= threshold <= 100):
        raise Refused("bad_params", f"{label}threshold must be a number from 0 to 100 (percent), not {threshold!r}")
    if args.get("ignore") is not None:
        check_ignore(args["ignore"], label)


def ignored(items: list[ui.Element], ignore: list[dict]) -> tuple[list[tuple[float, float, float, float]], list[str]]:
    """The rects (points) to paint out and the notes for the targets that aren't on the screen. A target is looked
    up on the list each time, so a clock that moved is still covered. Several matches without an `index` are
    `ambiguous`, as for a touch."""
    rects, notes = [], []
    for entry in ignore:
        if entry.get("width") is not None:
            rects.append((float(entry["x"]), float(entry["y"]), float(entry["width"]), float(entry["height"])))
            continue
        target = ui.parse_target(entry)
        try:
            found = ui.resolve(items, target)
        except Refused as e:
            if e.code != "not_found":
                raise
            notes.append(f'ignore {target.kind} "{getattr(target, target.kind)}" isn\'t on the screen; skipped.')
            continue
        if isinstance(found, ui.Element):
            rects.append(found.rect)
    return rects, notes


# ---- the files ----
def references_dir() -> Path:
    """Where references are kept: ~/.tapwright/references/<bundle id>/<name>.png. A function so tests can move it."""
    from .store import HOME
    return HOME / "references"


def reference_path(bundle: str, name: str) -> Path:
    return references_dir() / re.sub(r"[^\w.-]", "_", bundle) / f"{name}.png"


def script_references(script: Path) -> Path:
    """The folder that travels with a script (§14.3): `<script dir>/<script name>.references`."""
    return script.parent / f"{script.stem}.references"


def script_diffs(script: Path) -> Path:
    return script.parent / f"{script.stem}.diff"


def find(name: str, bundle: str, script: Path | None = None) -> Path | None:
    """The reference to compare with: in a replay the script's own folder first, then the store."""
    if script is not None:
        own = script_references(script) / f"{name}.png"
        if own.is_file():
            return own
    stored = reference_path(bundle, name)
    return stored if stored.is_file() else None


def export_to_script(pictures: dict[str, bytes], script: Path) -> tuple[Path, int]:
    """Write the pictures (name -> the exact PNG bytes a recording's checks used) next to a script. (the folder, how
    many were written) Nothing is read from the store or from any file: what a check used is what is exported, whoever
    rewrote the reference since."""
    folder = script_references(script)
    for name, data in pictures.items():
        write_file(folder / f"{name}.png", lambda out, data=data: out.write(data), script.parent)
    return folder, len(pictures)


def copy_to_script(names: list[str], bundle: str | None, script: Path,
                   sources: dict[str, Path] | None = None) -> tuple[Path, int]:
    """Copy the references of `names` next to a script. (the folder, how many were copied) A name in `sources` is
    copied from that file (the one a check was made against: a replayed script's own, say); any other from the
    store's folder for `bundle`, where _compare saved it."""
    folder, count = script_references(script), 0
    for name in dict.fromkeys(names):
        source = (sources or {}).get(name) or reference_path(bundle or "unknown", name)
        if source.is_file():
            data = source.read_bytes()
            write_file(folder / f"{name}.png", lambda out: out.write(data), script.parent)
            count += 1
    return folder, count
