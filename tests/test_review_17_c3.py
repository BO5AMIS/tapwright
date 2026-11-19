"""Codex's review of PR #12, cycle 3 (H1-H6, M1, M2): each test failed at 66b2ebe and passes after its fix. No phone,
synthetic values only. The last test lists every place a touch reaches the phone and holds each to the one set of final
checks (Daemon._last_checks), so a new way of touching can't skip one."""
import ast
import dataclasses
import inspect
import json
import os
import textwrap

import pytest
from fakes import button, el
from test_daemon import LongList, finish, make, run, started
from test_review_17_c1 import calls_of, chat, daemon_with, grant

from tapwright import daemon as daemon_module
from tapwright import policy, ui
from tapwright.ceiling import Ceiling
from tapwright.control import Refused


# ---- H1: a draft that only comes on screen after a scroll is the draft the person sees and sends ---------------------------
class Composer(LongList):
    """A form whose Post is row 30, below the screen, with a caption input the app rewrites on every scroll."""

    def __init__(self, d) -> None:
        self.caption, self.moves = "Approved draft A", 0
        super().__init__(d, labels=lambda i: "Post" if i == 30 else f"Row {i}")
        inner = self.s.scroll

        async def scroll(x0, y0, x1, y1):
            self.moves += 1
            self.caption = f"Unapproved draft B{self.moves}"
            return await inner(x0, y0, x1, y1)
        self.s.scroll = scroll

    def lay(self) -> None:
        super().lay()
        self.s.tree.append(el(f"e{len(self.s.tree) + 1}", "input", "Caption", "caption", value=self.caption,
                              rect=(10, 20, 380, 30)))




# ---- H2: a recipient held in a control's value is part of the draft's fingerprint ------------------------------------------


# ---- H3: drafts only holds whatever policy the call sends ----------------------------------------------------------------


# ---- H4: deletion and payment ask by the touch's own words and on text that takes a tap ---------------------------------




# ---- H5: a native navigation title is a payment screen's title --------------------------------------------------------------


# ---- H6: a schedule that fails validation never shows a vault value -------------------------------------------------------
@pytest.mark.parametrize("probe", ["caption_in_a_list", "folder_named_by_the_value"])
def test_h6_a_schedule_that_fails_validation_never_writes_a_vault_value(tmp_path, monkeypatch, probe):
    from test_review_17_c1 import AdmissionPhone, admission_deps
    from tapwright import auditlog, schedulerun, store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    email = "synthetic.secret@example.test"
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 2, "vault": {"me": {"kind": "identity", "name": "Tess Tester",
                                                               "email": email}}}), encoding="utf-8")
    phone = AdmissionPhone(0)
    deps, _ = admission_deps(tmp_path, phone)
    data = {"version": 1, "tool": "tapwright", "route": "post_update", "at": "09:00 daily", "accounts": str(vault)}
    if probe == "caption_in_a_list":
        data["with"] = {"caption": [f"@{email}.txt"]}
    else:
        deps = dataclasses.replace(deps, where=tmp_path / email / "sched")
        data["version"] = 9  # malformed: its error names the file's path, which holds the value
    deps.where.mkdir(parents=True, exist_ok=True)
    (deps.where / "post.json").write_text(json.dumps(data), encoding="utf-8")
    out = schedulerun.run("post", deps=deps)
    assert out.status == "failed" and phone.calls == []
    written = "\n".join(auditlog.read_lines(tmp_path / "audit"))
    assert "failed" in written and email not in written and "synthetic.secret" not in written, written
    assert email not in out.text and "synthetic.secret" not in out.text, out.text
    report = (tmp_path / "runs").joinpath(os.path.basename(out.report)).read_text("utf-8")
    assert email not in report and "synthetic.secret" not in report
    # a vault that can't be read: the error's own words are never shown
    (deps.where / "post.json").write_text(json.dumps({**data, "accounts": str(tmp_path / "missing.json")}), "utf-8")
    out = schedulerun.run("post", deps=deps)
    assert out.status == "failed" and email not in out.text and "isn't valid" in out.text


# ---- M1: /otp never swallows a Notification Center that wasn't closed --------------------------------------------------------
def test_m1_otp_answers_internal_when_notification_center_may_still_be_open(tmp_path):
    from starlette.testclient import TestClient
    from test_notifications import BUNDLE, handover_daemon
    from test_otp import AUTH, A, prove
    from tapwright.device import DeviceError

    app, d, s = handover_daemon(tmp_path)
    real_swipe = s.swipe

    async def swipe(x0, y0, x1, y1, *args, **kwargs):
        if y1 < y0:  # the swipe up that closes it fails
            raise DeviceError("HTTP 500: synthetic")
        return await real_swipe(x0, y0, x1, y1, *args, **kwargs)

    async def launch(bundle_id, arguments=None):
        raise DeviceError("HTTP 500: synthetic")
    s.swipe, s.launch = swipe, launch
    c = TestClient(app, client=A)
    prove(c)
    got = c.post("/otp", json={"app": BUNDLE}, headers=AUTH)
    assert got.status_code == 500 and got.json()["error"]["code"] == "internal", got.text
    assert "may still be on the phone's screen" in got.json()["error"]["message"] and "code" not in got.json()
    assert s.center_open


# ---- M2: toggle's tap and type_text's focusing tap go through the hit test --------------------------------------------------


# ---- one path for every touch -------------------------------------------------------------------------------------------------
# Every place in the package that makes a touch on the phone, by (module, function, the session's method), and why it is
# safe. "checked": the function itself runs Daemon._last_checks before the touch. "checked by <f>": the touch is made in a
# helper that only <f> (which runs _last_checks first) calls. The rest touch no element an agent chose:
TOUCHES = {
    ("daemon", "Daemon._alert", "alert"): "checked",
    ("daemon", "Daemon._touch", "_press"): "checked",  # tap, long_press: _last_checks, then _press
    ("daemon", "Daemon._press", "tap"): "checked by Daemon._touch",  # (and a double tap's second: checked inside)
    ("daemon", "Daemon._toggle", "tap"): "checked",
    ("daemon", "Daemon._type_text", "type_text"): "checked",  # its focusing tap (typing itself: §11.1, Return guarded)
    ("daemon", "ACTIONS", "alert"): "checked by Daemon._alert",
    ("daemon", "Daemon._swipe", "ACTIONS"): "checked",  # a short swipe is a touch where it starts; a full one asks
    ("daemon", "ACTIONS", "swipe"): "checked by Daemon._swipe",
    ("daemon", "ACTIONS", "press_button"): "hardware button: home, volume, lock (no element)",
    ("daemon", "Daemon._scroll", "scroll"): "a drag through a scroll view (activates nothing)",
    ("daemon", "Daemon._scroll_to", "scroll"): "a drag through a scroll view (activates nothing)",
    ("daemon", "Daemon._dismiss_keyboard_now", "tap"): "a blank spot no element holds, to close the keyboard",
    ("daemon", "Daemon._dismiss_keyboard_now", "swipe"): "a drag on the content, to close the keyboard",
    ("daemon", "Daemon._press_done", "key"): "a backspace taking back the new line a Done key typed into a multiline field",
    ("daemon", "Daemon._press_done", "tap"): "an input accessory's or toolbar's Done button just above the keyboard",
    ("daemon", "Daemon._back", "tap"): "the navigation bar's back button, or outside a popover: leaving",
    ("daemon", "Daemon._back", "swipe"): "the edge swipe back: leaving",
    ("daemon", "Daemon._close_sheet", "tap"): "beside a sheet, to close it: leaving",
    ("daemon", "Daemon._close_sheet", "swipe"): "a sheet pulled down: leaving",
    ("daemon", "Daemon._person_action", "tap"): "a person's own input from the live view",
    ("daemon", "Daemon._person_action", "path"): "a person's own input from the live view",
    ("daemon", "Daemon._person_action", "scroll"): "a person's own input from the live view",
    ("daemon", "Daemon._person_action", "type_text"): "a person's own input from the live view",
    ("daemon", "Daemon._person_action", "key"): "a person's own input from the live view",
    ("daemon", "Daemon._person_action", "press_button"): "a person's own input from the live view",
    ("center_tool", "CenterTools._read_center", "swipe"): "Notification Center pulled down (iOS's own screen)",
    ("center_tool", "CenterTools._read_center", "tap"): "a notification of a given app, opened (§24.2)",
    ("center_tool", "CenterTools._close_center", "swipe"): "Notification Center closed",
    ("center_tool", "CenterTools._close_center", "press_button"): "home, to close Notification Center",
    ("mirror", "_open_list", "swipe"): "Control Center, for Screen Mirroring (the tool's own setup of the stream)",
    ("mirror", "_open_list", "press_button"): "home, out of Control Center",
    ("mirror", "_open_list", "tap"): "Control Center's Screen Mirroring button",
    ("mirror", "start", "press_button"): "home, out of Control Center",
    ("mirror", "start", "tap"): "the computer's row in Screen Mirroring",
    ("mirror", "stop", "press_button"): "home, out of Control Center",
    ("mirror", "stop", "tap"): "Control Center's Stop Mirroring",
    ("unlock", "PhoneLockScreen.reveal", "_call"): "the lock screen's swipe up (serve --phone, §18.6)",
    ("unlock", "PhoneLockScreen.press_keys", "_call"): "the lock screen's passcode keys (serve --phone, §18.6)",
    ("unlock", "PhoneLockScreen.type_secret", "_call"): "the lock screen's passcode, typed (serve --phone, §18.6)",
}
PRIMITIVES = {"tap", "swipe", "scroll", "path", "key", "type_text", "press_button", "long_press", "_press", "alert"}
RECEIVERS = {"s", "session"}


def _receiver(func: ast.Attribute) -> bool:
    value = func.value
    return (isinstance(value, ast.Name) and value.id in RECEIVERS) or (
        isinstance(value, ast.Attribute) and value.attr == "session") or (
        isinstance(value, ast.Name) and value.id == "self" and func.attr == "_press")


def touch_sites() -> dict[tuple[str, str, str], list[int]]:
    """Every call of a touch primitive in the package's modules (session.py defines them): (module, the function it is in,
    the primitive) to its lines. A raw `_call` of a touch route counts too, and an ACTIONS entry run by a handler."""
    import tapwright
    root = os.path.dirname(tapwright.__file__)
    found: dict[tuple[str, str, str], list[int]] = {}
    for file in sorted(os.listdir(root)):
        if not file.endswith(".py") or file == "session.py":
            continue
        module = file[:-3]
        tree = ast.parse(open(os.path.join(root, file), encoding="utf-8").read())

        classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}

        def visit(node, where: str) -> None:
            for child in ast.iter_child_nodes(node):
                inner = where
                if isinstance(child, ast.ClassDef):
                    inner = child.name
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # a method is Class.method; a function nested in one (a closure, a lambda's home) is its outer one's
                    inner = f"{where}.{child.name}" if where in classes else (where or child.name)
                elif isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ACTIONS"
                                                           for t in child.targets):
                    inner = "ACTIONS"
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    name = child.func.attr
                    if name in PRIMITIVES and _receiver(child.func):
                        found.setdefault((module, where, name), []).append(child.lineno)
                    elif name == "_call" and child.args and isinstance(child.args[0], ast.Constant)                             and child.args[0].value == "POST" and len(child.args) > 1                             and isinstance(child.args[1], ast.Constant) and any(
                                r in str(child.args[1].value) for r in ("/actions", "/wda/keys", "/wda/tw/tap",
                                                                       "/wda/tw/drag")):
                        found.setdefault((module, where, "_call"), []).append(child.lineno)
                if isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id == "ACTIONS"                         and where.split(".")[-1] in ("_alert", "_swipe"):
                    found.setdefault((module, where, "ACTIONS"), []).append(child.lineno)
                visit(child, inner)
        visit(tree, "")
    return found


def _calls_last_checks_before(function, line: int) -> bool:
    source = textwrap.dedent(inspect.getsource(function))
    first = inspect.getsourcelines(function)[1]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_last_checks":
            if first + node.lineno - 1 < line:
                return True
    return False


def test_every_touch_site_is_listed_and_every_agent_touch_runs_the_last_checks_first():
    sites = touch_sites()
    assert set(sites) == set(TOUCHES), (
        f"touch sites not listed (classify each in TOUCHES, and run Daemon._last_checks before an agent's touch on an "
        f"element): {sorted(set(sites) - set(TOUCHES))}; listed but gone: {sorted(set(TOUCHES) - set(sites))}")
    for (module, where, primitive), why in TOUCHES.items():
        if why == "checked":
            function = getattr(daemon_module.Daemon, where.split(".")[-1])
            for line in sites[(module, where, primitive)]:
                assert _calls_last_checks_before(function, line), f"{where} touches at line {line} before _last_checks"
        elif why.startswith("checked by "):
            caller = getattr(daemon_module.Daemon, why.removeprefix("checked by ").split(".")[-1])
            assert "_last_checks" in inspect.getsource(caller), f"{caller.__name__} doesn't run _last_checks"
    # what each tool's handler is: the checked functions above. Runs, drives, routes, replays and reflexes call tools
    # through run_tool, so they reach these handlers and nothing else
    handlers = make()._handlers
    for tool, function in (("tap", "_tap"), ("long_press", "_long_press"), ("toggle", "_toggle"),
                           ("type_text", "_type_text"), ("alert", "_alert"), ("swipe", "_swipe")):
        assert handlers[tool].__func__ is getattr(daemon_module.Daemon, function)
    assert "_touch" in inspect.getsource(daemon_module.Daemon._tap)
    assert "_touch" in inspect.getsource(daemon_module.Daemon._long_press)
