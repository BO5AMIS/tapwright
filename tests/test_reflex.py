"""Reflexes (AGENT-TOOLS §18.7): which declared reflex is due, and how a firing is said. No phone."""
from tapwright import reflex, ui


def item(ref, role="button", label="", identifier="", value="", **kw):
    return ui.Element(ref, role, label, identifier, value, (0, 0, 100, 40), **kw)


RATE = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}, "max": 1}
SCREEN = [item("e1", "text", "Rate Wayfare"), item("e2", "button", "Not now"), item("e3", "button", "Save", "save")]


def test_a_reflex_is_due_when_its_text_label_or_id_is_on_screen():
    assert reflex.due([RATE], SCREEN, "home", {}) == [RATE]
    by_label = {**RATE, "name": "b", "when": {"label": "Rate Wayfare"}}
    by_id = {**RATE, "name": "c", "when": {"id": "save"}}
    assert [r["name"] for r in reflex.due([by_label, by_id], SCREEN, None, {})] == ["b", "c"]
    assert reflex.due([RATE], [item("e3", "button", "Save", "save")], "home", {}) == []


def test_it_fires_only_at_its_place_when_it_has_one():
    here = {**RATE, "at": "home"}
    assert reflex.due([here], SCREEN, "home", {}) == [here]
    assert reflex.due([here], SCREEN, "settings", {}) == []
    assert reflex.due([here], SCREEN, None, {}) == []  # a screen the map doesn't know is no place


def test_it_fires_at_most_max_times_a_call_and_one_by_default():
    assert reflex.due([RATE], SCREEN, "home", {"rate_prompt": 1}) == []
    twice = {**RATE, "max": 2}
    assert reflex.due([twice], SCREEN, "home", {"rate_prompt": 1}) == [twice]
    assert reflex.due([twice], SCREEN, "home", {"rate_prompt": 2}) == []
    assert reflex.due([{k: v for k, v in RATE.items() if k != "max"}], SCREEN, "home", {"rate_prompt": 1}) == []


def test_a_malformed_reflex_is_never_due():
    assert reflex.due([{"name": "x"}, {"name": "y", "when": "Rate", "do": {}}, "text"], SCREEN, "home", {}) == []


def test_the_screens_own_words_are_what_a_policy_looks_at():
    """The `do` target can carry another word on another screen: the element it names now is the one checked (§18.7)."""
    target = reflex.touched(RATE, SCREEN)
    assert target is not None and target.label == "Not now"
    assert reflex.touched(RATE, [item("e3", "button", "Save")]) is None
    assert reflex.touched({"do": {"tool": "back"}}, SCREEN) is None
    assert reflex.word(target) == "Not now"
    assert reflex.word(item("e9", "input", "Email", value="a@b.c")) == "Email"  # an input by its label: its value is data


def test_a_firing_is_said():
    assert reflex.aim(RATE) == '"Not now"'
    assert reflex.said(RATE, "Rate Wayfare", reflex.aim(RATE)) == 'Reflex: closed "Rate Wayfare" (tapped "Not now").'
    long_press = {**RATE, "do": {"tool": "long_press", "target": {"id": "x"}}}
    assert reflex.aim(long_press) == "id=x"
    assert reflex.said(long_press, "Rate Wayfare", reflex.aim(long_press)) == (
        'Reflex: rate_prompt: "Rate Wayfare" was on screen (long-pressed id=x).')
    back = {**RATE, "do": {"tool": "back"}}
    assert reflex.said(back, "Rate Wayfare", None) == 'Reflex: rate_prompt: "Rate Wayfare" was on screen (went back).'


def test_a_loading_indicator_is_what_the_built_in_wait_looks_for():
    spinner = item("e5", "activityindicator", "In progress", type="ActivityIndicator")
    assert reflex.loading([item("e1", "text", "Hi"), spinner])
    assert reflex.loading([item("e6", "progressindicator", "Progress", type="ProgressIndicator")])
    assert not reflex.loading(SCREEN)
    assert not reflex.loading([])


def test_the_built_in_lines():
    assert reflex.waited(1.24) == "Reflex: waited 1.2 s for loading to finish."
    assert reflex.keyboard("dragged the form a little") == "Reflex: closed the keyboard (dragged the form a little)."
