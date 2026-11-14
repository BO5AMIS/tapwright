"""Codex, PR #10 cycle 20: an escape's boundary candidates are looked up by the exact boundary text and the next
character, not scanned over every value's forms; a file holds at most 32 accounts."""
import json
import random
import time
import urllib.parse

import pytest
from test_pr10_cycle14 import one_char

from tapwright import accounts as accountlib

REVIEW_TRACE = "%E2%80%8E" * 16000 + "x" + "%E2%80%8E" * 16000


def redaction(*values: str) -> accountlib.Redaction:
    return accountlib.Redaction(tuple((f"t{n}", "password", v) for n, v in enumerate(values)))


def write(tmp_path, accounts: dict) -> str:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"version": 1, "accounts": accounts}), "utf-8")
    return str(path)


def best(found: accountlib.Redaction, text: str) -> float:
    found.scrub("warm")
    took = 1e9
    for _ in range(2):
        began = time.perf_counter()
        found.scrub(text)
        took = min(took, time.perf_counter() - began)
    return took


# ---- the cap ----
def test_the_review_file_of_128_accounts_is_refused_at_load_naming_no_value(tmp_path):
    accounts = {f"t{i}": {"password": f"E{i:05d}%4"} for i in range(128)}
    loaded = accountlib.load(write(tmp_path, accounts), git=lambda args, cwd: None)
    assert loaded.problem is not None and "128 accounts" in loaded.problem and "at most 32" in loaded.problem
    assert "E000" not in loaded.problem and loaded.names == () and not loaded.redacts


def test_33_accounts_are_refused_and_32_load(tmp_path):
    accounts = {f"t{i}": {"username": f"user{i}@example.com", "password": f"synthetic-pw-{i}"} for i in range(33)}
    loaded = accountlib.load(write(tmp_path, accounts), git=lambda args, cwd: None)
    assert loaded.problem is not None and "33 accounts" in loaded.problem and "synthetic-pw" not in loaded.problem
    del accounts["t32"]
    loaded = accountlib.load(write(tmp_path, accounts), git=lambda args, cwd: None)
    assert loaded.problem is None and len(loaded.names) == 32 and len(loaded.entries()) == 64


def test_the_phone_entry_with_only_a_passcode_is_not_counted(tmp_path):
    accounts = {f"t{i}": {"password": f"synthetic-pw-{i}"} for i in range(32)}
    accounts["phone"] = {"passcode": "000000"}
    loaded = accountlib.load(write(tmp_path, accounts), git=lambda args, cwd: None)
    assert loaded.problem is None and loaded.passcode == "000000"
    accounts["phone"] = {"passcode": "000000", "password": "synthetic-pw-x"}
    loaded = accountlib.load(write(tmp_path, accounts), git=lambda args, cwd: None)
    assert loaded.problem is not None and "33 accounts" in loaded.problem


# ---- the timing ----
def test_32_accounts_shaped_like_the_review_on_its_trace_stay_fast():
    """The review: 128 such passwords on this line took 11.72 s (32: 2.4 s). Indexed by the boundary text and the next
    character, measured about 0.08 s."""
    found = redaction(*(f"E{i:05d}%4" for i in range(32)))
    assert best(found, REVIEW_TRACE) < 0.6
    assert best(found, REVIEW_TRACE.replace("x", "0")) < 0.6


def test_32_values_sharing_a_prefix_against_escapes_that_make_it_stay_fast():
    """Every escape makes the values' first character and the level goes on with their second: each escape meets all
    32 forms (measured 1.6 s before, about 0.2 s now)."""
    found = redaction(*(f"1{i:05d}%4" for i in range(32)))
    assert best(found, "%F0%9F%98%80" + "%310" * 16000) < 1.0


def test_one_account_on_the_cycle_19_trace_stays_fast():
    assert best(redaction("Ex%4"), REVIEW_TRACE) < 0.6


# ---- the same matches as before: the cycle-19 candidate logic, kept here as the reference ----
def old_found(r: accountlib.Redaction, level, chain, known, merge):
    """Redaction._found as it was at cfb5023 (each escape scanned the whole bucket of its last or first character)."""
    heads: dict = {}
    tails: dict = {}
    for form, rank, label, _, _, _ in r._forms:
        for k in range(1, min(accountlib.SEAM, len(form) - 1) + 1):
            heads.setdefault(form[k - 1], []).append((form, k, rank, label))
            tails.setdefault(form[len(form) - k], []).append((form, k, rank, label))
    hay = level.out
    for form, rank, label, lead, trail, fail in r._forms:
        for i, j in accountlib._occurrences(hay, form, fail, merge):
            yield i, j, rank, label, lead, trail
    if not chain:
        return
    slots, n, end = level.slots(), len(level.source), 2 * len(hay)
    made = accountlib._made(chain, known)
    at_slot = None
    seam = min(accountlib.SEAM, max((len(raw) for _, raw, _ in made), default=1) - 1)
    for x, raw, gone in made:
        hs, ts = heads.get(raw[-1]), tails.get(raw[0])
        if hs is None and ts is None:
            continue
        i, own = divmod((slots[x] & ~1 if x < n else end) if gone else slots[x], 2)
        for form, k, rank, label in hs or ():
            if not raw.endswith(form[:k]):
                continue
            at, rest = i + own, len(form) - k
            if hay.startswith(form[k:], at):
                yield i, at + rest, rank, label, not own, False
            shortest = rest - min(seam, rest)
            if not hay.startswith(form[k:k + shortest], at):
                continue
            if at_slot is None:
                at_slot = {}
                for y, other, dropped in made:
                    starts = at_slot.setdefault((slots[y] & ~1 if y < n else end) if dropped else slots[y], set())
                    starts.update(other[:m] for m in range(1, min(seam, len(other)) + 1))
            for k2 in range(1, min(seam, rest) + 1):
                q, tail = at + rest - k2, form[len(form) - k2:]
                for own2 in (1, 0):
                    if q + own2 > i and tail in at_slot.get(2 * q + own2, ()) and \
                            hay.startswith(form[k:len(form) - k2], at):
                        yield i, q + own2, rank, label, not own, not own2
        for form, k, rank, label in ts or ():
            rest = len(form) - k
            if i >= rest and raw.startswith(form[rest:]) and hay.startswith(form[:rest], i - rest):
                yield (i - rest, i + 1, rank, label, False, False) if own else (i - rest, i, rank, label, False, True)


def same_everywhere(r: accountlib.Redaction, text: str) -> int:
    """Assert the new and the old candidates are the same set at every level of `text`; how many there were."""
    known_new: list = []
    known_old: list = []
    count = 0
    for level, chain in accountlib._levels(text):
        if level is None:
            continue
        for merge in (True, False):
            new = set(r._found(level, chain, known_new, merge))
            old = set(old_found(r, level, chain, known_old, merge))
            assert new == old, (text, sorted(new ^ old))
            count += len(new)
    return count


HEADS = ["%", "%2", "%25", "%4", "%E2%80%8", "%F0%9F%98", "%C3%A", "\\", "\\u00", "\\u20", "\\x", "\\x4", "\\U0001F60",
         "\\ud83d\\ude0", "%5C", "%5Cu00"]
TAILS = ["F", "0", "1", "41", "2F", "e2%80%8e", "E%80%8E", "%80", "00", "e00", "6", "x", "8e", "A9"]
MIDDLE_PIECES = ["%E2%80%8E", "‎", "%25", "\\\\", "%2F", "\\u0041", " ", "+", "%20", "a", "%", "\\"]


def random_value(rng: random.Random) -> str:
    alphabet = "abcE01234%2F\\x u41-8e"
    while True:
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 14)))
        if not accountlib.weak(value) and not accountlib.unsettled(value):
            return value


def shown(value: str, rng: random.Random) -> str:
    """The value as text could show it: its ends taken into escapes around it, its middle per character, maybe one
    whole-text layer on top."""
    k1, k2 = rng.randint(0, min(3, len(value) - 1)), rng.randint(0, min(3, len(value) - 1))
    head = rng.choice(HEADS) if k1 or rng.random() < 0.3 else ""
    tail = rng.choice(TAILS) if k2 or rng.random() < 0.3 else ""
    body = value[k1:len(value) - k2] if len(value) - k2 > k1 else ""
    middle = "".join(one_char(c, rng) if c not in "%\\" and rng.random() < 0.5 else c for c in body)
    out = head + value[:k1] + middle + (value[len(value) - k2:] if len(value) - k2 > k1 else "") + tail
    if rng.random() < 0.3:
        out = rng.choice([lambda t: urllib.parse.quote(t, safe=""), lambda t: json.dumps(t)[1:-1],
                          lambda t: repr(t)[1:-1]])(out)
    return out


def test_the_indexed_candidates_are_the_old_candidates_at_random():
    rng = random.Random("cycle20-equivalence")
    total = 0
    for _ in range(2500):
        values = [random_value(rng) for _ in range(rng.randint(1, 5))]
        if rng.random() < 0.3:  # (values sharing a prefix, as in the review)
            stem = values[0][:rng.randint(1, 3)]
            values += [stem + random_value(rng) for _ in range(3)]
        pieces = []
        if rng.random() < 0.3:  # (a value whose rest is one escape, shown with its first character escaped too)
            first, rest = rng.choice("0123456789abcdef"), rng.choice(["%41", "%2F", "\\u0041", "\\x41", "%E2%80%8E", "%4",
                                                                       "\\U0001F600", "%C3%A9"])
            values.append(first + rest)
            pieces.append(rng.choice(["%3" + first if first.isdigit() else "%6" + first, "\\u003" + first
                                      if first.isdigit() else "\\x6" + first]) + rest + rng.choice(["", "x", "%41"]))
        r = redaction(*values)
        for _ in range(rng.randint(1, 6)):
            pieces.append(rng.choice([lambda: shown(rng.choice(values), rng), lambda: rng.choice(MIDDLE_PIECES),
                                      lambda: rng.choice(HEADS), lambda: rng.choice(TAILS)])())
        text = "".join(pieces)
        total += same_everywhere(r, text)
        assert r.scrub(r.scrub(text)) == r.scrub(text)
    assert total > 2000  # (the generator does reach the boundary checks)


@pytest.mark.parametrize("values, text, found", [
    (["Ex%4"], "%E2%80%8E" * 40 + "x" + "%E2%80%8E" * 40, False),
    ([f"E{i:05d}%4" for i in range(8)], "%E2%80%8E" * 20 + "E00003%4" + "%E2%80%8E" * 20, True),
    (["abc%2"], "%2a%62c%2F", True),
    (["abc%F0%9F%98"], "token=a%62c%F0%9F%98%80;", True),
    (["pw\U0001F600abc"], "token=pw\ud83d%5Cude00abc;", True),
    (["1%41b", "1%41c", "1x%4"], "%31%41b %31%2541c %F0%9F%98%80%31x%41", True),
    (["ab%2", "ab%3"], "ab%2F ab%30 %61b%2F x%61b%3", True),
    # (an escape takes in the first character, and the next escape all the rest: the value goes on with `%` or `\\`
    # where the level has the character that escape made)
    (["1%41"], "%31%41", True),
    (["a%2F", "x\\u0041"], "%61%2F x \\u0078\\u0041", True),
    (["b%E2%80%8E"], "%62%E2%80%8E;", True),
])
def test_the_indexed_candidates_are_the_old_candidates_on_known_cases(values, text, found):
    assert (same_everywhere(redaction(*values), text) > 0) == found
