"""`brief` (AGENT-TOOLS 1.13 §13.4): a shorter answer for an agent that knows its way around."""
import asyncio

import pytest

from tapwright import brief, client, mcp_server, params, ui
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from fakes import button, el
from test_daemon import finish, make, started, unsettled


def run(go):
    asyncio.run(go())


def rows(n: int, text=lambda i: f"Row {i}", first: int = 2) -> list[ui.Element]:
    """n text rows with identifiers, so a changed text is a `~` line and not a gone and a new element."""
    return [el(f"e{first + i}", "text", text(i), f"row{i}", rect=(10, 100 + 24 * i, 300, 20)) for i in range(n)]


SAVE = button("e1", "Save", "save", rect=(10, 100, 100, 40))
MORE = "…{n} more: look shows them."


# ---- the cut itself (pure) ----

def test_a_change_block_of_twelve_lines_or_fewer_is_whole():
    block = ["Same screen. Changes:", *(f'~ e{i} text "x" (was "y")' for i in range(12))]
    assert brief.cut(block) == block
    assert brief.cut(["Same screen. Changes:", "No visible change."]) == ["Same screen. Changes:", "No visible change."]


def test_a_longer_block_is_cut_to_twelve_lines_and_says_how_many_more():
    block = ["Same screen. Changes:", *(f'~ e{i} text "x" (was "y")' for i in range(15))]
    cut = brief.cut(block)
    assert cut == [*block[:13], MORE.format(n=3)]


def test_a_cut_keeps_new_text_lines_ahead_of_changed_and_moved_ones():
    changed = [f'~ e{i} text "x" (was "y")' for i in range(12)]
    new = ['+ e90 text "Wrong password" @10,300 200x20', '+ e91 header "Sign in" @10,60 200x30']
    block = ["Same screen. Changes:", *changed, "↕ 7 elements moved", *new]
    cut = brief.cut(block)
    assert new[0] in cut and new[1] in cut and "↕ 7 elements moved" not in cut
    assert cut[-1] == MORE.format(n=3)
    assert len([ln for ln in cut if ln[0] in "+~↕-"]) == 12
    assert cut[1:-1] == [*changed[:10], *new]  # what survives keeps its order


def test_alert_keyboard_and_network_lines_are_never_cut_and_do_not_count():
    block = ["Alert: \"Delete it?\" [Cancel] [Delete]", "Keyboard: shown", "Network: 3g (Tapwright, whole phone).",
             "Same screen. Changes:", *(f'~ e{i} text "x" (was "y")' for i in range(14))]
    cut = brief.cut(block)
    assert cut[:4] == block[:4] and cut[-1] == MORE.format(n=2) and len(cut) == 4 + 12 + 1


def test_a_new_screen_shows_headers_actionables_and_new_text_only():
    before = [el("e1", "text", "Old words", rect=(10, 100, 100, 20))]
    items = [el("e2", "header", "Welcome", rect=(10, 60, 200, 30)),
             el("e3", "text", "Old words", rect=(10, 100, 100, 20)),
             el("e4", "text", "Wrong password", rect=(10, 130, 200, 20)),
             el("e5", "image", "", "logo", rect=(10, 160, 50, 50)),
             el("e6", "cell", "Settings row", rect=(10, 220, 300, 44)),
             button("e7", "Log in", "login", rect=(10, 300, 100, 40)),
             el("e8", "input", "Email", "email", rect=(10, 360, 300, 44)),
             el("e9", "switch", "Remember me", "remember", rect=(10, 420, 300, 44), checked=False),
             el("e10", "link", "Forgot password", rect=(10, 480, 200, 20)),
             el("e11", "tab", "Home", rect=(10, 740, 80, 50))]
    lines = brief.screen(items, before)
    assert [ln.split(" ")[0] for ln in lines] == ["e4", "e2", "e6", "e7", "e8", "e9", "e10", "e11"]  # new text, header, actions (a list row is one)
    assert all("Old words" not in ln and "logo" not in ln for ln in lines)


def test_a_new_screen_is_cut_at_twelve_lines_new_text_first():
    items = [*(button(f"e{i}", f"Button {i}", f"b{i}", rect=(10, 100 + 50 * i, 100, 40)) for i in range(1, 15)),
             el("e20", "text", "Card declined", rect=(10, 20, 200, 20))]
    lines = brief.screen(items, [])
    assert len(lines) == 13 and lines[0].startswith('e20 text "Card declined"')
    assert lines[1].startswith('e1 button "Button 1"') and lines[11].startswith('e11 button "Button 11"')
    assert lines[-1] == MORE.format(n=3)


# ---- the daemon ----

async def world(d, extra=()):
    stop, task = await started(d)
    d.session.tree = [SAVE, *rows(40), *extra]
    await d.run_tool("a", "look", {"screenshot": False})
    return stop, task


def changed_rows(n: int, extra=()):
    """The world's screen with its first n rows' texts changed."""
    return [SAVE, *rows(n, lambda i: f"Row {i}!"), *rows(40)[n:], *extra]


def test_brief_cuts_a_long_change_block_and_a_plain_answer_keeps_every_line():
    async def go():
        d = make()
        stop, task = await world(d)
        wrong = el("e90", "text", "Wrong password", rect=(10, 40, 200, 20))
        d.session.tree = changed_rows(14, [wrong])
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "brief": True})
        lines = unsettled(r)
        assert lines[0].startswith("Tapped e1 button") and lines[1] == "Same screen. Changes:"
        assert lines[2].startswith('+ e42 text "Wrong password"')
        assert sum(ln.startswith("~ ") for ln in lines) == 11 and lines[-1] == MORE.format(n=3) and len(lines) == 15
        assert "Settled in" in r["text"]
        # the same change, not brief: every line
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})
        d.session.tree = [SAVE, *rows(40)]
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = changed_rows(14, [wrong])
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert sum(ln.startswith("~ ") for ln in r["text"].splitlines()) == 14 and "more: look" not in r["text"]
        await finish(stop, task)
    run(go)


def test_brief_shows_a_new_screen_as_its_actionable_elements_headers_and_new_text():
    async def go():
        d = make()
        stop, task = await world(d)
        screen = [el("e50", "header", "Welcome", rect=(10, 60, 200, 30)),
                  el("e51", "text", "Wrong password", rect=(10, 100, 200, 20)),
                  el("e52", "text", "Row 3", rect=(10, 130, 200, 20)),  # was on the last screen: not new
                  el("e53", "cell", "Settings row", rect=(10, 160, 300, 44)),
                  *(button(f"e{60 + i}", f"Button {i}", f"b{i}", rect=(10, 220 + 45 * i, 100, 40)) for i in range(14))]
        d.session.tree = screen
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "brief": True})
        lines = unsettled(r)
        assert lines[0].startswith("Tapped e1 button") and lines[1].startswith("Screen 400x800 pt")
        assert lines[2] == "App: Example (com.example.app)"
        shown = lines[3:-1]
        assert [ln.split(" ")[1] for ln in shown] == ["text", "header", "cell"] + ["button"] * 9
        assert 'text "Wrong password"' in shown[0] and 'Row 3' not in r["text"] and "Settings row" in r["text"]
        assert lines[-1] == MORE.format(n=5) and "Settled in" in r["text"]
        await finish(stop, task)
    run(go)


def test_brief_keeps_alerts_the_keyboard_and_every_note():
    async def go():
        d = make()
        stop, task = await world(d)
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})  # a block that covers two actions: a note
        d.session.last_alert = ui.Alert("Delete it?", ("Cancel", "Delete"), (0, 0, 100, 100))
        d.session.last_keyboard = True
        d.session.tree = changed_rows(14)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "force": True, "brief": True})
        lines = unsettled(r)
        assert 'Alert: "Delete it?" [Cancel] [Delete]' in lines and "Keyboard: shown" in lines
        assert sum(ln.startswith("~ ") for ln in lines) == 12
        notes = lines[lines.index(MORE.format(n=2)) + 1:]  # the cut ends the block; every note follows it
        assert "Note: changes are since your last list, 2 actions ago." in notes
        assert any(n.startswith("Note: forced (behind_alert)") for n in notes)
        await finish(stop, task)
    run(go)


def test_look_is_never_brief_and_does_not_take_the_argument():
    async def go():
        plain, briefed = make(), make()
        briefed.ceiling = Ceiling(brief=True)
        texts = []
        for d in (plain, briefed):
            stop, task = await started(d)
            d.session.tree = [SAVE, *rows(30)]
            texts.append((await d.run_tool("a", "look", {"screenshot": False}))["text"])
            await finish(stop, task)
        assert texts[0] == texts[1] and "more: look" not in texts[0]
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {"brief": True})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_the_server_default_applies_and_an_argument_overrides_it():
    async def go():
        d = make()
        d.ceiling = Ceiling(brief=True)
        stop, task = await world(d)
        d.session.tree = changed_rows(14)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert MORE.format(n=2) in r["text"]
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})
        d.session.tree = [SAVE, *rows(40)]
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = changed_rows(14)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "brief": False})
        assert "more: look" not in r["text"] and sum(ln.startswith("~ ") for ln in r["text"].splitlines()) == 14
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "save"}, "brief": "yes"})
        assert e.value.code == "bad_params" and "brief must be true or false" in str(e.value)
        await finish(stop, task)
    run(go)


def test_run_takes_brief_and_a_failing_run_is_cut_the_same_way():
    async def go():
        d = make()
        stop, task = await world(d)
        d.session.tree = changed_rows(14)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "save"}}], "brief": True})
        lines = unsettled(r)
        assert lines[0].startswith("Ran 1 of 1 steps") and lines[-1] == MORE.format(n=2) and len(lines) == 15
        d.session.tree = [SAVE, *rows(40)]
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = changed_rows(14)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"brief": True, "steps": [
                {"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved", "timeout": 0.5}}]})
        text = str(e.value)
        assert e.value.code == "expect_failed" and text.startswith("Ran 0 of 1 steps; stopped at step 1")
        assert MORE.format(n=2) in text
        await finish(stop, task)
    run(go)


# ---- the tools, the table and the flags ----

def test_brief_is_a_parameter_of_every_action_tool_and_of_run_but_not_of_look():
    tools = {t.name: set(t.parameters["properties"]) for t in mcp_server.server._tool_manager.list_tools()}
    acting = {"tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "open_url", "wait", "wait_for",
              "open_app", "alert", "press_button", "rotate", "run"}
    for name in acting:
        assert "brief" in tools[name] and "brief" in params.ALLOWED[name], name
    assert "brief" not in tools["look"] and "brief" not in params.ALLOWED["look"]


def test_the_mcp_server_sends_brief_by_default_when_started_with_it(monkeypatch):
    sent = []

    def fake_call(tool, args, client_id, timeout=120.0):
        sent.append((tool, dict(args)))
        return {"text": "ok", "image": None}
    monkeypatch.setattr(client, "call", fake_call)

    async def go():
        monkeypatch.setattr(mcp_server, "BRIEF_DEFAULT", False)
        await mcp_server.tap(target=mcp_server.Target(id="x"))
        monkeypatch.setattr(mcp_server, "BRIEF_DEFAULT", True)
        await mcp_server.tap(target=mcp_server.Target(id="x"))
        await mcp_server.tap(target=mcp_server.Target(id="x"), brief=False)
        await mcp_server.look()
        await mcp_server.run_steps(steps=[{"tool": "wait", "seconds": 1}])
        await mcp_server.tap(target=mcp_server.Target(id="x"), brief=True)
    run(go)
    assert "brief" not in sent[0][1]
    assert sent[1][1]["brief"] is True and sent[2][1]["brief"] is False
    assert sent[3][0] == "look" and "brief" not in sent[3][1]
    assert sent[4][0] == "run" and sent[4][1]["brief"] is True and sent[5][1]["brief"] is True


def test_the_flag_is_on_mcp_and_serve(monkeypatch):
    from tapwright import cli, device, server
    got: dict = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: got.update(kw))
    monkeypatch.setattr("sys.argv", ["tapwright", "mcp", "--apps", "com.a", "--brief"])
    assert cli.main() == 0 and got["brief"] is True
    monkeypatch.setattr("sys.argv", ["tapwright", "mcp", "--apps", "com.a"])
    cli.main()
    assert got["brief"] is False
    served: dict = {}
    monkeypatch.setattr(server, "serve", lambda **kw: served.update(kw) or 0)
    monkeypatch.setattr(device, "run", lambda fn, *a: fn(*a))
    monkeypatch.setattr("sys.argv", ["tapwright", "serve", "--brief"])
    cli.main()
    assert served["ceiling"].brief is True and served["ceiling"].is_set
    assert Ceiling().brief is False and not Ceiling().is_set


def test_a_list_screen_in_brief_shows_its_rows_radios_and_checkboxes():
    """AGENT-TOOLS 1.13.1 clarification 1: a list screen shows the rows an agent came to tap, with the header."""
    items = [el("e1", "header", "Destinations", rect=(10, 60, 200, 30)),
             *(el(f"e{2 + i}", "cell", f"Destination {i}", rect=(0, 100 + 60 * i, 400, 56)) for i in range(5)),
             el("e7", "radio", "Window seat", rect=(0, 420, 400, 44)), el("e8", "checkbox", "Extra bag", rect=(0, 470, 400, 44))]
    lines = brief.screen(items, [])
    assert [ln.split(" ")[0] for ln in lines] == [f"e{i}" for i in range(1, 9)]
    assert [ln.split(" ")[1] for ln in lines] == ["header", *["cell"] * 5, "radio", "checkbox"]


def test_brief_after_a_tap_into_a_list_screen_lists_the_rows():
    async def go():
        d = make()
        stop, task = await world(d)
        d.session.tree = [el("e50", "header", "Destinations", rect=(10, 60, 200, 30)),
                          *(el(f"e{51 + i}", "cell", f"Destination {i}", rect=(0, 100 + 60 * i, 400, 56)) for i in range(4))]
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "brief": True})
        lines = unsettled(r)
        assert lines[1].startswith("Screen 400x800 pt"), r["text"]  # a new screen, not a change block
        assert [ln.split(" ")[1] for ln in lines[3:]] == ["header", "cell", "cell", "cell", "cell"], r["text"]
        await finish(stop, task)
    run(go)


def test_a_brief_new_screen_keeps_list_rows():
    """A list screen in brief shows its rows: they are what an agent came to tap (the lead's clarification of §13.4)."""
    from tapwright import brief
    assert "cell" in brief.ACTIONABLE and "radio" in brief.ACTIONABLE and "checkbox" in brief.ACTIONABLE
