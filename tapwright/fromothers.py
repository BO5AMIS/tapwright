"""Text the tool KNOWS other people wrote (AGENT-TOOLS 1.16 §21.1).

Tapwright knows only in some system apps' views, and only from the accessibility tree's types, identifiers, labels and
geometry. Unmarked text isn't claimed either way: a third-party app's comments are never marked, and a system view that
doesn't meet a rule below is left unmarked too. There are no recorded Messages or Mail trees in tests/data, so every
rule here is a conservative HEURISTIC built from what the tree can say, tested on synthetic trees; it errs toward
leaving text unmarked. (The marking is a hint, never the boundary: the skill says that ALL screen text the agent did
not write is data, whether or not it is marked, §21.2.)

The rules
- Messages (`com.apple.MobileSMS`): inside the conversation's transcript view (a scrolling container whose identifier or
  name contains "transcript"), a bubble is incoming when it lies toward the LEADING edge: its left gap is clearly
  smaller than its right gap (by more than 10 % of the screen width). A bubble on the right (the user's own), a
  centred one (a time stamp), and one too wide to tell are left unmarked. This assumes a left-to-right layout; in a
  right-to-left language the sides mirror, so the rule marks the user's own bubbles instead (a false mark only makes an
  agent more careful; a missed one is never claimed). The transcript's own identifier is not verified on a real phone.
- Mail (`com.apple.mobilemail`):
  - a message's body (the text inside a web view) when the screen is a message being READ: no Send button, no Subject,
    To, Cc or Bcc field and no compose identifier anywhere, and no keyboard up (a compose window's body is the
    user's own text);
  - the rows of a mailbox list (cells in a table or collection view), unless the navigation bar's title says it is
    the Sent, Drafts, Outbox or Scheduled mailbox (English titles only).
- Notes shared by others: the tree doesn't say whose a note is, so nothing is marked there.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

MESSAGES = "com.apple.MobileSMS"
MAIL = "com.apple.mobilemail"
KNOWN_APPS = frozenset({MESSAGES, MAIL})

# the roles of ui.Element a mark goes on: text a person wrote, the row that holds it
MARKED_ROLES = frozenset({"text", "header", "cell"})

_SENT_BOXES = re.compile(r"\b(sent|drafts?|outbox|scheduled|send later)\b", re.IGNORECASE)
_COMPOSE_NAME = re.compile(r"^(send|subject:?|to:?|cc:?|bcc:?|cc/bcc:?)$", re.IGNORECASE)
_TEXT_TYPES = {"StaticText", "TextView", "Cell", "Other", "Link", "Button"}
LEADING_GAP = 0.10  # of the screen's width: how much more room a bubble has on its right than on its left


def _type(node: ET.Element) -> str:
    return (node.get("type") or node.tag).removeprefix("XCUIElementType")


def _rect(node: ET.Element) -> tuple[float, float, float, float]:
    try:
        return tuple(float(node.get(k) or 0) for k in ("x", "y", "width", "height"))  # type: ignore[return-value]
    except ValueError:
        return (0.0, 0.0, 0.0, 0.0)


def _says(node: ET.Element) -> str:
    return (node.get("label") or node.get("value") or node.get("name") or "").strip()


def _subtree(node: ET.Element) -> list[ET.Element]:
    return list(node.iter())


def mark(root: ET.Element, bundle: str | None, screen: tuple[float, float] | None) -> set[ET.Element]:
    """The nodes whose subtree is text from other people, for the app `bundle`; empty for any other app."""
    if bundle == MESSAGES:
        return _messages(root, screen)
    if bundle == MAIL:
        return _mail(root)
    return set()


def _messages(root: ET.Element, screen: tuple[float, float] | None) -> set[ET.Element]:
    width = screen[0] if screen else _rect(root)[2]
    if width <= 0:
        return set()
    marked: set[ET.Element] = set()
    for transcript in root.iter():
        if "transcript" not in f"{transcript.get('name') or ''} {transcript.get('label') or ''}".casefold():
            continue
        if _type(transcript) not in ("CollectionView", "Table", "ScrollView", "Other"):
            continue
        for node in transcript.iter():
            if node is transcript or _type(node) not in _TEXT_TYPES or not _says(node):
                continue
            x, _, w, h = _rect(node)
            if w <= 0 or h <= 0 or w > 0.85 * width:
                continue  # a row across the screen says nothing about whose it is
            if (width - (x + w)) - x > LEADING_GAP * width:
                marked.add(node)
    return marked


def _mail(root: ET.Element) -> set[ET.Element]:
    nodes = _subtree(root)
    marked: set[ET.Element] = set()
    composing = any(_type(n) == "Keyboard" for n in nodes) or any(
        _COMPOSE_NAME.match(_says(n)) or _COMPOSE_NAME.match(n.get("name") or "")
        or "compose" in (n.get("name") or "").casefold() for n in nodes)
    if not composing:
        for web in nodes:
            if _type(web) == "WebView":
                marked.update(n for n in web.iter() if n is not web and _type(n) in ("StaticText", "Link", "TextView"))
    titles = " ".join(_says(n) for bar in nodes if _type(bar) == "NavigationBar" for n in [bar])
    if not _SENT_BOXES.search(titles) and not composing:
        for box in nodes:
            if _type(box) in ("Table", "CollectionView"):
                for cell in box.iter():
                    if _type(cell) == "Cell" and _says(cell):
                        marked.update(cell.iter())
    return marked
