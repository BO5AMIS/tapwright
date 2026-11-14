"""The explorer's skip floor (AGENT-TOOLS §25.5, §28.3): what `explore` never touches, checked afresh at EVERY touch.

The floor is the union of
1. the policy file's `confirm` and `block` words, the global and the app's (a union, not the replacement §11.1 gives an
   app's own list), and the built-in `confirm` words: `"confirm": []` and `--no-policy` don't lower it;
2. the reach-others words (§21.3: Send, Post, Publish, Reply, Comment) and Report and Block;
3. the never-grantable list of §19.2: payments, deleting an account, a card, Settings, Wallet and Passwords;
4. what only a tool can see: the system screens and sheets (§28.4), and a screen whose title says it is an account,
   privacy or security setting.
A user's skip list (`"explore": {"skip": [...]}`) only adds to it. Nothing here allows anything: a verdict is a reason to
skip, never a permission, and an element the floor doesn't hold is not thereby allowed (the dry-run check and the daemon's
own checks still stand). The word lists are policy.py's and grants.py's, not copies; the few words only the explorer adds
are named below. Pure functions over `ui.Element`: no phone.
"""
import re
from dataclasses import dataclass

from . import grants, policy, ui

REASONS = ("floor", "policy", "skip_list", "app_mark", "may_submit", "system")  # explored.WHYS, in the order they are given
# §25.5 item 2: the reach-others words of §21.3, and Report and Block
REACH_OTHERS = (*grants.REACH_WORDS, "Report", "Block")
# §19.2 "a card": the touches that add or show one (typing a card is the vault's, §20.1)
CARD_WORDS = ("Card number", "Credit card", "Debit card", "Add card", "CVV", "CVC")
# §19.2 "anything in an account, privacy or security setting", read from a screen's title (a place's name is `place_<n>`,
# so a title is all there is to go by; it is read, never kept)
PROTECTED_TITLE_WORDS = ("Account", "Accounts", "Privacy", "Security", "Password", "Passwords", "Wallet", "Payment", "Payments",
                         "Billing")
# §25.7: on a screen with a text input, a control with one of these words may submit a form
SUBMIT_WORDS = ("Save", "Submit", "Continue", "Sign in", "Log in", "Sign up", "Register", "Create", "Confirm", "Apply",
                "Update", "Share")
# the system's own sheets that spend or share, seen inside the app's tree (§28.4). A sheet in another process shows as
# another app in front, which the explorer treats as leaving the app.
SHEET_MARKERS = ("ActivityListView",)
# an alert's answers the explorer may give, best first: it denies and cancels, it never allows (§25.5, §28.4)
DENY_BUTTONS = ("Don't Allow", "Not Now", "Ask App Not to Track", "Cancel", "Dismiss", "Close", "Later", "No Thanks")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NOT_WORD = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class Skip:
    """Why an action is skipped: `why` is one of explored.WHYS, `word` the word that matched, when there is one."""
    why: str
    word: str | None = None


def _plain(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def id_words(identifier: str) -> str:
    """An id as words: `delete-row` and `deleteRow` are `delete row` (§25.5: for an element with no label and no text)."""
    return " ".join(_NOT_WORD.sub(" ", _CAMEL.sub(" ", identifier)).split())


def name_of(element: ui.Element) -> str:
    """What the floor matches a word against: the element's own label or text; an input's label only (what was typed in it
    is data, §11.1); an element with neither, such as an icon button, its id split into words (§25.5, for the explorer only)."""
    own = element.label if element.role == "input" else element.text
    return own if own else id_words(element.identifier)


def _first(words: tuple[str, ...], text: str) -> str | None:
    return next((w for w in words if grants.matches(w, text)), None)


class Floor:
    """The floor for one app. `policy` is the one the server runs with; `skip` the user's own words (by default the policy
    file's `explore.skip`). Built once; every method reads the element or screen it is given, so a policy or a screen that
    changed counts at once."""

    def __init__(self, rules: policy.Policy, app: str, skip: tuple[str, ...] | None = None) -> None:
        own = rules.apps.get(app, policy.AppWords())
        self.app = app
        self.floor_words = (*policy.DEFAULT_CONFIRM, *REACH_OTHERS, *grants.PAYMENT_WORDS, *grants.ACCOUNT_WORDS, *CARD_WORDS)
        self.policy_words = (*rules.block, *rules.confirm, *(own.block or ()), *(own.confirm or ()))
        self.skip_words = tuple(rules.explore.skip if skip is None else skip)

    # ---- one element ----

    def named(self, name: str) -> Skip | None:
        """The skip for a name (an element's label or text, or its id as words), or None. The floor's own words come
        first (`floor`), then the policy file's (`policy`), then the user's skip list (`skip_list`)."""
        name = _plain(name)
        if not name:
            return None
        for why, words in (("floor", self.floor_words), ("policy", self.policy_words), ("skip_list", self.skip_words)):
            if (word := _first(words, name)) is not None:
                return Skip(why, word)
        return None

    def element(self, element: ui.Element) -> Skip | None:
        """The skip for one element, or None."""
        return self.named(name_of(element))

    def recipients(self, element: ui.Element, items: list[ui.Element]) -> list[ui.Element]:
        """Every element a touch on `element` could land on or act through: itself, what lies inside its rect (a row's own
        button, a label drawn in a button), and the front-most element at its centre (§25.5: the real recipient)."""
        cx, cy = element.center
        front = next((e for e in reversed(items) if e.x <= cx < e.x + e.width and e.y <= cy < e.y + e.height), None)
        found = [element]
        for other in [*items, *( [front] if front is not None else [])]:
            if other is not element and other not in found and (other is front or inside(other, element)):
                found.append(other)
        return found

    def touch(self, element: ui.Element, items: list[ui.Element]) -> Skip | None:
        """The skip for a touch on `element`: the first of its recipients that the floor holds. Checked at every touch."""
        for each in self.recipients(element, items):
            if (hit := self.element(each)) is not None:
                return hit
        return None

    # ---- a screen ----

    def screen(self, tree: ui.Tree, bundle: str | None = None) -> Skip | None:
        """The skip for a whole screen (§28.4): Settings, Wallet and Passwords, a system sheet in the app's tree, a title
        that says account, privacy or security. Everything on it is skipped and it is not expanded."""
        where = bundle or (tree.app[1] if tree.app else None)
        if where in grants.NEVER_APPS:
            return Skip("system")
        if any((e.identifier or e.name) in SHEET_MARKERS for e in [*tree.items, *(h for h, _ in tree.behind)]):
            return Skip("system")
        titles = [tree.structure.title, *(e.text for e in tree.items if e.role == "header")]
        for title in titles:
            if title and (word := _first(PROTECTED_TITLE_WORDS, _plain(title))) is not None:
                return Skip("floor", word)
        return None

    # ---- submit (§25.7) ----

    def may_submit(self, element: ui.Element, items: list[ui.Element]) -> Skip | None:
        """On a screen with a text input, a control with a submit word may submit a form: skipped (`may_submit`). Part 2b
        decides when a listed id (`explore.submit`) may be touched; until then none is, and the floor wins over it anyway."""
        if not any(e.role == "input" and not e.in_alert for e in items):
            return None
        for each in self.recipients(element, items):
            if (word := _first(SUBMIT_WORDS, name_of(each))) is not None:
                return Skip("may_submit", word)
        return None


def inside(inner: ui.Element, outer: ui.Element) -> bool:
    return (outer.width > 0 and outer.height > 0 and outer.x <= inner.x and outer.y <= inner.y
            and inner.x + inner.width <= outer.x + outer.width and inner.y + inner.height <= outer.y + outer.height)


def safe_button(alert: ui.Alert, floor: Floor) -> str | None:
    """The button the explorer may press on an alert: a deny or cancel word first (`Don't Allow`, `Not Now`, `Cancel`...),
    never `Allow` or `OK` on a prompt that offers a denial; else the only button, when nothing on the alert is held by the
    floor. None when there is none (the branch ends and the explorer goes back another way)."""
    labels = {_plain(b).casefold(): b for b in alert.buttons}
    for word in DENY_BUTTONS:
        button = labels.get(word.casefold())
        if button is not None and floor.named(button) is None:
            return button
    if len(alert.buttons) == 1 and floor.named(_plain(alert.buttons[0])) is None \
            and floor.named(_plain(f"{alert.title} {alert.message}")) is None:
        return alert.buttons[0]
    return None
