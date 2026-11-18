"""The reproduction check (AGENT-TOOLS 1.16.2 §29.4) over a fake port: n of N, flaky, 0 of N, a step that needs a person
(stopped before any move), a hold the dispatch gate finds (no move of that step), a person first, the kinds a run matches,
and the checks that can't run. No phone."""
import asyncio

import pytest

from tapwright import bug, bugcheck

KEY = {"signature": "c41f9a07d2aa0011"}
OTHER_KEY = {"signature": "ffffffffffffffff"}
CRASH_GOT = "the app crashed: EXC_BAD_ACCESS (SIGSEGV)"
FRAMES = (" 3 * Wayfare renderDays +148",)


def crash_target(steps=None, start="home"):
    sig = bug.signature("crash", KEY, CRASH_GOT, "EXC_BAD_ACCESS (SIGSEGV)", FRAMES)
    steps = steps if steps is not None else [{"tool": "tap", "target": {"id": "trips-tab"}},
                                             {"tool": "tap", "target": {"id": "trip-row-1"}}]
    return bugcheck.Target("crash", sig, KEY, CRASH_GOT, start, steps)


class Port:
    """A phone: `crashes` says, run by run, whether the last step crashes the app."""

    def __init__(self, crashes=(True, True, True), key=KEY, done=None, stop_at=None, resets=None):
        self.crashes = list(crashes)
        self.key, self.done, self.stop_at = key, done or {}, stop_at
        self.resets = list(resets) if resets is not None else None
        self.run = -1
        self.at = 0
        self.moves: list[dict] = []
        self.gates = 0
        self.t = 0.0

    def clock(self):
        return self.t

    async def gate(self):
        self.gates += 1
        if self.stop_at is not None and self.gates >= self.stop_at:
            raise bugcheck.Stopped("paused")

    async def reset(self):
        self.run += 1
        self.at = 0
        return self.resets.pop(0) if self.resets else self.resets is None

    async def reach(self, place):
        return place == "home"

    async def place_key(self):
        return self.key

    def mark(self):
        return 0

    async def step(self, step):
        self.at += 1
        if self.at in self.done:
            return self.done[self.at]
        self.moves.append(step)
        self.t += 1
        return bugcheck.Done()

    async def seen(self, mark):
        last = self.at == 2
        if last and self.run < len(self.crashes) and self.crashes[self.run]:
            return bugcheck.Seen(crash=("EXC_BAD_ACCESS (SIGSEGV)", FRAMES))
        return bugcheck.Seen()

    async def issues(self, kind):
        return ()

    async def has_back(self):
        return True


def check(port, target, runs=3):
    return asyncio.run(bugcheck.check(port, target, target.steps, runs, build="1.5.0 (220)",
                                      when=lambda: "2026-11-14T11:20:02.014Z", scrub=lambda s: s))


def test_a_crash_that_shows_every_run_is_3_of_3_with_the_reset_it_used():
    got = check(Port(), crash_target())
    assert got == {"n": 3, "of": 3, "build": "1.5.0 (220)", "when": "2026-11-14T11:20:02.014Z",
                   "reset": "relaunch only, data kept"}


def test_flaky_and_never_and_kept_as_not_reproduced():
    assert check(Port(crashes=(True, False, False)), crash_target())["n"] == 1
    never = check(Port(crashes=(False, False, False)), crash_target())
    assert (never["n"], never["of"]) == (0, 3)
    lines = bug.reproduced_lines({"reproduced": never}, "crash")
    assert lines[0].startswith("0 of 3 runs: not reproduced")
    assert bug.reproduced_lines({"reproduced": {"n": 1, "of": 3}}, "crash")[0].startswith("1 of 3 runs (flaky)")


def test_another_place_or_another_symbol_is_not_the_same_bug():
    assert check(Port(key=OTHER_KEY), crash_target())["n"] == 0


def test_a_typed_step_or_a_policy_word_stops_before_any_move():
    steps = [{"tool": "tap", "target": {"id": "go-form"}}, {"tool": "type_text", "target": {"id": "email"}, "text": "${typed}"}]
    assert bugcheck.person_step(steps, lambda s: None) == (2, "a typed value")
    held = [{"tool": "tap", "target": {"id": "trips-tab"}}, {"tool": "tap", "target": {"label": "Delete trip"}}]
    word = bugcheck.person_step(held, lambda s: "Delete" if "Delete" in str(s) else None)
    assert word == (2, 'the policy word "Delete"')
    assert bugcheck.needs_person(*word) == {"checked": False, "why": 'step 2 needs a person (the policy word "Delete")'}
    assert bugcheck.person_step(held[:1], lambda s: None) is None


def test_a_hold_the_dispatch_gate_finds_stops_the_check_at_that_step():
    held = bugcheck.Done("not_allowed", 'needs_confirmation: button Remove is on the skip floor (policy word "Remove"); '
                                        "explore and repair touch nothing it holds, and ask nobody.")
    port = Port(done={2: held})
    got = check(port, crash_target())
    assert got == {"checked": False, "why": 'step 2 needs a person (the policy word "Remove")'}
    assert len(port.moves) == 1  # step 1 ran; step 2 was refused at dispatch; nothing after it, no other run


def test_a_person_first_stops_the_check():
    with pytest.raises(bugcheck.Stopped) as e:
        check(Port(stop_at=3), crash_target())
    assert e.value.code == "paused"
    with pytest.raises(bugcheck.Stopped):
        check(Port(done={1: bugcheck.Done("busy", "a person is using the phone")}), crash_target())


def test_a_run_that_couldnt_start_isnt_made_and_none_is_not_checked():
    got = check(Port(resets=[False, True, True]), crash_target())
    assert (got["n"], got["of"]) == (2, 2)
    assert check(Port(), crash_target(start="elsewhere")) == {"checked": False, "why": bugcheck.NO_RESET}


def test_what_cant_be_checked_says_why():
    assert bugcheck.why_not(bugcheck.Target("unreachable", "x", KEY, "g", "home", []), "own") == bugcheck.UNREACHABLE
    assert "app_command reset" in bugcheck.why_not(crash_target(), "app")
    assert bugcheck.why_not(crash_target(start=None), "own") == bugcheck.NO_START
    assert bugcheck.unaimed([{"row": True, "index": 1}]).startswith("step 1 names a row")
    assert bugcheck.unaimed(crash_target().steps) is None


def test_expect_failed_matches_the_same_step_and_observed_the_same_screen():
    steps = [{"tool": "tap", "target": {"id": "a"}}, {"tool": "tap", "target": {"id": "b"}, "expect": {"text": "Saved"}}]
    target = bugcheck.Target("expect_failed", "s", KEY, "x", "home", steps)
    port = Port(crashes=(), done={2: bugcheck.Done("expect_failed", "its action ran, but no \"Saved\"")})
    assert check(port, target, 2)["n"] == 2
    early = Port(crashes=(), done={1: bugcheck.Done("expect_failed", "no")})
    assert check(early, target, 1)["n"] == 0  # another step's check failed: not the same bug
    observed = bugcheck.Target("observed", "s", KEY, "Total is wrong", "home", steps[:1])
    assert check(Port(crashes=()), observed, 2)["n"] == 2
    assert check(Port(crashes=(), key=OTHER_KEY), observed, 2)["n"] == 0


def test_runs_of_and_the_clients_wait():
    assert bugcheck.runs_of(None, 3) == 3 and bugcheck.runs_of(True, 0) == 3
    assert bugcheck.runs_of(False, 3) == 0 and bugcheck.runs_of(0, 3) == 0 and bugcheck.runs_of(7, 0) == 7
    assert bugcheck.timeout_for({"from": "now", "note": "x"}) == 120.0
    assert bugcheck.timeout_for({"finding": "f3"}) > bugcheck.timeout_for({"finding": "f3", "verify": False})
    assert bugcheck.timeout_for({"bug": "b", "export": {"to": "zip"}}) > 120.0
