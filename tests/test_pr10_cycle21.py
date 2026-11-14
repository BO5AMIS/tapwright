"""Codex, PR #10 cycle 21: a placeholder already in a text no longer makes every overlapping occurrence of a value be
listed one by one (they are merged as found, and only the ones near a placeholder are looked at alone); boundary fusion
is a best effort, outside the guarantee (the docstring threat model)."""
import bisect
import json
import random
import time

import pytest

from tapwright import accounts as accountlib

# the review's file: 32 accounts, 64 values of `a` runs, 3 to 66 characters (synthetic)
REVIEW_ACCOUNTS = {f"t{i}": {"username": "a" * (2 * i + 3), "password": "a" * (2 * i + 4)} for i in range(32)}


def review_redaction(tmp_path) -> accountlib.Loaded:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"version": 1, "accounts": REVIEW_ACCOUNTS}), "utf-8")
    loaded = accountlib.load(str(path), git=lambda args, cwd: None)
    assert loaded.problem is None and len(loaded.entries()) == 64
    return loaded


def best(found: accountlib.Loaded, text: str) -> float:
    took = 1e9
    for _ in range(2):
        began = time.perf_counter()
        found.scrub(text)
        took = min(took, time.perf_counter() - began)
    return took


def test_the_review_trace_with_a_placeholder_is_as_fast_as_without(tmp_path):
    found = review_redaction(tmp_path)
    placed = "<t0's username> " + "a" * 64000
    assert found.scrub(placed) == "<t0's username> <t31's password>"
    assert found.scrub("a" * 64000) == "<t31's password>"
    with_placeholder, without = best(found, placed), best(found, "a" * 64000)
    # (6.05 s before, against 0.76 s without: now the same work; ~0.6 s each on the PC this was written on)
    assert with_placeholder < 2 * without + 0.1, (with_placeholder, without)
    assert with_placeholder < 2.0


def test_runs_around_and_between_placeholders_are_redacted_and_the_placeholders_kept(tmp_path):
    found = review_redaction(tmp_path)
    text = "a" * 5000 + "<t3's password>" + "a" * 7 + "<t0's username>" + "a" * 2 + " " + "a" * 9000
    assert found.scrub(text) == ("<t31's password><t3's password><t2's username><t0's username>aa "
                                 "<t31's password>")
    once = found.scrub(text)
    assert found.scrub(once) == once


def test_a_value_crossing_a_placeholder_in_a_run_is_still_redacted_whole():
    # (cycle 12: a value that holds placeholder-shaped text is redacted whole, here inside a run of its overlaps)
    value = "ab<t's username>ab"
    found = accountlib.Redaction((("t", "username", "uuu"), ("t", "password", value)))
    text = "x " + value[:-2] + value + value[2:] + " y"  # overlapping occurrences, all crossing the placeholder
    assert found.scrub(text) == "x <t's password> y"
    assert found.scrub(f"<t's username> {text}") == "<t's username> x <t's password> y"


# ---- the same spans as listing every occurrence (the old way), on random texts with placeholders ----
def listed_one_by_one(found: accountlib.Redaction, text: str) -> set[int]:
    """The characters the occurrences cover, each occurrence looked at alone (merge off), those wholly inside a
    placeholder passed over: what `_spans` did with a placeholder in the text before cycle 21 (without the quotes)."""
    assert found.placeholders is not None
    shown = [(m.start(), m.end()) for m in found.placeholders.finditer(text)]
    starts = [a for a, _ in shown]
    covered: set[int] = set()
    known: list = []
    for level, chain in accountlib._levels(text):
        if level is None:
            for a, b in accountlib._unchecked(text, chain):
                covered.update(range(a, b))
            continue
        for i, j, _, _, lead, trail in found._found(level, chain, known, merge=False):
            a, b = accountlib._back(chain, *level.span(i, j, lead, trail), lead, trail)
            k = bisect.bisect_right(starts, a) - 1
            if not (k >= 0 and b <= shown[k][1]):
                covered.update(range(a, b))
    return covered


def spanned(found: accountlib.Redaction, text: str, monkeypatch: pytest.MonkeyPatch) -> set[int]:
    with monkeypatch.context() as m:
        m.setattr(accountlib, "_quoted", lambda t, a, b: (a, b))
        return {c for a, b, _ in found._spans(text) for c in range(a, b)}


SHAPES = {"a": ["a", "a", "a", "%61", "\\u0061", "\\x61", "%2561"], "b": ["b", "b", "%62", "\\u0062"],
          " ": [" ", "+", "%20"]}


def random_case(rng: random.Random) -> tuple[accountlib.Redaction, str]:
    unit = rng.choice(["a", "ab", "aab", "a b"])
    name = rng.choice(["aaaa", "aaaaaaaa", "ab", "aab"])  # (a placeholder that holds a value's characters)
    extra = rng.choice(["", "", ">", "<", "'s"])  # (a value that crosses a placeholder's edge)
    values = [(name, "password", (unit * 8)[:rng.randint(3, 7)]),
              ("tester", "username", rng.choice([extra + unit * 3, unit * 3 + extra, (unit * 8)[:rng.randint(3, 9)]]))]
    found = accountlib.Redaction(tuple(v for v in values if not accountlib.weak(v[2])))
    labels = [f"<{n}'s {k}>" for n, k, v in values if not accountlib.weak(v)] + [accountlib.UNCHECKED]
    pieces = []
    for _ in range(rng.randint(1, 8)):
        if rng.random() < 0.35:
            pieces.append(rng.choice(labels))
        else:
            run = (unit * 40)[:rng.randint(1, 40)]
            pieces.append("".join(rng.choice(SHAPES.get(c, [c])) + ("‎" if rng.random() < 0.05 else "")
                                  for c in run))
        if rng.random() < 0.2:
            pieces.append("x")
    return found, "".join(pieces)


def test_merged_runs_cover_exactly_what_the_occurrences_one_by_one_cover(monkeypatch):
    rng = random.Random("cycle21-runs")
    split = 0
    real = accountlib._kept

    def counting(*args):
        nonlocal split
        out = real(*args)
        split += out != [(args[3], args[4])]
        return out

    monkeypatch.setattr(accountlib, "_kept", counting)
    for _ in range(1500):
        found, text = random_case(rng)
        if not found.redacts:
            continue
        assert spanned(found, text, monkeypatch) == listed_one_by_one(found, text), text
    assert split > 200  # (runs that met a placeholder and were split around it: the new path was exercised)


PERIODIC = "<x's password><x's password>"  # the placeholder of an account named `x's password><x`: periodic itself
ENCODED = "%3Cx's password>"  # (a period of it with `<` percent-encoded: no placeholder as it is, one run decoded)


@pytest.mark.parametrize("text", [
    ENCODED * 3 + PERIODIC + ENCODED * 6, ENCODED * 3 + PERIODIC, PERIODIC + ENCODED * 6, PERIODIC,
    ENCODED * 2 + PERIODIC + ENCODED * 3 + PERIODIC + ENCODED * 4])
def test_a_long_run_through_a_placeholder_keeps_both_sides(text, monkeypatch):
    # (a decoded run of overlapping occurrences that passes through a placeholder holding one of them whole: the run
    # before it and the run after it, longer than the value, are both redacted; random texts rarely reach this)
    found = accountlib.Redaction((("x's password><x", "password", "<x's password><x"),))
    assert spanned(found, text, monkeypatch) == listed_one_by_one(found, text)
    assert "%3C" not in found.scrub(text)


def test_scrub_is_idempotent_on_random_texts_with_placeholders():
    # (values with no `<`, `>` or `'s`: one built of a placeholder's own characters can match across two placeholders
    # side by side, before and after this change alike)
    rng = random.Random("cycle21-idempotent")
    checked = 0
    for _ in range(500):
        found, text = random_case(rng)
        if not found.redacts or any(c in v for _, _, v in found_values(found) for c in "<>'"):
            continue
        once = found.scrub(text)
        assert found.scrub(once) == once, text
        checked += 1
    assert checked > 200


def found_values(found: accountlib.Redaction) -> list[tuple[str, str, str]]:
    return [(label, "", form) for form, _, label, _, _, _ in found._forms]
