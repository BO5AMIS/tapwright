"""What the 1.8.5 phone checks found (W177): a relaunch that shows a loading screen (verify and explore), `bug_report
from:"failure"` through the real daemon route the thin `tapwright call` uses, typed field values in explore files, the
dedupe count, and the page's title, logs, start and wording. No phone, no real ~/.tapwright, no network."""
import asyncio
import datetime
import json
import socket
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from explorefakes import FakePhone, build, identity
from mapfakes import SCREEN, screen
from test_daemon import finish, run, started
from test_daemon_bug import bugged, files_text, folder_of
from test_daemon_explore import BUNDLE, ExploreSession, explored, report_of

from tapwright import bug, bugmake, bugstore, explore, explorereport, findings, reflex, thin, ui
from tapwright.actionlog import ActionLog
from tapwright.server import create_app

TOKEN = "t0ken-w177"
SECRET = "TWTESTSECRET-7731"


def loading_tree(text: str = "Downloading 45.00% (1/2)", bundle: str = BUNDLE) -> ui.Tree:
    """A development build's first screen: a blank root with Metro's banner, nothing to touch."""
    def node(kind, **attrs):
        return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                    **{k: str(v) for k, v in attrs.items()}})
    app = node("Application", bundleId=bundle, label="Example", x=0, y=0, width=400, height=800)
    app.append(node("StaticText", label=text, name=text, value=text, x=0, y=40, width=400, height=20))
    return ui.read(ET.tostring(app, encoding="unicode"), SCREEN)


class LoadingSession(ExploreSession):
    """After each launch the app shows its loading screen for `loading_reads` reads; a link opened then is lost."""
    loading_reads = 0
    left = 0

    async def launch(self, bundle_id, arguments=None):
        text = await super().launch(bundle_id, arguments)
        self.left = self.loading_reads
        return text

    def _show(self):
        if self.left > 0:
            self.left -= 1
            tree = loading_tree()
            self.items, self.last_tree = list(tree.items), tree
            self.foreground, self.app = BUNDLE, tree.app
            self.last_alert, self.last_keyboard = None, False
            return tree
        return super()._show()

    async def open_url(self, url):
        if self.left > 0:
            self.left -= 1
            return f"Opened {url}."  # (lost: the app is still loading)
        return await super().open_url(url)


def loading(d, reads: int) -> None:
    d.session.__class__ = LoadingSession
    d.session.loading_reads = reads


async def serving(d, home):
    import uvicorn
    app = create_app(d, TOKEN)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    (home / "serve.json").write_text(json.dumps({"port": sock.getsockname()[1], "token": TOKEN}))
    return server, task, sock


async def call(capsys, *argv) -> tuple[int, str, str]:
    code = await asyncio.to_thread(thin.main, list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TAPWRIGHT_APPS", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


FAILING = [{"tool": "tap", "target": {"id": "go-settings"}, "expect": {"text": "Never on this screen", "timeout": 0.5}}]


def test_from_failure_through_the_thin_call(tmp_path, home, capsys):
    """The phone check's way: each `tapwright call` is a process of its own, and its default client is its parent process
    (a new shell for each command), so the `run` and the `bug_report` after it come from two client ids. The failure the
    run left must still be found: a thin call's last run is the thin calls' last run."""
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        server, serve_task, sock = await serving(d, home)
        try:
            code, out, err = await call(capsys, "look", "{}", "--client", "4101")
            assert code == 0, err
            code, out, err = await call(capsys, "run", json.dumps({"steps": FAILING}), "--client", "4102")
            assert code == 2 and "expect_failed" in err, (out, err)
            code, out, err = await call(capsys, "bug_report", '{"from": "failure", "verify": false}', "--client", "4103")
            assert code == 0, err
            assert out.startswith("New bug: Check failed on "), out
            # a later thin run that ends well clears it, for every thin caller (only the last one counts, §29.3)
            code, out, err = await call(capsys, "run", json.dumps({"steps": [{"tool": "back"}]}), "--client", "4104")
            assert code == 0, err
            code, out, err = await call(capsys, "bug_report", '{"from": "failure", "verify": false}', "--client", "4105")
            assert code == 2 and "not_found" in err, (out, err)
        finally:
            server.should_exit = True
            await serve_task
            sock.close()
            await finish(stop, task)
    run(go)


def test_an_mcp_client_never_takes_another_clients_failure(tmp_path):
    async def go():
        from tapwright.control import Refused
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Refused):
            await d.run_tool("a", "run", {"steps": FAILING})
        with pytest.raises(Refused) as e:
            await d.run_tool("b", "bug_report", {"from": "failure", "verify": False})
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_a_failure_that_cant_be_kept_is_logged_not_swallowed(tmp_path):
    async def go():
        from tapwright.control import Refused
        logged: list[str] = []
        d = bugged(tmp_path)
        d.log = logged.append
        stop, task = await started(d)
        await d.run_tool("a", "look", {})

        def broken(*a, **k):
            raise RuntimeError("no build")
        d._explore_identity = broken
        with pytest.raises(Refused):
            await d.run_tool("a", "run", {"steps": FAILING})
        assert any("from" in line and "RuntimeError" in line for line in logged), logged
        await finish(stop, task)
    run(go)


# ---- 1. a relaunch that shows a loading screen ----

def test_starting_tells_a_loading_screen_from_a_screen_of_the_app():
    assert reflex.starting(loading_tree(), BUNDLE) == "loading"
    assert reflex.starting(loading_tree("Loading from Metro..."), BUNDLE) == "loading"
    assert reflex.starting(loading_tree("Welcome"), BUNDLE) == "empty"
    assert reflex.starting(loading_tree(bundle="com.apple.springboard"), BUNDLE) == "elsewhere"
    assert reflex.starting(None, BUNDLE) == "elsewhere"
    home = screen("Home", ids=("go-a", "go-b", "go-c"), texts=("Downloading 3 files",), bundle=BUNDLE)
    assert reflex.starting(home, BUNDLE) is None  # (words on a real screen with things to touch hold nothing up)


def test_verify_waits_for_a_relaunch_that_shows_a_loading_screen(tmp_path):
    """The phone check: every verify was "not checked (no reset reached a known place)" on a development build."""
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Exception):
            await d.run_tool("a", "run", {"steps": FAILING})
        loading(d, 6)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": 2})
        assert r["text"].splitlines()[1] == "Reproduced: 2 of 2 runs.", r["text"]
        await finish(stop, task)
    run(go)


def test_verify_says_when_the_app_was_still_loading_at_the_deadline(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Exception):
            await d.run_tool("a", "run", {"steps": FAILING})
        loading(d, 10 ** 6)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": 1})
        assert r["text"].splitlines()[1] == ("Reproduced: not checked (no reset reached a known place; the app was still "
                                             "loading after 30 s)."), r["text"]
        await finish(stop, task)
    run(go)


def test_the_reach_after_a_reset_is_tried_once_more_when_the_place_isnt_reached():
    from tapwright.bugcheck import DaemonCheck

    async def go():
        port = DaemonCheck.__new__(DaemonCheck)
        tries, waits, at = [], [], iter([False, True])

        async def reach_once(place):
            tries.append(place)
            return True

        async def where(place):
            return next(at)

        async def launched():
            waits.append(1)
            return True
        port._reach_once, port._at, port._launched = reach_once, where, launched
        assert await port.reach("place_31") is True
        assert tries == ["place_31", "place_31"] and waits == [1]
    asyncio.run(go())


def test_explore_started_on_a_loading_screen_waits_for_the_app(tmp_path):
    """Seen on 1.8.0: explore started on the loading screen answered "0 places, frontier empty" in 2 s."""
    async def go():
        d = explored(tmp_path)
        loading(d, 0)
        d.session.left = 8
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {"minutes": 1})
        body = json.loads((report_of(r) / "report.json").read_text("utf-8"))
        assert body["coverage"]["places"] >= 3, r["text"]
        await finish(stop, task)
    run(go)


# ---- 3. typed field values in explore files ----

def element_issue(n: int, rule: str, role: str, name: str, ident: str) -> str:
    return f'{n}. {rule}: e15 {role} "{name}" id={ident} value="{SECRET}" @21,607 388x47: it fails. Fix: fix it.'


def test_explore_reports_and_bugs_keep_no_typed_field_value(tmp_path):
    screens = {"home": screen("Home", ids=("go-form",)), "form": screen("Form", ids=("b-save",), back="Home")}
    phone = FakePhone(screens, "home", {("home", "go-form"): "form"}, {"form": "home"})
    phone.audits["home"] = (element_issue(1, "contrast", "input", "Bug note", "bug-note"),)
    phone.uxes["form"] = (f'1. dead_end: e3 input "Search" value="{SECRET}": a field with nothing after it (no next step)',)
    explorer, maps = build(tmp_path / "maps", phone)
    result = asyncio.run(explorer.run())
    when = datetime.datetime(2026, 11, 19, 2, 47, tzinfo=datetime.timezone.utc)
    home = tmp_path / "explore"
    folder = explorereport.write(home, result, identity(), explore.Budget(), when, when, lambda s: s)
    text = files_text(folder)
    assert SECRET not in text and findings.FIELD_VALUE in text
    report = json.loads((folder / "report.json").read_text("utf-8"))
    kinds = {f["kind"]: f for f in report["findings"]}
    assert {"a11y", "ux"} <= set(kinds)
    from test_bugstore import ctx_for, store_with
    ctx = ctx_for(tmp_path, keep=store_with("not-this-one"))
    titles = {}
    for kind in ("a11y", "ux"):
        got = bugmake.read_run(home, folder, kinds[kind]["id"])
        made = bugmake.from_run(ctx, got, with_picture=False, reproduced={"checked": False, "why": "not asked"}, named=True)
        assert SECRET not in files_text(made.folder)
        titles[kind] = made.title
    assert titles["a11y"].endswith(": Contrast: id bug-note"), titles
    assert titles["ux"].endswith(': Dead end: input "Search"'), titles


def test_hide_values_hides_every_text_field_value_and_nothing_else():
    line = ('e4 input "Password" id=pw value="hunter2" @1,2 3x4\n'
            'e5 input "Search" value="caf\\"e" placeholder="Find"\n'
            '2. contrast: e6 input "Note" id=n value="TWTESTSECRET-7731…(+12)" @1,2 3x4: x\n'
            'e7 slider "Volume" value="50%"\ne8 input "Empty" id=e placeholder="Note"')
    out = findings.hide_values(line)
    assert "hunter2" not in out and 'caf\\"e' not in out and "TWTESTSECRET" not in out
    assert 'e7 slider "Volume" value="50%"' in out and 'placeholder="Note"' in out and 'placeholder="Find"' in out
    assert out.count(findings.FIELD_VALUE) == 3


# ---- 4. the dedupe count ----

def test_two_runs_make_count_two_whatever_each_runs_own_count(tmp_path):
    from test_bugstore import APP, ctx_for, write_run
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path)
    for run_name in ("20261119T024013Z", "20261119T024702Z"):
        folder = write_run(home, run_name, kind="dead_end", got="no way back to place_4 except a reset", picture=False)
        data = json.loads((folder / "report.json").read_text("utf-8"))
        data["findings"][0]["count"] = 2  # (this run saw it twice: "x2")
        (folder / "report.json").write_text(json.dumps(data), "utf-8")
        made = bugmake.from_run(ctx, bugmake.read_run(home, folder, "f3"), with_picture=False,
                                reproduced={"checked": False, "why": "not asked"}, named=True)
    seen = made.meta["seen"]
    assert (seen["count"], seen["occurrences"]) == (2, 4) and len(seen["sources"]) == 2
    page = (made.folder / "report.md").read_text("utf-8")
    assert "2 times; first" in page and "It showed 4 times in all" in page
    assert bugmake.answer(made, ctx.scrub, home=tmp_path).splitlines()[2].startswith("Seen: 2 times, ")
    assert made.folder.parent == tmp_path / "bugs" / APP


# ---- 5. the page ----

def test_a11y_and_ux_titles_are_the_rule_and_its_target():
    assert bug.issue_title(element_issue(1, "contrast", "input", "Bug note", "bug-note")) == "Contrast: id bug-note"
    assert bug.issue_title('1. dead_end: e3 text "Nothing here leads anywhere at all, ever": x (y)') == \
        'Dead end: text "Nothing here leads anywhere a…"'
    assert bug.issue_title("1. clipped_text: the screen: x") == "Clipped text: the screen"
    assert bug.issue_title("not an issue line") is None
    assert bug.title("a11y", "place_31", element_issue(1, "contrast", "input", "N", "bug-note")) == \
        "Accessibility on place_31: Contrast: id bug-note"
    assert bug.title("js_error", "place_31", "BUGLAB planted js error") == "JS error on place_31: BUGLAB planted js error"


def test_from_now_logs_are_the_apps_own_lines(tmp_path):
    from tapwright import diagnostics, device as devicelib
    assert devicelib.own_line("/private/var/containers/Bundle/Application/A1/Wayfare.app/Frameworks/hermes.framework/hermes",
                              "/var/containers/Bundle/Application/A1/Wayfare.app/Wayfare")
    assert not devicelib.own_line("/System/Developer/Library/PrivateFrameworks/XCTAutomationSupport.framework/X",
                                  "/var/containers/Bundle/Application/A1/Wayfare.app/Wayfare")
    assert devicelib.own_line("", "")  # (the phone didn't say: the line counts as the app's)

    async def go():
        d = bugged(tmp_path)
        ring = diagnostics.LogRing()
        ring.add(diagnostics.LogEntry(1_700_000_000_000, "info", "XCTPerformOnMainRunLoop[MT]: inside CFRunLoopPerform", False))
        ring.add(diagnostics.LogEntry(1_700_000_000_001, "error", "BUGLAB planted js error", True))
        d._rings[BUNDLE] = ring
        d.ensure_logs = lambda bundle: None

        async def refreshed(bundle):
            return None
        d._refresh_logs = refreshed
        lines = await d._bug_logs(BUNDLE)
        assert len(lines) == 1 and lines[0].endswith("error BUGLAB planted js error"), lines
    run(go)


def test_from_now_starts_on_the_place_before_the_first_step_and_says_a_link_once():
    trail = [{"tool": "open_url", "ok": True, "app": BUNDLE, "link": "wayfare://buglab", "place": None, "after": "place_31"},
             {"tool": "open_url", "ok": True, "app": BUNDLE, "link": "wayfare://buglab", "place": None, "after": "place_31"},
             {"tool": "tap", "ok": True, "app": BUNDLE, "target": {"id": "bug-flaky"}, "place": None, "after": "place_31"}]
    seen = bugmake.observed_path([{"tool": "tap", "ok": True, "app": BUNDLE, "target": {"point": True}, "place": None,
                                   "after": "place_12"}, *trail], BUNDLE, "place_31")
    assert seen.path_from == "place_12" and seen.cut
    assert [s["tool"] for s in seen.path] == ["open_url", "tap"]


def test_from_now_through_thin_calls_starts_on_the_place_the_last_read_left(tmp_path, home, capsys):
    """Each thin call its own client (its parent process): the page said "The screen it started on isn't known.\""""
    async def go():
        d = bugged(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        server, serve_task, sock = await serving(d, home)
        try:
            assert (await call(capsys, "look", "{}", "--client", "5101"))[0] == 0
            assert (await call(capsys, "tap", '{"target": {"id": "go-form"}}', "--client", "5102"))[0] == 0
            code, out, err = await call(capsys, "bug_report", '{"from": "now", "note": "looks wrong"}', "--client", "5103")
            assert code == 0, err
            page = (Path(out.splitlines()[3].removeprefix("Folder: ").replace("~", str(Path.home()), 1)) / "report.md")
            text = page.read_text("utf-8")
            assert "\nStart on place_" in text and "isn't known" not in text, text
        finally:
            server.should_exit = True
            await serve_task
            sock.close()
            await finish(stop, task)
    run(go)


def test_not_checked_without_a_reason_when_no_check_was_asked():
    meta = {"reproduced": {"checked": False, "why": "not asked"}}
    assert bug.reproduced_lines(meta, "js_error") == ["not checked"]
    meta = {"reproduced": {"checked": False, "why": "no reset reached a known place"}}
    assert bug.reproduced_lines(meta, "js_error") == ["not checked (no reset reached a known place)"]
