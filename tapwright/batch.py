"""Batches and scripts (docs/AGENT-TOOLS.md §9, v1.8): checking `run`'s steps and their `expect`, naming a step in an
answer, the script file, and the target a recording saves. The daemon runs the steps (Daemon._batch)."""
from __future__ import annotations

import json
from pathlib import Path

from . import compare, params, performance, ui
from . import shot as shotlib
from .control import Refused

MAX_STEPS = 50
BATCH_LIMIT = 120.0  # seconds a whole batch may take
EXPECT_DEFAULT, EXPECT_MIN, EXPECT_MAX = 2.0, 0.5, 30.0
STEP_TOOLS = ("tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "open_url", "wait", "wait_for",
              "alert", "press_button", "rotate", "open_app", "close_app", "compare_screen", "performance")
EXPECT_KINDS = ("text", "id", "label")
STEP_KEYS = ("tool", "expect", "route", "secure", "look", "screenshot")  # besides its tool's own arguments (a step's look and screenshot are ignored, §9.1)
FORMAT, VERSION = "agent-tools-script", 1


def check_steps(steps: object, *, script: bool = False) -> list[dict]:
    """The steps as given, checked (`bad_params` otherwise): 1 to 50 objects, each naming a tool a step may use, with
    a well-formed `expect`. A script may not use refs: they don't outlive the session that made them."""
    if not isinstance(steps, list) or not steps:
        raise Refused("bad_params", "steps must be a non-empty list of tool calls, like "
                                    '[{"tool": "tap", "target": {"id": "save"}}]')
    if len(steps) > MAX_STEPS:
        raise Refused("bad_params", f"a batch takes at most {MAX_STEPS} steps, not {len(steps)}; split it")
    for n, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise Refused("bad_params", f'step {n} must be an object with "tool", like {{"tool": "tap", ...}}')
        if step["tool"] not in STEP_TOOLS:
            raise Refused("bad_params", f"step {n}: {step['tool']!r} can't be a step; a step is one of "
                                        f"{', '.join(STEP_TOOLS)}")
        params.check(step["tool"], {k: v for k, v in step.items() if k not in STEP_KEYS}, f"step {n}: ")
        target = step.get("tap") if step["tool"] == "performance" else step.get("target")
        if script and isinstance(target, dict) and "ref" in target:
            raise Refused("bad_params", f"step {n}: a script can't use refs (they don't outlive a session); use the "
                                        "element's id, label or text")
        if step["tool"] == "compare_screen":
            check_compare_step(step, n, script)
        if "expect" in step:
            check_expect(step["expect"], n, script, step["tool"])
    return steps


def check_compare_step(step: dict, n: int, script: bool) -> None:
    """A `compare_screen` step (§14.2) is a check: it never saves, so `update` is refused here."""
    if step.get("name") is None:
        raise Refused("bad_params", f'step {n}: compare_screen needs a name, like {{"tool": "compare_screen", '
                                    '"name": "home"}')
    if step.get("update") is not None:
        raise Refused("bad_params", f"step {n}: a compare_screen step only checks and never saves; leave out update "
                                    "(save a reference with compare_screen on its own, outside run)")
    compare.check_params(step, f"step {n}: ")
    _no_refs(step.get("ignore"), n, script)
    width = step.get("width")
    if width is not None and (not isinstance(width, int) or isinstance(width, bool) or not shotlib.MIN_WIDTH <= width <= shotlib.MAX_WIDTH):
        raise Refused("bad_params", f"step {n}: compare_screen's width is a whole number of px from {shotlib.MIN_WIDTH} to {shotlib.MAX_WIDTH}, "
                                    f"not {width!r}")


def _no_refs(ignore: object, n: int, script: bool) -> None:
    if script and isinstance(ignore, list) and any(isinstance(i, dict) and "ref" in i for i in ignore):
        raise Refused("bad_params", f"step {n}: a script can't use refs in ignore (they don't outlive a session); use "
                                    "the element's id, label or text")


def check_expect(expect: object, n: int, script: bool = False, tool: str = "") -> None:
    if not isinstance(expect, dict):
        raise Refused("bad_params", f"step {n}: expect must be an object, like {{\"text\": \"Saved\"}}")
    if "under" in expect or tool == "performance":
        # a performance step is checked against limits, and only a performance step has them
        if tool != "performance" or set(expect) != {"under"}:
            raise Refused("bad_params", f"step {n}: {performance.UNDER_ONLY}")
        try:
            performance.check_under(expect["under"])
        except ValueError as e:
            raise Refused("bad_params", f"step {n}: {e}") from None
        return
    given = [k for k in EXPECT_KINDS if expect.get(k) is not None]
    matches = expect.get("matches") is not None
    if len(given) > 1 or (not given and not matches):
        raise Refused("bad_params", f"step {n}: expect needs exactly one of text, id or label, as a non-empty string "
                                    '(or "matches", the name of a reference picture, alone or beside one of them)')
    if given and (not isinstance(expect[given[0]], str) or not expect[given[0]]):
        raise Refused("bad_params", f"step {n}: expect needs exactly one of text, id or label, as a non-empty string")
    if matches:
        compare.check_params({"name": expect["matches"], "threshold": expect.get("threshold"),
                              "ignore": expect.get("ignore")}, f"step {n}: expect's matches: ")
        _no_refs(expect.get("ignore"), n, script)
    elif "threshold" in expect or "ignore" in expect:
        raise Refused("bad_params", f"step {n}: expect's threshold and ignore go with matches")
    if not given and any(k in expect for k in ("gone", "timeout")):
        raise Refused("bad_params", f"step {n}: expect's gone and timeout go with text, id or label; matches is "
                                    "checked once, as the step ends")
    if "gone" in expect and not isinstance(expect["gone"], bool):
        raise Refused("bad_params", f"step {n}: expect's gone must be true or false")
    if "value" in expect:
        if not given or given[0] == "text" or not isinstance(expect["value"], str):
            raise Refused("bad_params", f"step {n}: expect's value goes with id or label, and is a string "
                                        '(a switch: "on" or "off")')
        if expect.get("gone"):
            raise Refused("bad_params", f"step {n}: expect can't have both value and gone")
    timeout = expect.get("timeout", EXPECT_DEFAULT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not EXPECT_MIN <= timeout <= EXPECT_MAX:
        raise Refused("bad_params", f"step {n}: expect's timeout must be from {EXPECT_MIN:g} to {EXPECT_MAX:g} s")


def args_of(step: dict) -> dict:
    """The tool call a step makes: its parameters without `tool` and `expect`, and without an answer of its own
    (the batch answers once)."""
    args = {k: v for k, v in step.items() if k not in ("tool", "expect", "route", "secure", "look", "screenshot")}
    if step["tool"] not in ("close_app", "performance"):  # (a measurement has no list to show)
        args["look"] = False
    return args


def summary(step: dict) -> str:
    """How an answer names a step: its tool and its target as the agent wrote it (`tap "Save"`, `tap id=save`,
    `type_text "Email"`, `scroll down`, `wait 1.0`)."""
    tool, target = step["tool"], step.get("target")
    if isinstance(target, dict):
        if target.get("ref"):
            aim = str(target["ref"])
        elif target.get("id"):
            aim = f"id={target['id']}"
        elif target.get("label") or target.get("text"):
            aim = json.dumps(target.get("label") or target.get("text"), ensure_ascii=False)
        elif target.get("x") is not None and target.get("y") is not None:
            aim = f"({target['x']:g}, {target['y']:g})"
        else:
            aim = ""
        return f"{tool} {aim}".strip()
    extra = {"scroll": step.get("to") or step.get("direction") or "down", "wait": step.get("seconds"),
             "open_app": step.get("app"), "close_app": step.get("app"), "open_url": step.get("url"),
             "alert": step.get("action"), "press_button": step.get("name"), "rotate": step.get("to"),
             "performance": "launch" if step.get("launch") else step.get("seconds"),
             "compare_screen": json.dumps(step["name"], ensure_ascii=False) if isinstance(step.get("name"), str) else None,
             "wait_for": next((json.dumps(step[k], ensure_ascii=False) for k in EXPECT_KINDS if step.get(k)), None)}
    shown = extra.get(tool)
    return f"{tool} {shown}" if shown is not None else tool


def expect_failed(expect: dict) -> str:
    """The reason a text, id or label expectation failed, as the answer says it."""
    kind = next(k for k in EXPECT_KINDS if expect.get(k) is not None)
    what = f"{kind} {json.dumps(expect[kind], ensure_ascii=False)}"
    seconds = f"{float(expect.get('timeout', EXPECT_DEFAULT)):g}"
    if "value" in expect:
        return f"{what} didn't have the value {json.dumps(expect['value'], ensure_ascii=False)} within {seconds} s."
    return f"{what} didn't {'disappear' if expect.get('gone') else 'appear'} within {seconds} s."


def value_of(element: ui.Element) -> str:
    """An element's value as `expect` compares it: a switch is "on" or "off"."""
    if element.checked is not None:
        return "on" if element.checked else "off"
    return element.value


def has_value(element: ui.Element, want: str) -> bool:
    """Does the element hold the expected value? A switch also takes "true" and "false", as its line shows it."""
    if element.checked is not None:
        want = {"true": "on", "false": "off"}.get(want.strip().lower(), want.strip().lower())
    return value_of(element) == want


def load_script(path: str) -> dict:
    """A script file (§9.4), checked."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise Refused("bad_params", f"can't read the script {path}: {e.strerror or e}") from None
    except ValueError as e:
        raise Refused("bad_params", f"the script {path} isn't valid JSON: {e}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise Refused("bad_params", f'the script {path} isn\'t an agent-tools script (its "format" must be "{FORMAT}")')
    if data.get("version") != VERSION:
        raise Refused("bad_params", f"the script {path} is version {data.get('version')!r}; this tool reads version "
                                    f"{VERSION}")
    if data.get("app") is not None and not isinstance(data["app"], str):
        raise Refused("bad_params", f'the script {path}: "app" must be a bundle id or an app\'s name')
    check_steps(data.get("steps"), script=True)
    return data


def recorded_target(target: dict | None, items: list[ui.Element]) -> dict | None:
    """The target a recording saves (§9.4): the element's id, else its label, else its text, else the point; with
    `index` when that target matches several elements. A ref is turned into the element it names first."""
    if not isinstance(target, dict):
        return None
    if target.get("x") is not None:
        return {"x": target["x"], "y": target["y"]}
    if not target.get("ref"):
        return {k: v for k, v in target.items() if k in ("id", "label", "text", "index") and v is not None}
    try:
        found = ui.resolve(items, ui.parse_target(target))
    except Refused:
        return None
    if not isinstance(found, ui.Element):
        return None
    for kind, value in (("id", found.identifier), ("label", found.label), ("text", found.text)):
        if not value:
            continue
        aim = {kind: value}
        # the candidates are the ones replay's `ui.resolve` picks among, so the saved selector finds this element again
        same = ui.candidates(items, ui.parse_target(aim))
        place = next((n for n, e in enumerate(same) if e is found), None)
        if place is None:
            continue  # this selector never reaches the element (a nested text, a match behind an alert): try the next
        if len(same) > 1:
            aim["index"] = place
        return aim
    x, y = found.center
    return {"x": round(x, 1), "y": round(y, 1)}


def recorded_ignore(ignore: list[dict], items: list[ui.Element]) -> list[dict]:
    """The `ignore` list a recording saves (§14.2): a ref becomes the element's id, label or text (with `index` when it
    matches several), because refs don't outlive a session. A ref whose element has none of the three can't be saved
    (`bad_params`, naming it). Rects and the other targets are kept as given."""
    saved = []
    for entry in ignore:
        if not isinstance(entry, dict) or not entry.get("ref"):
            saved.append(entry)
            continue
        try:
            found = ui.resolve(items, ui.parse_target(entry))
        except Refused:
            found = None
        aim = recorded_target(entry, items) if isinstance(found, ui.Element) else None
        if aim is None or "x" in aim:
            what = found.line(ref=False, actions=False, place=False) if isinstance(found, ui.Element) else entry["ref"]
            raise Refused("bad_params", f"can't record ignore {entry['ref']} ({what}): it has no id, label or text to "
                                        "find it by when the script replays; ignore it by a rect {x, y, width, "
                                        "height} instead")
        saved.append(aim)
    return saved


def scripts_dir() -> Path:
    """Where `record` saves a script by its name, and `replay` finds it: ~/.tapwright/scripts."""
    from .store import HOME
    return HOME / "scripts"


def named(name: str) -> Path:
    """The file of a saved script: its name, made safe for a file name."""
    safe = "".join(c if c.isalnum() or c in "-_ ." else "-" for c in name).strip(" .") or "recording"
    return scripts_dir() / f"{safe}.json"


def saved() -> list[str]:
    """The names of the saved scripts, sorted."""
    folder = scripts_dir()
    return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []


def resolve(name: str | None, path: str | None) -> str:
    """The file to replay: `path` as given, else the saved script called `name`."""
    if path:
        return path
    file = named(name or "")
    if not file.exists():
        known = ", ".join(saved()) or "none yet"
        raise Refused("bad_params", f'no saved script called "{name}" (saved: {known})')
    return str(file)


def writable(path: str) -> Path:
    """Where `record` may save: a `.json` file that doesn't exist yet or is an agent-tools script already. Never
    another kind of file (an agent's recording must not overwrite what isn't a script)."""
    file = Path(path)
    if file.suffix.lower() != ".json":
        raise Refused("bad_params", f"a script is saved as a .json file, not {file.name!r}")
    if file.exists():
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise Refused("bad_params", f"{file.name} exists and isn't an agent-tools script; pick another path")
    return file


def script(name: str, app: str | None, steps: list[dict]) -> dict:
    return {"format": FORMAT, "version": VERSION, "name": name, "app": app, "fresh": True, "steps": steps}
