"""The bug report (AGENT-TOOLS 1.16.2 §29): the signature (§29.5) and the page (§29.1). Pure functions: no phone and no
files (the folder is bugstore.py, making one bugmake.py, the tool bugtool.py).

Every text that reaches these functions has already gone through the single-pass redactor (§29.6); they only cut, and
a cut always comes after redaction. Tapwright's places are `place_<n>` until a person names them, so a bug is told apart
by its place's KEY (a signature key, `{"signature": "c41f9a07d2"}`), never by a name (§29.5, §29.8).
"""
from __future__ import annotations

import hashlib
import html
import json
import re

from . import findings

BUG_KINDS = (*findings.KINDS, "observed")  # `observed` is a kind only a bug report has (§29.3)
KIND_WORDS = {
    "crash": "Crash", "js_error": "JS error", "dead_end": "Dead end", "refused": "Refused touch", "slow": "Slow screen",
    "a11y": "Accessibility", "ux": "Usability", "unreachable": "Unreachable place", "expect_failed": "Check failed",
    "observed": "Observed",
}
# §29.8 (Tapwright): a system sheet or prompt that was in front at the moment, by its kind only
SYSTEM_SHEETS = findings.SYSTEM_SHEETS
HOWS = ("explore", "failure", "journey", "now")
HOW_WORDS = {"explore": "explore", "failure": "a failed check", "journey": "a journey", "now": "seen while driving"}
STEPS_SHOWN = 15
NOT_ASKED = "not asked"  # the `why` of a bug no check was asked for: the page and the answer say only `not checked`
TITLE_MAX = 80
UNSYMBOLICATED = "unsymbolicated"
SIGNATURE_HEX = 16
BUG_ID = re.compile(r"^bug_[0-9a-f]{16}(?:_[0-9]{1,4})?$")


# ---- the signature (§29.5) ----

def place_key_json(key: dict | None) -> str:
    """The place key as JSON with its keys sorted and no spaces: a signature key by its signature only (never `parts`),
    a route key with its `layer` when there is one; `null` when it isn't known."""
    if not isinstance(key, dict):
        return "null"
    if isinstance(key.get("signature"), str):
        return json.dumps({"signature": key["signature"]}, separators=(",", ":"), ensure_ascii=False)
    out = {k: key[k] for k in ("layer", "route", "tab") if key.get(k) is not None}
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# (an id is told apart by the letters and digits around it, not by `\b`: `order_7b9e3c1a-…` and `user_deadbeef12` hold
# ids after a `_` too, review R185 L4; a shared vector, so Tapline agrees)
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])")
_ISO_DATE = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
_CLOCK = re.compile(r"(?<![0-9])\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?")
_HEX_PREFIXED = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]+(?![0-9A-Za-z])")
_HEX_LONG = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}(?![0-9A-Za-z])")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def normalize_got(got: str) -> str:
    """The first line of `got`, normalized in this order: UUIDs, dates and times, clock times, hex numbers, every other run
    of digits (with its decimal part) become `#`; whitespace collapsed and the ends cut."""
    text = first_line(got)
    for pattern in (_UUID, _ISO_DATE, _CLOCK, _HEX_PREFIXED, _HEX_LONG, _DIGITS):
        text = pattern.sub("#", text)
    return " ".join(text.split())


def exception_type(reason: str) -> str:
    """The text of the reason before the first `:`, else its first word (`TypeError`, `EXC_BAD_ACCESS`)."""
    text = reason.strip()
    if ":" in text:
        return text.split(":", 1)[0].strip()
    return text.split()[0] if text.split() else ""


_FRAME_NUMBER = re.compile(r"^\s*\d+\s+")
_OBJC = re.compile(r"[-+]\[[^\]]+\]")
_OFFSET = re.compile(r"\s*\+\s*(?:0x[0-9a-fA-F]+|\d+)\s*$")
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]+\b")
_ANONYMOUS = re.compile(r"^(?:<anonymous>|anonymous|\?+|\(anonymous\)|\(anonymous function\)|<redacted>)$", re.I)


def _unmarked(frame: str) -> tuple[str, bool]:
    """A frame without its number and its mark, and whether it was marked `*` (the app's own code, §15.2). Tapwright
    writes ` 3 * Wayfare renderDays +148`; Tapline `*TripDetail.tsx:48 renderDays`."""
    text = frame.strip()
    marked = text.startswith("*")
    if not marked:
        rest = _FRAME_NUMBER.sub("", text, count=1)
        if rest != text and rest.startswith("*"):
            marked, text = True, rest
    if marked:
        text = text.lstrip("*").strip()
    return text, marked


def is_app_frame(frame: str) -> bool:
    return _unmarked(frame)[1]


def frame_symbol(frame: str) -> str:
    """The function name of a frame, without its address, offset, file name or line: `*TripDetail.tsx:48 renderDays`
    gives `renderDays`, `3 Wayfare 0x0000000102a4b3c8 -[TripViewController renderDays] + 148` gives
    `-[TripViewController renderDays]`, Tapwright's ` 3 * Wayfare renderDays(_:) +148` gives `renderDays(_:)`. A frame
    with no symbol, or an anonymous one, gives `unsymbolicated`."""
    text, _ = _unmarked(frame)
    objc = _OBJC.search(text)
    if objc is not None:
        return objc.group(0)
    text = _FRAME_NUMBER.sub("", text, count=1)
    parts = text.split(None, 1)  # the image (or `file:line`), then the symbol
    symbol = parts[1] if len(parts) > 1 else ""
    symbol = _OFFSET.sub("", symbol)
    symbol = " ".join(_ADDRESS.sub("", symbol).split())
    symbol = _OFFSET.sub("", symbol).strip()
    if not symbol or symbol.startswith("+") or _ANONYMOUS.match(symbol):
        return UNSYMBOLICATED
    return symbol


def top_app_symbol(frames: list[str] | tuple[str, ...]) -> str:
    """The symbol of the first frame marked `*`, or `unsymbolicated` when there is none."""
    top = next((f for f in frames if is_app_frame(f)), None)
    return UNSYMBOLICATED if top is None else frame_symbol(top)


def signature(kind: str, place_key: dict | None, got: str, crash_reason: str | None = None,
              crash_frames: list[str] | tuple[str, ...] = ()) -> str:
    """The first 16 hex digits of the SHA-256 of the parts, in UTF-8, joined by `\\n`: the kind, the place key, the
    normalized `got` (already redacted), and for a crash its exception type and the top app frame's symbol."""
    parts = [kind, place_key_json(place_key), normalize_got(got)]
    if kind == "crash":
        parts += [exception_type(crash_reason or ""), top_app_symbol(list(crash_frames))]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:SIGNATURE_HEX]


# ---- text ----

def one_line(text: str) -> str:
    return " ".join(re.sub(r"[\r\n\u2028\u2029]+", " ", text).split())


_ROOTED = r"(?:[A-Za-z]:[\\/]|/(?:Users|home|private|var|tmp|Volumes)/)"
_QUOTED_PATH = re.compile(rf"([\"']){_ROOTED}[^\"'\r\n]*[\\/]([^\"'\r\n\\/]+)\1")
# (a folder's name may hold spaces, quotes, brackets and a placeholder the redactor left, `<tester's password>`)
_BARE_PATH = re.compile(rf"(?<![\w:/.\\-]){_ROOTED}(?:[^\\/:\"|\r\n]+[\\/])+([^\\/\s:\"'()<>|]+)")
_ANY_PATH = re.compile(r"(?<![\w:/.\\-])(?:[A-Za-z]:)?(?:[\\/][^\s\\/:'\"()]+){2,}")
# (review R185 M2) a `file://` URL, a UNC path `\\MACHINE\share\...`, and a path that ends at the user folder itself
_FILE_URL = re.compile(r"(?i)file://[^\s/\"'<>()]*((?:/[^\s/\"'<>()]+)+)")
_UNC = re.compile(r"(?<![\w\\])\\\\[^\\\s\"'<>()|]+(?:\\[^\\\s\"'<>()|]+)+")
_USER_HOME = re.compile(r"(?<![\w:/.\\-])(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)|/(?:Users|home))[\\/]"
                        r"[^\\/\s:\"'()<>|]+[\\/]?(?=$|[\s\"'():,;<>|])(?!(?:\s+[^\s\\/\"'<>|:]+)+[\\/])")
# (the last part: `C:\Users\Jane Doe\proj\…` is a path under a folder with a space in its name, cut whole below)


def strip_paths(text: str) -> str:
    """A path in a frame or a log line cut to its file name: no user folder, no machine name (§29.6). A path under a user
    folder may hold spaces (`C:\\Users\\Jane Doe\\...`): it is cut whole, so what follows (`:48 renderDays`) stays. A
    `file://` URL and a UNC path are cut the same way; a path that ends at a user folder itself becomes `~`."""
    text = _FILE_URL.sub(lambda m: m.group(1).rsplit("/", 1)[-1], text)
    text = _UNC.sub(lambda m: m.group(0).rsplit("\\", 1)[-1], text)
    text = _USER_HOME.sub("~", text)
    text = _QUOTED_PATH.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}", text)
    text = _BARE_PATH.sub(lambda m: m.group(1), text)
    return _ANY_PATH.sub(lambda m: re.split(r"[\\/]", m.group(0))[-1], text)


def frame_shown(frame: str) -> str:
    """A frame as the page shows it: the app's own marked `*`, its number, addresses and offsets dropped (§29.6), any path cut
    to its file name."""
    text, marked = _unmarked(frame)
    text = _FRAME_NUMBER.sub("", text, count=1)
    text = " ".join(_ADDRESS.sub("", _OFFSET.sub("", text)).split())
    text = _OFFSET.sub("", text).strip()
    return ("*" if marked else "") + one_line(strip_paths(text))


def short_got(got: str) -> str:
    """The title's `got`: the first line; over 80 characters, its first 45 and its last 30 with ` … ` between."""
    text = one_line(first_line(got))
    if len(text) <= TITLE_MAX:
        return text
    return f"{text[:45]} … {text[-30:]}"


_ISSUE = re.compile(r"^\s*\d+\.\s+([a-z][a-z_]*):\s+(.*)$")
_ELEMENT = re.compile(r'^(?:e\d+\s+)?([a-z]+)(?:\s+\(from others\))?(?:\s+("(?:[^"\\]|\\.)*"))?(.*)$')
_ELEMENT_ID = re.compile(r"(?<![\w-])id=(\S+)")
ISSUE_NAME_MAX = 30


def issue_title(got: str) -> str | None:
    """The short title of an accessibility or usability finding (W177): its first issue's rule and what it is about,
    `Contrast: id bug-note`, `Dead end: text "Nothing here leads…"`, never an element line cut mid-word. None when the
    first line isn't an issue line."""
    m = _ISSUE.match(first_line(got))
    if m is None:
        return None
    rule = m.group(1).replace("_", " ").strip().capitalize()
    rest = m.group(2).strip()
    element = _ELEMENT.match(rest) if not rest.startswith("the screen") else None
    if element is None:
        return f"{rule}: the screen"
    role, quoted, tail = element.group(1), element.group(2), element.group(3)
    ident = _ELEMENT_ID.search(tail)
    if ident is not None:
        return f"{rule}: id {ident.group(1).rstrip(':,;')}"
    if quoted:
        try:
            name = one_line(str(json.loads(quoted)))
        except ValueError:
            name = one_line(quoted.strip('"'))
        name = name if len(name) <= ISSUE_NAME_MAX else name[:ISSUE_NAME_MAX - 1].rstrip() + "…"
        return f'{rule}: {role} "{name}"'
    return f"{rule}: a {role}"


def title(kind: str, place: str | None, got: str) -> str:
    shown = (issue_title(got) if kind in findings.MULTILINE_KINDS else None) or short_got(got)
    return f"{KIND_WORDS.get(kind, kind)} on {place or 'an unnamed screen'}: {shown}"


def bug_id(sig: str, n: int = 1) -> str:
    return f"bug_{sig}" if n <= 1 else f"bug_{sig}_{n}"


# ---- the steps, for a person (§29.1) ----

def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{({1: 'st', 2: 'nd', 3: 'rd'}).get(n % 10, 'th')}"


_PLAIN_SEGMENT = re.compile(r"^[a-z][a-z-]{0,23}$")


def cut_down_link(url: str) -> str:
    """A link for a person: the scheme and the path's plain words, never a query, a fragment or a value (§18.3)."""
    m = re.match(r"^([A-Za-z][\w+.-]*:)(//)?([^/?#]*)([^?#]*)", url.strip())
    if m is None:
        return "(a link)"
    host = m.group(3) or ""
    segments = [s if s == "" or _PLAIN_SEGMENT.match(s) else "[value]" for s in (m.group(4) or "").split("/")]
    host = host if host == "" or _PLAIN_SEGMENT.match(host) else "[value]"
    return f"{m.group(1)}{m.group(2) or ''}{host}{'/'.join(segments)}"


def _phrase(target: dict, name: str | None) -> str:
    """`"Trips" (id trips-tab)`, `id trip-row-1`, or `"Save"`."""
    ident = target.get("id") if isinstance(target.get("id"), str) and target.get("id") else None
    shown = name or next((target[k] for k in ("label", "text") if isinstance(target.get(k), str) and target[k]), None)
    if shown:
        return f'"{one_line(shown)}" (id {ident})' if ident and name else f'"{one_line(shown)}"'
    return f"id {ident}" if ident else "the element"


LINK_CUT = "its query and fragment aren't kept: if the bug needs them, this link doesn't reproduce it"
NOT_A_TEST_APP = ("not one of this daemon's test apps (tapwright serve --apps): this page keeps no screen text and no log "
                  "lines, its picture is private, and any export of it (a zip too) needs confirm")


def step_line(step: dict, name: str | None = None, others: bool = False, needs_person: bool = False,
              link_cut: bool = False) -> str:
    """One step written for a person. The value of a typed step is never shown, whatever it was. A link is shown cut down
    (§18.3); `link_cut`: the link opened had a query or a fragment, which isn't kept."""
    tool = step.get("tool", "tap") if not step.get("row") else step.get("tool", "tap")
    target = step.get("target") if isinstance(step.get("target"), dict) else None
    row = step.get("row") is True or (target is not None and target.get("row") is True)
    index = step.get("index", target.get("index") if target else 1)
    index = index if isinstance(index, int) and not isinstance(index, bool) and index >= 1 else 1
    mark = " (from others)" if others else ""
    phrase = "" if row or target is None else _phrase(target, name)
    if tool in ("tap", "long_press", "toggle"):
        verb = {"tap": "Tap", "long_press": "Press and hold", "toggle": "Switch"}[tool]
        line = f"{verb} the {_ordinal(index)} row of the list." if row else f"{verb} {phrase or 'the element'}{mark}"
    elif tool == "type_text":
        line = f"Type something in {phrase}{mark}" if phrase else "Type something"
    elif tool == "scroll":
        to = step.get("to")
        direction = step.get("direction") if isinstance(step.get("direction"), str) else "down"
        line = "Scroll to the end" if to == "end" else "Scroll to the start" if to == "start" else f"Scroll {direction}"
    elif tool == "swipe":
        try:
            dx = float(step["x1"]) - float(step["x0"])
            dy = float(step["y1"]) - float(step["y0"])
        except (KeyError, TypeError, ValueError):
            dx, dy = 0.0, 1.0
        line = ("Swipe up" if dy < 0 else "Swipe down") if abs(dy) >= abs(dx) else ("Swipe left" if dx < 0 else "Swipe right")
    elif tool == "back":
        line = "Go back"
    elif tool == "open_url":
        url = step.get("url") if isinstance(step.get("url"), str) else step.get("link")
        line = f"Open the link {cut_down_link(url if isinstance(url, str) else '')}" + (f" ({LINK_CUT})" if link_cut else "")
    else:
        line = f"Run {tool}" + (f" {one_line(name)}" if name else "")
    line = one_line(line)
    return f"{line} (needs a person)" if needs_person else line


# ---- the page (§29.1) ----

def reproduced_lines(meta: dict, kind: str) -> list[str]:
    r = meta.get("reproduced") or {}
    if r.get("checked") is False:
        if r.get("why") == NOT_ASKED:
            return ["not checked"]  # (§29.3, §29.9: no check was asked for, so there is no reason to give; W177)
        return [f"not checked ({one_line(str(r.get('why') or 'no check'))})"]
    n, of = r.get("n", 0), r.get("of", 0)
    runs = f"{n} of {of} runs"
    if kind == "observed":
        return [f"{runs} reached the same screen. A note can't be checked by the tool; look at it.",
                "Script: repro.json (a version 2 script)."]
    body = runs if n == of else f"{runs}: not reproduced" if n == 0 else f"{runs} (flaky)"
    return [f"{body}. Script: repro.json (a version 2 script)."]


def seen_lines(meta: dict) -> list[str]:
    s = meta["seen"]
    times = f"{s['count']} {'time' if s['count'] == 1 else 'times'}"
    builds = s["builds"]
    shown = f"build {builds[0]}" if len(builds) == 1 else f"builds {', '.join(builds)}"
    occurrences = s.get("occurrences")
    more = (f" It showed {occurrences} times in all (a run can show it more than once)."
            if isinstance(occurrences, int) and not isinstance(occurrences, bool) and occurrences > s["count"] else "")
    return [f"{times}; first {s['first'][:10]}, last {s['last'][:10]}; {shown}.{more}"]


def picture_lines(finding: dict, meta: dict, picture_file: str | None) -> list[str]:
    pic = finding.get("picture")
    if picture_file is not None:
        return [f"![The screen at the moment]({picture_file})"]
    if not isinstance(pic, dict):
        return ["There is no picture."]
    if pic.get("private") is not False:
        if (meta.get("source") or {}).get("how") == "now":
            # (review R185 L3) a picture of the screen at the moment is kept nowhere else: only the call that made it can add it
            return ["A picture was taken and is private. It can only be included in the call that makes or updates the bug "
                    "(with_picture: true)."]
        return [f'A picture exists and is private. To include it: bug_report {{"bug": "{meta["id"]}", "with_picture": true}}']
    return ["A picture was taken, but its file is gone."]


def environment_lines(finding: dict, meta: dict) -> list[str]:
    """The Environment section: the app, the device, the tool and when, and the reset a check used (§29.1, §29.4)."""
    when = finding["when"]
    source = meta.get("source") or {}
    origin = HOW_WORDS.get(source.get("how"), "explore") + (f", finding {source['finding']}" if source.get("finding") else "")
    env = [f"- App: {finding['app']} {finding['app_version']} ({finding['build']})",
           f"- Device: {finding['device']['model']}, {finding['device']['os']}",
           f"- Tool: {finding['tool']} {finding['tool_version']}, found {when[:10]} {when[11:19]} UTC ({origin})"]
    r = meta.get("reproduced") or {}
    if r.get("checked") is not False and r.get("reset"):
        env.append(f"- Reset before each run: {r['reset']}")
    if meta.get("test_app") is not True:
        env.append(f"- The app is {NOT_A_TEST_APP}.")
    return env


def _place_line(finding: dict, meta: dict) -> str:
    name = finding.get("place") or "an unnamed screen"
    key = meta.get("place_key")
    if isinstance(key, dict) and isinstance(key.get("signature"), str):
        return f"{name} (signature {key['signature']})"
    if isinstance(key, dict) and isinstance(key.get("route"), list):
        tab = f", tab {key['tab']}" if key.get("tab") else ""
        return f"{name} (route {' › '.join(str(x) for x in key['route'])}{tab})"
    return f"{name} (its key isn't known)"


def markdown(finding: dict, meta: dict, picture_file: str | None) -> str:
    """The page as Markdown: nine sections in a fixed order, about 80 lines at most. `finding` and `meta` are already
    redacted; this only cuts."""
    out = [f"# {title(finding['kind'], finding.get('place'), finding['got'])}", ""]

    def section(name: str, lines: list[str]) -> None:
        out.extend([f"## {name}", *lines, ""])
    got = one_line("; ".join(finding["got"].split("\n")))
    expected = one_line(finding["expected"]).rstrip(".")
    section("What happened", [f"Expected: {expected}. Got: {got[:findings.LOG_CUT]}"])

    steps = list(finding.get("path") or [])
    how: list[str] = []
    if finding.get("path_from"):
        how.append(f"Start on {finding['path_from']}.")
    else:
        how.append("The screen it started on isn't known." if finding["kind"] == "observed"
                   else "It doesn't start from a known screen.")
    if meta.get("steps_cut"):
        how.append("Earlier steps couldn't be written down.")
    if not steps:
        how.append("There are no steps: it shows on the screen as it is.")
    first = max(0, len(steps) - STEPS_SHOWN)
    if first:
        how.append(f"The first {first} steps are in repro.json.")
    names = meta.get("names") or []
    needs = set(meta.get("needs_person") or [])
    others = set(meta.get("others") or [])
    cut = set(meta.get("links_cut") or [])
    for i in range(first, len(steps)):
        name = names[i] if i < len(names) else None
        how.append(f"{i - first + 1}. {step_line(steps[i], name, i in others, i in needs, i in cut)}")
    section("How to see it", how)

    section("Reproduced", reproduced_lines(meta, finding["kind"]))
    section("Environment", environment_lines(finding, meta))

    where = [f"- Place: {_place_line(finding, meta)}"]
    crash = finding.get("crash")
    if isinstance(crash, dict) and crash.get("frames"):
        where.append(f"- Exception: {one_line(strip_paths(crash.get('reason', '')))[:findings.LOG_CUT]}")
        where.append("- Frames: " + ", ".join(frame_shown(f) for f in crash["frames"][:findings.MAX_FRAMES]))
    elif isinstance(crash, dict) and crash.get("reason"):
        where.append(f"- Exception: {one_line(strip_paths(crash['reason']))[:findings.LOG_CUT]}; no frames in the report.")
    if finding.get("system") in SYSTEM_SHEETS:
        where.append(f"- A system sheet was in front: {finding['system']}.")
    section("Where", where)

    logs = [one_line(strip_paths(line))[:findings.LOG_CUT].replace("```", "'''")
            for line in list(finding.get("logs") or [])[-findings.MAX_LOGS:]]
    section("Logs", ["```", *logs, "```"] if logs else ["No log lines."])
    section("Picture", picture_lines(finding, meta, picture_file))
    section("Seen before", seen_lines(meta))
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def replace_section(page: str, name: str, lines: list[str]) -> str:
    """The page with the lines of its `## <name>` section replaced, the rest as it is (a missing section is added at the
    end)."""
    rows = page.rstrip("\n").split("\n")
    head = f"## {name}"
    if head not in rows:
        return "\n".join([*rows, "", head, *lines]) + "\n"
    start = rows.index(head)
    end = start + 1
    while end < len(rows) and not rows[end].startswith("## "):
        end += 1
    tail = [""] if end < len(rows) else []
    return "\n".join([*rows[:start + 1], *lines, *tail, *rows[end:]]) + "\n"


_STYLE = ("body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b;"
          "background:#fff}h1{font-size:1.4rem}h2{font-size:1.05rem;margin-top:1.4rem;border-bottom:1px solid #ddd}"
          "pre{background:#f4f4f4;padding:.6rem;overflow:auto;white-space:pre-wrap}img{max-width:20rem}")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([\w.-]+)\)$")


def page_html(page: str) -> str:
    """`report.html`: the same page as one file, inline style, no script, no request to anywhere, every text escaped. A
    picture is linked by its file name."""
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    rows = page.rstrip("\n").split("\n")
    body: list[str] = []
    heading = "Bug report"
    kind: str | None = None

    def close() -> None:
        nonlocal kind
        if kind is not None:
            body.append(f"</{kind}>")
        kind = None
    i = 0
    while i < len(rows):
        row = rows[i]
        if row.startswith("```"):
            close()
            code = []
            i += 1
            while i < len(rows) and not rows[i].startswith("```"):
                code.append(rows[i])
                i += 1
            body.append(f"<pre>{esc(chr(10).join(code))}</pre>")
        elif row.startswith("# "):
            close()
            heading = row[2:]
            body.append(f"<h1>{esc(row[2:])}</h1>")
        elif row.startswith("## "):
            close()
            body.append(f"<h2>{esc(row[3:])}</h2>")
        elif (m := re.match(r"^(?:- |\d+\. )(.*)$", row)) is not None:
            want = "ul" if row.startswith("- ") else "ol"
            if kind != want:
                close()
                body.append(f"<{want}>")
                kind = want
            body.append(f"<li>{esc(m.group(1))}</li>")
        elif (m := _IMAGE.match(row)) is not None:
            close()
            body.append(f'<p><img src="{esc(m.group(2))}" alt="{esc(m.group(1))}"></p>')
        elif row.strip():
            close()
            body.append(f"<p>{esc(row)}</p>")
        else:
            close()
        i += 1
    close()
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{esc(heading)}</title>'
            f"<style>{_STYLE}</style></head><body>\n" + "\n".join(body) + "\n</body></html>\n")


# ---- app folders (Tapline 1.8.6's rule, shared) ----

_SAFE_APP = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\.|$)", re.I)


def app_folder(app: str) -> str:
    """An app id as one folder name: a lowercase safe id keeps its name; any other is `_<cleaned>-<16 hex of SHA-256 over
    its UTF-16LE code units>`, so two ids never share a folder, also where the file system folds case."""
    if _SAFE_APP.match(app) and len(app) <= 100 and not app.endswith(".") and not _RESERVED.match(app):
        return app
    readable = re.sub(r"[^a-z0-9._-]", "_", app.lower())
    readable = re.sub(r"[._-]+$", "", re.sub(r"^[._-]+", "", readable)[:82])
    digest = hashlib.sha256(app.encode("utf-16-le", "surrogatepass")).hexdigest()[:16]
    return f"_{readable}-{digest}" if readable else f"_{digest}"
