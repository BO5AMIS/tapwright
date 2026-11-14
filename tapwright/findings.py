"""Findings (AGENT-TOOLS §27): one shape for what a test or an explore found, so that a bug report is built from it
whichever tool found it.

`make` builds a finding from what the explorer has; `parse` reads one back (from JSON or a dict, from either tool); `to_dict`
and `dumps` write it. Every text of a finding goes through the redaction of §20.3 (`scrub`) whole, before any cut, so a
vault value is its placeholder and a cut can't leave half a value (§27.2). The only text kept as it is is the exact
`${typed}` a step that typed carries: the typed value itself is never kept. `picture.private` is true unless the caller
says the app is a test app (§27.2), and `visible_picture` shows a private one only when a person asked. A key a tool doesn't
know is ignored (§27.1). Pure functions: no phone and no files.
"""
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import routes

KINDS = ("crash", "js_error", "dead_end", "refused", "slow", "a11y", "ux", "unreachable", "expect_failed")
MULTILINE_KINDS = ("a11y", "ux")  # `got` holds the issues, one a line
MAX_ISSUES = 10
MAX_LOGS = 20
LOG_CUT = 300
MAX_FRAMES = 8
TYPED = "${typed}"  # what a step that typed carries instead of the text (§27.1)
_ID = re.compile(r"^f[0-9]{1,6}$")
_WHEN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_PLACE = re.compile(r"^[a-z0-9_]{1,60}$")
_FILE = re.compile(r"^(?!/|\\|[A-Za-z]:)(?!.*(?:^|[/\\])\.\.(?:[/\\]|$))[^\x00-\x1f]{1,200}$")
_TOOL = re.compile(r"^[a-z_]{1,40}$")
_ID_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()+,-]{0,79}$")  # a version, a build, a model, an OS: nothing free


class FindingError(ValueError):
    """A finding that isn't one (§27.1): the message says what is wrong. Nothing agent-facing: the explorer decides what to say."""


@dataclass(frozen=True)
class Device:
    model: str
    os: str


@dataclass(frozen=True)
class Picture:
    file: str
    private: bool = True


@dataclass(frozen=True)
class Crash:
    reason: str
    frames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """§27.1. `path` steps are dicts as `run` takes them (id, else label, else text, never a ref; a row as
    `{"row": true, "index": 1}`; a step that typed has the text `${typed}`)."""
    id: str
    kind: str
    app: str
    app_version: str
    build: str
    tool: str
    tool_version: str
    when: str
    device: Device
    expected: str
    got: str
    place: str | None = None
    path_from: str | None = None
    path: tuple[dict, ...] = ()
    picture: Picture | None = None
    logs: tuple[str, ...] = ()
    crash: Crash | None = None
    component_stack: tuple[str, ...] = ()
    repro: str | None = None
    count: int = 1


# ---- redaction (§27.2) ----

def redact(value: object, scrub: Callable[[str], str]) -> object:
    """Every string in `value` (dicts and lists, to any depth; keys are never touched) reaches `scrub` whole, whatever it
    looks like: only a string that is exactly `${typed}` is kept, as the tool writes it. Any other `${...}` is text like
    any other, so a value inside one is hidden (Tapline, PR #13 cycle 3)."""
    if isinstance(value, str):
        return value if value == TYPED else scrub(value)
    if isinstance(value, dict):
        return {k: redact(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, scrub) for v in value]
    return value


def quote_from_others(text: str) -> str:
    """Text that others wrote (§21.1) is quoted as data, marked as in the element line."""
    return f'(from others) "{text}"'


# ---- the pieces ----

def now_ms() -> str:
    """UTC, to the millisecond (§27.1)."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FindingError(f"{what} must be a non-empty text")
    return value


def _label(value: object, what: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, what)
    if not pattern.match(text):
        raise FindingError(f"{what} isn't one: {text!r}")
    return text


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _got(kind: str, text: str) -> str:
    """`expected` and `got` are one line each; for `a11y` and `ux` `got` holds the issues, at most 10 lines (§27.1)."""
    if kind in MULTILINE_KINDS:
        return "\n".join([line for line in (_one_line(x) for x in text.splitlines()) if line][:MAX_ISSUES])
    return _one_line(text)


def _step(raw: object, scrub: Callable[[str], str]) -> dict:
    """One path step: a step as `run` takes it, aimed as a road is (§18.2): never by ref. A step that typed keeps no text."""
    if not isinstance(raw, dict):
        raise FindingError("a path step is an object")
    if raw.get("row") is True:
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise FindingError("a row step has an index from 1")
        return {"row": True, "index": index}
    tool = raw.get("tool")
    if not isinstance(tool, str) or not _TOOL.match(tool):
        raise FindingError("a path step has a tool")
    target = raw.get("target")
    if isinstance(target, dict) and ("ref" in target or "refs" in target):
        raise FindingError("a path step is never aimed by ref (a ref doesn't outlive the screen)")
    if "ref" in raw:
        raise FindingError("a path step is never aimed by ref (a ref doesn't outlive the screen)")
    step = {k: v if k == "tool" else redact(v, scrub) for k, v in raw.items()}  # (a tool's name is never content: §20.3)
    if tool == "type_text":
        step["text"] = TYPED  # the value isn't kept
    return step


def typed_in(path: tuple[dict, ...]) -> bool:
    return any(s.get("tool") == "type_text" for s in path)


def _file(value: object, what: str, folder: str) -> str:
    text = _text(value, what)
    if not _FILE.match(text) or not text.replace("\\", "/").startswith(folder):
        raise FindingError(f"{what} is a relative path in {folder}, not {text!r}")
    return text


# ---- building ----

def make(*, finding_id: str, kind: str, app: str, app_version: str, build: str, tool: str, tool_version: str, device: Device,
         expected: str, got: str, scrub: Callable[[str], str], when: str | None = None, place: str | None = None,
         path_from: str | None = None, path: list[dict] | tuple[dict, ...] = (), picture_file: str | None = None,
         test_app: bool = False, logs: list[str] | tuple[str, ...] = (), crash_reason: str | None = None,
         crash_frames: list[str] | tuple[str, ...] = (), component_stack: list[str] | tuple[str, ...] = (),
         repro: str | None = None, count: int = 1) -> Finding:
    """A finding as §27.1 has it, made safe: all text redacted whole, then cut (a log line at 300 characters, at most 20
    lines, the top 8 frames, at most 10 issues). `picture_file` is where the picture was saved (`findings/f3.jpg`), None for
    none; it is private unless `test_app` (§27.2). `FindingError` for what can't be a finding."""
    if kind not in KINDS:
        raise FindingError(f"kind is one of {', '.join(KINDS)}, not {kind!r}")
    if (crash_reason is not None or crash_frames) and kind != "crash":
        raise FindingError("only a crash finding has a crash")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise FindingError("count is a whole number from 1")
    if crash_frames and crash_reason is None:
        raise FindingError("a crash has its reason")

    def safe(text: str) -> str:  # redaction is always before a cut
        if not isinstance(text, str):
            raise FindingError("a log line, a frame, a component or a version is a text")
        return text if text == TYPED else scrub(text)
    crash = Crash(safe(crash_reason), tuple(safe(f) for f in crash_frames[:MAX_FRAMES])) if crash_reason is not None else None
    return Finding(
        id=_label(finding_id, "id", _ID), kind=kind, app=_text(app, "app"),
        app_version=_label(safe(app_version), "app_version", _ID_TEXT), build=_label(safe(build), "build", _ID_TEXT),
        tool=_label(tool, "tool", _TOOL), tool_version=_label(safe(tool_version), "tool_version", _ID_TEXT),
        when=_label(when or now_ms(), "when", _WHEN),
        device=Device(_label(safe(device.model), "device model", _ID_TEXT), _label(safe(device.os), "device os", _ID_TEXT)),
        expected=_one_line(safe(_text(expected, "expected"))), got=_got(kind, safe(_text(got, "got"))),
        place=None if place is None else _label(place, "place", _PLACE),
        path_from=None if path_from is None else _label(path_from, "path_from", _PLACE),
        path=tuple(_step(s, scrub) for s in path),
        picture=None if picture_file is None else Picture(_file(picture_file, "picture file", "findings/"), not test_app),
        logs=tuple(safe(line)[:LOG_CUT] for line in logs[:MAX_LOGS]), crash=crash,
        component_stack=tuple(safe(c) for c in component_stack),
        repro=None if repro is None else _file(repro, "repro", "repro/"), count=count)


def visible_picture(finding: Finding, asked: bool = False) -> str | None:
    """The picture's file to show in a report, a bug report or an export: a private one only when a person asked (§27.2)."""
    pic = finding.picture
    return pic.file if pic is not None and (asked or not pic.private) else None


def repeat_key(finding: Finding) -> tuple[str, str | None, str]:
    """The same finding is the same kind, place and first line of `got` (§25.10)."""
    return finding.kind, finding.place, finding.got.split("\n", 1)[0]


def add(found: tuple[Finding, ...], new: Finding) -> tuple[Finding, ...]:
    """`new` joins `found`, unless it is a repeat of one: that one is reported once with its `count` raised (§25.10)."""
    key = repeat_key(new)
    for i, held in enumerate(found):
        if repeat_key(held) == key:
            return (*found[:i], replace(held, count=held.count + new.count), *found[i + 1:])
    return (*found, new)


def next_id(found: tuple[Finding, ...]) -> str:
    """The next `f<n>` of a run."""
    return f"f{1 + max((int(f.id[1:]) for f in found), default=0)}"


def repro_script(finding: Finding, name: str | None = None) -> dict:
    """The script that reaches it (§27.1, §9.4 version 2): `start` is `path_from`, the steps are `path`. A step that typed
    has `${typed}` and the script lists `typed` in `variables`, so a person gives a value; it is never in the file. A literal
    `${` in any other text is written `$${`, as a recording writes it."""
    steps = [s if s.get("row") else routes.escaped(s) for s in finding.path]
    return routes.route(name or finding.id, finding.app, finding.path_from or "", None,
                        ["typed"] if typed_in(finding.path) else [], steps)


# ---- JSON ----

def to_dict(finding: Finding) -> dict:
    """§27.1's JSON: the keys that have a value (`place` and `picture` are always there, null when none)."""
    out: dict = {"id": finding.id, "kind": finding.kind, "app": finding.app, "app_version": finding.app_version,
                 "build": finding.build, "tool": finding.tool, "tool_version": finding.tool_version, "when": finding.when,
                 "device": {"model": finding.device.model, "os": finding.device.os}, "place": finding.place,
                 "path_from": finding.path_from, "path": [dict(s) for s in finding.path], "expected": finding.expected,
                 "got": finding.got,
                 "picture": None if finding.picture is None else {"file": finding.picture.file, "private": finding.picture.private},
                 "logs": list(finding.logs)}
    if finding.crash is not None:
        out["crash"] = {"reason": finding.crash.reason, "frames": list(finding.crash.frames)}
    if finding.component_stack:
        out["component_stack"] = list(finding.component_stack)
    if finding.repro is not None:
        out["repro"] = finding.repro
    out["count"] = finding.count
    return out


def dumps(finding: Finding) -> str:
    return json.dumps(to_dict(finding), indent=2, ensure_ascii=False) + "\n"


def parse(raw: object, scrub: Callable[[str], str]) -> Finding:
    """A finding read from JSON text or a dict (either tool's), checked and redacted as `make` does; a key it doesn't know is
    ignored. A picture with no `private`, or any value but `false`, is private. `FindingError` for what isn't one."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise FindingError(f"a finding doesn't parse ({e})") from None
    if not isinstance(raw, dict):
        raise FindingError("a finding is a JSON object")
    device = raw.get("device")
    if not isinstance(device, dict):
        raise FindingError("device holds the phone's model and OS")
    crash = raw.get("crash")
    if crash is not None and not isinstance(crash, dict):
        raise FindingError("crash holds a reason and frames")
    picture = raw.get("picture")
    if picture is not None and not isinstance(picture, dict):
        raise FindingError("picture is an object or null")
    lists = {k: raw.get(k, []) for k in ("path", "logs", "component_stack")}
    for key, value in lists.items():
        if not isinstance(value, list):
            raise FindingError(f"{key} is a list")
    frames = (crash or {}).get("frames", [])
    if not isinstance(frames, list) or not all(isinstance(x, str) for x in [*frames, *lists["logs"], *lists["component_stack"]]):
        raise FindingError("logs, frames and component names are texts")
    def text(key: str, source: dict = raw) -> str:
        return _text(source.get(key), key)

    def maybe(key: str, source: dict = raw) -> str | None:
        return None if source.get(key) is None else text(key, source)
    count = raw.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int):
        raise FindingError("count is a whole number from 1")
    return make(finding_id=text("id"), kind=text("kind"), app=text("app"), app_version=text("app_version"), build=text("build"),
                tool=text("tool"), tool_version=text("tool_version"), when=text("when"),
                device=Device(text("model", device), text("os", device)), expected=text("expected"), got=text("got"),
                scrub=scrub, place=maybe("place"), path_from=maybe("path_from"), path=lists["path"],
                picture_file=None if picture is None else text("file", picture),
                test_app=picture is not None and picture.get("private") is False,
                logs=lists["logs"], crash_reason=None if crash is None else text("reason", crash), crash_frames=frames,
                component_stack=lists["component_stack"], repro=maybe("repro"), count=count)
