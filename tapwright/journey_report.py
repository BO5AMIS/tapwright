"""The visual report of a journey (docs/AGENT-TOOLS.md §17.5): one folder with report.md, report.html (self-contained
but for its pictures, which are relative files), a screenshot per screen, a filmstrip, the findings' marked-up
screenshots and journey.gif (360 px wide, taps drawn as rings, at most 3 MB). Pillow only; no phone."""
from __future__ import annotations

import html
import io
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from . import filmstrip, journey as jr

GIF_WIDTH = 360
GIF_MAX_BYTES = 3_000_000
GIF_MAX_FRAMES = 60  # the GIF shows at most this many pictures (sampled over the path); the folder keeps them all
GIF_FRAME_MS = 800
GIF_HOLD_MS = (1200, 1600)  # the first and the last picture stay longer
SAME_PICTURE = 2.0  # mean grey difference (0-255) under which two frames are one still stretch
STILL_MAX_MS = 3000  # a stretch of equal pictures shows at most this long
SHOT_WIDTH = 540  # screenshots are kept at this width
STRIP_FRAMES = 12  # at most this many screens in the filmstrip
STRIP_WIDTH = 160
KEY_WIDTH = 360  # the key pictures in the stop answer
RING = (255, 59, 48)
SEVERITY_COLOUR = {"high": (214, 40, 40), "medium": (230, 140, 0), "low": (60, 120, 200)}


# ---- pictures ----
def store_shot(jpeg: bytes, path: Path) -> tuple[int, int]:
    """Keep a screenshot as a JPEG at most SHOT_WIDTH wide. Returns its size."""
    img = Image.open(io.BytesIO(jpeg))
    img.draft("RGB", (SHOT_WIDTH, SHOT_WIDTH * 2))
    img = img.convert("RGB")
    if img.width > SHOT_WIDTH:
        img = img.resize((SHOT_WIDTH, max(1, round(img.height * SHOT_WIDTH / img.width))), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=72)
    return img.size


def _ring(img: Image.Image, point: tuple[float, float], screen: tuple[float, float], radius: int) -> None:
    x, y = point[0] / screen[0] * img.width, point[1] / screen[1] * img.height
    draw = ImageDraw.Draw(img)
    for r, colour, w in ((radius + 2, (255, 255, 255), 6), (radius, RING, 4)):
        draw.ellipse([x - r, y - r, x + r, y + r], outline=colour, width=w)


def _gif_frame(frame: jr.Frame, width: int) -> Image.Image:
    img = Image.open(frame.path).convert("RGB")
    img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    if frame.point is not None and frame.screen_size:
        _ring(img, frame.point, frame.screen_size, max(10, width // 18))
    return img


def _same(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    small = (24, 48)
    diff = ImageChops.difference(a.convert("L").resize(small), b.convert("L").resize(small))
    return ImageStat.Stat(diff).mean[0] < SAME_PICTURE


def _encode(images: list[Image.Image], durations: list[int], colours: int) -> bytes:
    pal = [im.convert("P", palette=Image.ADAPTIVE, colors=colours) for im in images]
    buf = io.BytesIO()
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True,
                disposal=2)
    return buf.getvalue()


def make_gif(frames: list[jr.Frame], out: Path, width: int = GIF_WIDTH, max_bytes: int = GIF_MAX_BYTES) -> int:
    """journey.gif: every picture in order at `width`, a ring where the finger touched. To fit `max_bytes` it first
    stretches the still parts (pictures alike one after another become one that stays longer), then uses fewer
    colours, then drops every other picture (never the first or the last). Returns the file's size, 0 with no frames."""
    if not frames:
        return 0
    if len(frames) > GIF_MAX_FRAMES:  # spread over the path, the first and the last kept (memory: one RGB picture each)
        step = (len(frames) - 1) / (GIF_MAX_FRAMES - 1)
        frames = [frames[round(i * step)] for i in range(GIF_MAX_FRAMES)]
    images = [_gif_frame(f, width) for f in frames]
    # a ring marks a touch, so a frame with one is never merged into the one before it
    merged: list[tuple[Image.Image, int]] = []
    for frame, img in zip(frames, images):
        if merged and frame.point is None and _same(merged[-1][0], img):
            merged[-1] = (merged[-1][0], min(STILL_MAX_MS, merged[-1][1] + GIF_FRAME_MS))
        else:
            merged.append((img, GIF_FRAME_MS))
    merged[0] = (merged[0][0], max(merged[0][1], GIF_HOLD_MS[0]))
    merged[-1] = (merged[-1][0], max(merged[-1][1], GIF_HOLD_MS[1]))
    data = b""
    for colours in (256, 128, 64, 32):
        data = _encode([m[0] for m in merged], [m[1] for m in merged], colours)
        if len(data) <= max_bytes:
            break
    while len(data) > max_bytes and len(merged) > 2:
        keep = [m for i, m in enumerate(merged) if i % 2 == 0 or i == len(merged) - 1]
        merged = [(im, d * 2 if i not in (0, len(keep) - 1) else d) for i, (im, d) in enumerate(keep)]
        data = _encode([m[0] for m in merged], [min(m[1], STILL_MAX_MS) for m in merged], 32)
    out.write_bytes(data)
    return len(data)


def make_strip(j: jr.Journey, out: Path) -> tuple[int, int] | None:
    """filmstrip.jpg: the first picture of each screen on the path, at most STRIP_FRAMES (spread over the path)."""
    firsts: list[int] = []
    for i, f in enumerate(j.frames):
        if all(j.frames[k].screen != f.screen for k in firsts):
            firsts.append(i)
    if not firsts:
        return None
    if len(firsts) > STRIP_FRAMES:
        step = (len(firsts) - 1) / (STRIP_FRAMES - 1)
        firsts = [firsts[round(i * step)] for i in range(STRIP_FRAMES)]
    data, size = filmstrip.compose([j.frames[i].path.read_bytes() for i in firsts], STRIP_WIDTH)
    out.write_bytes(data)
    return size


def mark_finding(src: Path, out: Path, number: int, severity: str, rect: tuple | None,
                 screen_size: tuple[float, float] | None) -> None:
    """A copy of a screenshot with the finding's number, and a box round its element (or round the whole screen)."""
    img = Image.open(src).convert("RGB")
    colour = SEVERITY_COLOUR.get(severity, RING)
    draw = ImageDraw.Draw(img)
    if rect is not None and screen_size:
        sx, sy = img.width / screen_size[0], img.height / screen_size[1]
        x, y, w, h = rect
        draw.rectangle([x * sx, y * sy, (x + w) * sx, (y + h) * sy], outline=colour, width=4)
    else:
        draw.rectangle([0, 0, img.width - 1, img.height - 1], outline=colour, width=6)
    r = 16
    draw.ellipse([6, 6, 6 + 2 * r, 6 + 2 * r], fill=colour)
    text = str(number)
    x0, y0, x1, y1 = draw.textbbox((0, 0), text)
    draw.text((6 + r - (x1 - x0) / 2 - x0, 6 + r - (y1 - y0) / 2 - y0), text, fill=(255, 255, 255))
    img.save(out, "JPEG", quality=80)


def key_picture(path: Path, width: int = KEY_WIDTH) -> tuple[bytes, tuple[int, int]]:
    """A picture file as a small JPEG for the answer."""
    img = Image.open(path).convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue(), img.size


# ---- the document ----
def _rel(folder: Path, path: Path) -> str:
    return path.relative_to(folder).as_posix()


def model(j: jr.Journey, reached: bool, why: str | None, elapsed: float, pictures: dict, folder: Path) -> dict:
    """Everything a report says, as plain data for the two renderers."""
    who = j.persona or "no persona"
    facts = [("Goal", j.goal), ("Persona", who),
             ("Result", "reached" if reached else "not reached" + (f": {why}" if why else "")),
             ("Started", j.started_wall.strftime("%Y-%m-%d %H:%M:%S"))]
    if reached and why:
        facts[2] = ("Result", f"reached: {why}")
    if j.conditions:
        facts.append(("Conditions", ", ".join(f"{k} {v}" for k, v in j.conditions.items())))
    eyes = "a user's eyes (no ids, nothing off screen, no jump-scrolling)" if j.users_eyes else \
        "the agent's eyes (ids and off-screen elements shown)"
    if j.eyes_switches:
        name = {"user": "a user's eyes", "agent": "the agent's eyes"}
        first = "agent" if j.eyes_switches[0][1] == "user" else "user"  # the eyes it started in
        eyes = name[first] + "".join(f" until call {call}, then {name[to]}" for call, to in j.eyes_switches)
    facts.append(("Seen through", eyes))
    if j.allow:
        facts.append(("Shortcuts allowed", ", ".join(j.allow)))
    numbers = [("Taps", str(j.taps)), ("Screens", str(len(j.screens))),
               ("Typing", f"{j.typing} entries, {j.typed_chars} characters"), ("Scrolls", str(j.scrolls)),
               ("Time", f"{jr.duration(elapsed)} ({jr.duration(j.waiting)} waiting for loading)"),
               ("Wrong turns", str(j.wrong_turns)), ("Dead ends", str(j.dead_ends)),
               ("Errors", str(j.errors)), ("Hesitations", str(j.hesitations))]
    if reached:
        numbers.append(("Taps to the goal", str(j.taps)))
    path = []
    for s in j.screens:
        shot = next((f for f in j.frames if f.screen == s.index), None)
        path.append({"n": s.index, "title": s.title, "image": _rel(folder, shot.path) if shot else None,
                     "caption": shot.caption if shot else "", "acted": s.acted})
    findings = []
    for n, f in enumerate(j.findings, 1):
        findings.append({"n": n, "severity": f["severity"], "screen": f.get("screen") or "", "what": jr.finding_text(f),
                         "basis": f["basis"], "image": pictures.get(n)})
    notes = list(j.notes)
    if j.left_out:
        notes.append(f"{jr.plural(j.left_out, 'frame')} left out: another app was in front.")
    if j.over_cap:
        notes.append(f"{jr.plural(j.over_cap, 'picture')} not kept: a report holds at most {jr.MAX_FRAMES}.")
    if j.shortcuts:
        notes.append("Shortcuts used: " + "; ".join(j.shortcuts))
    return {"title": f'Journey: {j.goal}', "facts": facts, "numbers": numbers, "path": path, "findings": findings,
            "stress": list(j.stress), "notes": notes, "extra": [(h, list(lines)) for h, lines in j.extra_sections],
            "strip": pictures.get("strip"), "gif": pictures.get("gif")}


def markdown(m: dict) -> str:
    out = [f"# {m['title']}", ""]
    out += [f"- **{k}:** {v}" for k, v in m["facts"]]
    out += ["", "## Numbers", "", "| | |", "|---|---|", *[f"| {k} | {v} |" for k, v in m["numbers"]], ""]
    if m["gif"]:
        out += [f"![The journey]({m['gif']})", ""]
    out += ["## Path", ""]
    if m["strip"]:
        out += [f"![Filmstrip of the screens]({m['strip']})", ""]
    for p in m["path"]:
        out.append(f"{p['n']}. {p['title']}" + ("" if p["acted"] else " (nothing done here)"))
        if p["image"]:
            out.append(f"   ![screen {p['n']}]({p['image']})")
    out += ["", "## Findings", ""]
    if not m["findings"]:
        out.append("None filed.")
    for f in m["findings"]:
        out.append(f"{f['n']}. **{f['severity']}**" + (f" ({f['screen']})" if f["screen"] else "") + f": {f['what']}")
        out.append(f"   Basis: {f['basis']}")
        if f["image"]:
            out.append(f"   ![finding {f['n']}]({f['image']})")
    if m["stress"]:
        out += ["", "## Stress moments", "", *[f"- {line}" for line in m["stress"]]]
    for heading, lines in m["extra"]:
        out += ["", f"## {heading}", "", *lines]
    if m["notes"]:
        out += ["", "## Notes", "", *[f"- {n}" for n in m["notes"]]]
    return "\n".join(out) + "\n"


CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8dc;--high:#d62828;--medium:#e68c00;--low:#3c78c8}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f4;--mut:#a0a0a8;--line:#3a3a3e}}
body{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:980px;margin:0 auto;padding:16px}
h1{font-size:1.5rem}h2{margin-top:2rem;border-bottom:1px solid var(--line)}
table{border-collapse:collapse}td{padding:2px 12px 2px 0}td:first-child{color:var(--mut)}
img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px}
.path{display:flex;flex-wrap:wrap;gap:12px}.path figure{margin:0;width:200px}.path img{width:200px}
.sev{font-weight:700}.sev.high{color:var(--high)}.sev.medium{color:var(--medium)}.sev.low{color:var(--low)}
.finding{margin:12px 0;padding:8px 12px;border-left:4px solid var(--line)}
.finding img{max-width:240px}
"""


def page(m: dict) -> str:
    e = html.escape

    def pic(src: str | None, alt: str) -> str:
        return f'<img src="{e(src, quote=True)}" alt="{e(alt)}">' if src else ""
    body = [f"<h1>{e(m['title'])}</h1>", "<table>" + "".join(
        f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in m["facts"]) + "</table>",
        "<h2>Numbers</h2><table>" + "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in m["numbers"])
        + "</table>"]
    if m["gif"]:
        body.append(f"<p>{pic(m['gif'], 'The journey')}</p>")
    body.append("<h2>Path</h2>")
    if m["strip"]:
        body.append(f"<p>{pic(m['strip'], 'Filmstrip of the screens')}</p>")
    body.append('<div class="path">' + "".join(
        f"<figure>{pic(p['image'], 'screen ' + str(p['n']))}<figcaption>{p['n']}. {e(p['title'])}"
        f"{'' if p['acted'] else ' (nothing done here)'}</figcaption></figure>" for p in m["path"]) + "</div>")
    body.append("<h2>Findings</h2>")
    if not m["findings"]:
        body.append("<p>None filed.</p>")
    for f in m["findings"]:
        screen = f" ({e(f['screen'])})" if f["screen"] else ""
        body.append(f'<div class="finding"><span class="sev {e(f["severity"])}">{f["n"]}. {e(f["severity"])}</span>'
                    f"{screen}: {e(f['what'])}<br><small>Basis: {e(f['basis'])}</small><br>"
                    f"{pic(f['image'], 'finding ' + str(f['n']))}</div>")
    if m["stress"]:
        body.append("<h2>Stress moments</h2><ul>" + "".join(f"<li>{e(s)}</li>" for s in m["stress"]) + "</ul>")
    for heading, lines in m["extra"]:
        body.append(f"<h2>{e(heading)}</h2><pre>{e(chr(10).join(lines))}</pre>")
    if m["notes"]:
        body.append("<h2>Notes</h2><ul>" + "".join(f"<li>{e(n)}</li>" for n in m["notes"]) + "</ul>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(m['title'])}</title><style>{CSS}</style></head><body>{''.join(body)}</body></html>\n")


def write(j: jr.Journey, reached: bool, why: str | None, elapsed: float) -> dict:
    """Write the report into `j.folder`; returns {"folder", "strip", "key": [paths of up to two key pictures]}.
    Each part is best effort: a picture that can't be made is left out, never an error."""
    folder = j.folder
    pictures: dict = {}
    problems: list[str] = []
    try:
        if make_strip(j, folder / "filmstrip.jpg"):
            pictures["strip"] = "filmstrip.jpg"
    except Exception as e:  # noqa: BLE001 - a missing picture isn't worth losing the report for
        problems.append(f"no filmstrip: {e}")
    try:
        if make_gif(j.frames, folder / "journey.gif"):
            pictures["gif"] = "journey.gif"
    except Exception as e:  # noqa: BLE001
        problems.append(f"no GIF: {e}")
    for n, f in enumerate(j.findings, 1):
        i = f.get("frame")
        if i is None or i >= len(j.frames):
            continue
        frame = j.frames[i]
        try:
            out = folder / "findings" / f"finding-{n}.jpg"
            out.parent.mkdir(exist_ok=True)
            mark_finding(frame.path, out, n, f["severity"], f.get("rect"), frame.screen_size)
            pictures[n] = _rel(folder, out)
        except Exception as e:  # noqa: BLE001
            problems.append(f"no marked screenshot for finding {n}: {e}")
    j.notes.extend(problems)
    m = model(j, reached, why, elapsed, pictures, folder)
    (folder / "report.md").write_text(markdown(m), encoding="utf-8")
    (folder / "report.html").write_text(page(m), encoding="utf-8")
    (folder / "journey.json").write_text(json.dumps({k: v for k, v in m.items() if k != "extra"}, indent=1), encoding="utf-8")
    key: list[Path] = []
    if pictures.get("strip"):
        key.append(folder / pictures["strip"])
    first = next((folder / pictures[n] for n in range(1, len(j.findings) + 1) if n in pictures), None)
    if first is not None:
        key.append(first)
    elif j.frames and len(key) < 2:
        key.append(j.frames[-1].path)
    return {"folder": folder, "key": key[:2]}
