"""Exporting a kept bug (AGENT-TOOLS 1.16.2 §29.7), only when the user asked: `export` of `bug_report`, or `tapwright bug
export`. Two targets:
- `{"to": "zip"}`: `<bug id>.zip` next to the bug's folder, with the folder in it. No network, no `confirm`. The entries carry
  no user name, no path of this machine and a fixed time.
- `{"to": "issue", "repo": "owner/name", "confirm": true}`: a GitHub issue with the user's own `gh`, as a journey's export is
  (journey_export.py, whose `gh` calls this reuses). The title is the page's title and the body `report.md`; `repro.json`, and
  the picture when it may go, are committed as one commit to the repository's `qa-reports` branch under
  `.tapwright/bugs/<app folder>/<bug id>/` (the app is part of the path: two apps' bugs with one id never share files) and
  linked from the body. It needs `confirm: true` (the agent says its user asked), else `not_allowed: needs_confirmation`. A
  public repository, or one whose visibility can't be read, is refused unless `public: true` is given.

A bug of an app that isn't one of the daemon's test apps (`serve --apps`; review R185 H2) needs `confirm: true` for ANY
export, a zip too: its page says why.

A private picture goes only when the export itself says `with_picture: true` and the picture is in the folder; a picture that
isn't private goes always. When it doesn't go, the page sent says that a private picture exists instead of linking it.

Every export re-runs the grep gate (§29.6, PLAN-1.8.5 E1) on every text it sends (each file, the issue's title and body) and
refuses when anything is found: `export refused: a vault value was found in report.md.` names the file, never the value, and
nothing is sent. Every export, a refused one too, writes one audit line (kind `export`): its detail is the target and the bug
id, never content. A line that can't be written means nothing is sent.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Callable

from . import bug, bugmake, bugstore, journey_export
from .control import Refused

KEYS = ("to", "repo", "confirm", "public", "with_picture")
TARGETS = ("issue", "zip")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
NEED_CONFIRM = ("needs_confirmation: an export to GitHub writes to your user's repository. Ask your user first whether this "
                "bug may go there, then call again with export.confirm: true.")
NO_AUDIT = "export refused: the audit line couldn't be written, so nothing was sent."
NEED_CONFIRM_APP = ("needs_confirmation: this bug's app isn't one of this daemon's test apps (tapwright serve --apps), so any "
                    "export of it, a zip too, needs your user's yes. Ask your user first, then call again with export.confirm: "
                    "true.")
Gate = Callable[[dict[str, str]], "str | None"]
Audit = Callable[[str], bool]


def check(raw: object) -> dict:
    """The export argument's shape (`bad_params` otherwise), checked before the bug is made. `confirm` is the export's own
    question, asked when it runs."""
    if not isinstance(raw, dict):
        raise Refused("bad_params", 'export is {"to": "zip"} or {"to": "issue", "repo": "owner/name", "confirm": true}')
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise Refused("bad_params", f"export doesn't take {', '.join(unknown)}; it takes {', '.join(KEYS)}")
    if raw.get("to") not in TARGETS:
        raise Refused("bad_params", f'export to is "issue" or "zip", not {raw.get("to")!r}')
    for key in ("confirm", "public", "with_picture"):
        if raw.get(key) is not None and not isinstance(raw[key], bool):
            raise Refused("bad_params", f"export {key} must be true or false, not {raw[key]!r}")
    repo = raw.get("repo")
    if raw["to"] == "issue":
        if not isinstance(repo, str) or not journey_export.REPO.fullmatch(repo):
            raise Refused("bad_params", f'export to "issue" needs repo "owner/name", not {repo!r}')
    elif repo is not None or raw.get("public") is not None:
        raise Refused("bad_params", 'export to "zip" takes no repo or public: it stays on this computer')
    return {"to": raw["to"], "repo": repo, "confirm": raw.get("confirm") is True, "public": raw.get("public") is True,
            "with_picture": raw.get("with_picture") is True}


# ---- what is sent ----

def _picture(held: bugstore.Kept, with_picture: bool) -> tuple[bytes | None, bool]:
    """(the picture's bytes when it goes, whether a picture is in the folder at all). Private unless the folder's record
    says otherwise."""
    path = held.folder / "picture.jpg"
    try:
        if path.is_symlink() or not path.is_file():
            return None, False
        data = path.read_bytes()
    except OSError:
        return None, False
    record = held.meta.get("picture_file") if isinstance(held.meta.get("picture_file"), dict) else {}
    private = record.get("private") is not False
    return (data if (not private or with_picture) else None), True


def texts_of(held: bugstore.Kept, picture_goes: bool, picture_held: bool) -> dict[str, str]:
    """The folder's text files as they are sent. When a picture is in the folder but doesn't go, the page says a private
    picture exists (as when it was never added) and `finding.json` says it wasn't included."""
    texts: dict[str, str] = {}
    for name in bugstore.TEXT_FILES:
        path = held.folder / name
        if path.is_symlink() or not path.is_file():
            continue
        texts[name] = path.read_text("utf-8")
    if picture_held and not picture_goes and "report.md" in texts:
        meta = {k: v for k, v in held.meta.items() if k != "picture_file"}
        meta["picture_included"] = False
        page = bug.replace_section(texts["report.md"], "Picture", bug.picture_lines(held.finding, meta, None))
        texts["report.md"] = page
        texts["report.html"] = bug.page_html(page)
        texts["finding.json"] = json.dumps({**held.finding, "bug": meta}, indent=2, ensure_ascii=False) + "\n"
    return texts


def zip_bytes(bug_id: str, texts: dict[str, str], picture: bytes | None) -> bytes:
    """The zip: `<bug id>/<file>` entries in a fixed order, a fixed time, no user name and no path of this machine."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        entries = [(name, texts[name].encode("utf-8")) for name in bugstore.TEXT_FILES if name in texts]
        if picture is not None:
            entries.append(("picture.jpg", picture))
        for name, data in entries:
            info = zipfile.ZipInfo(f"{bug_id}/{name}", date_time=ZIP_TIME)
            info.create_system = 3  # (unix: the same bytes whatever system writes it)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return out.getvalue()


def _refuse(audit: Audit, target: str, bug_id: str, why: str, message: str, code: str = "not_allowed") -> Refused:
    audit(f"{target}: {bug_id}, refused ({why})")
    return Refused(code, message)


def _confirmed(held: bugstore.Kept, spec: dict, audit: Audit, target: str) -> None:
    """A bug of an app that isn't a test app is exported only with `confirm` (review R185 H2)."""
    if held.meta.get("test_app") is not True and not spec["confirm"]:
        raise _refuse(audit, target, held.id, "needs confirmation", NEED_CONFIRM_APP)


_HEADING = re.compile(r"^(#{1,6} )(.*)$")


def inert(page: str) -> str:
    """The page as an issue's body with the app's text kept from acting on GitHub (review R185 L6): an `@name` or a `#12`
    outside a code fence would mention a person or link an issue of the repository. Each `@` and `#` there is escaped; the
    page's own headings and the log's fence stay as they are."""
    out: list[str] = []
    fenced = False
    for row in page.split("\n"):
        if row.startswith("```"):
            fenced = not fenced
            out.append(row)
            continue
        if fenced:
            out.append(row)
            continue
        head, rest = "", row
        if (m := _HEADING.match(row)) is not None:
            head, rest = m.group(1), m.group(2)
        out.append(head + rest.replace("\\", "\\\\").replace("@", "\\@").replace("#", "\\#"))
    return "\n".join(out)


def _gated(gate: Gate, texts: dict[str, str], audit: Audit, target: str, bug_id: str) -> None:
    bad = gate(texts)
    if bad is not None:
        raise _refuse(audit, target, bug_id, "redaction check", f"export refused: a vault value was found in {bad}. "
                                                                "Nothing was sent.")


def _picture_word(sent: bool) -> str:
    return "picture sent" if sent else "picture not sent"


# ---- the zip ----

def to_zip(held: bugstore.Kept, spec: dict, gate: Gate, audit: Audit, home: Path | None = None, checked: str = "") -> str:
    picture, there = _picture(held, spec["with_picture"])
    path = held.folder.parent / f"{held.id}.zip"
    target = f"zip {bugmake.shown_path(path, home)}"
    _confirmed(held, spec, audit, target)
    texts = texts_of(held, picture is not None, there)
    _gated(gate, texts, audit, target, held.id)
    data = zip_bytes(held.id, texts, picture)
    if not audit(f"{target}: {held.id}, {_picture_word(picture is not None)}{checked and f'; {checked}'}"):
        raise Refused("not_allowed", NO_AUDIT)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    said = "with its picture" if picture is not None else (
        "without its private picture (export with_picture: true includes it)" if there else "")
    return f"Exported: {bugmake.shown_path(path, home)}" + (f", {said}." if said else ".") + (
        f" Checked for: {checked}." if checked else "")


# ---- the issue ----

def issue_base(held: bugstore.Kept) -> str:
    return f".tapwright/bugs/{bug.app_folder(held.app)}/{held.id}"


def issue_body(held: bugstore.Kept, page: str, repo: str, picture_sent: bool) -> str:
    """`report.md` as the issue's body, its text kept inert (`inert`): the picture linked from where it was pushed (when it
    went), and the script."""
    base = issue_base(held)
    page = inert(page)
    if picture_sent:
        page = bug.replace_section(page, "Picture", [
            f"![The screen at the moment]({journey_export.raw_url(repo, journey_export.BRANCH, f'{base}/picture.jpg')})"])
    script = journey_export.raw_url(repo, journey_export.BRANCH, f"{base}/repro.json")
    return page.rstrip("\n") + f"\n\nScript: [repro.json]({script}) on `{journey_export.BRANCH}` under `{base}/`.\n"


def _public(repo: str) -> bool:
    """Is the repository public? One that can't be told counts as public (§29.7)."""
    try:
        seen = str(journey_export._json(["repo", "view", repo, "--json", "visibility"]).get("visibility") or "").upper()
    except Refused:
        return True
    return seen not in ("PRIVATE", "INTERNAL")


def to_issue(held: bugstore.Kept, spec: dict, gate: Gate, audit: Audit, checked: str = "") -> str:
    repo = spec["repo"]
    target = f"issue {repo}"
    if not spec["confirm"]:
        raise _refuse(audit, target, held.id, "needs confirmation",
                      NEED_CONFIRM_APP if held.meta.get("test_app") is not True else NEED_CONFIRM)
    picture, there = _picture(held, spec["with_picture"])
    texts = texts_of(held, picture is not None, there)
    if "report.md" not in texts or "repro.json" not in texts:
        raise _refuse(audit, target, held.id, "missing files",
                      f"{held.id}: its page or its script is missing from its folder; make the bug again.", "not_found")
    title = bug.one_line(texts["report.md"].split("\n", 1)[0].removeprefix("# "))[:200]
    body = issue_body(held, texts["report.md"], repo, picture is not None)
    # (the page as it is too: the issue's body has its `@` and `#` escaped, which a value may hold)
    _gated(gate, {"title": title, "report.md": texts["report.md"], "issue body": body, "repro.json": texts["repro.json"]},
           audit, target, held.id)
    public = _public(repo)
    if public and not spec["public"]:
        raise _refuse(audit, target, held.id, "public repository",
                      f"export refused: {repo} is a public repository (or its visibility can't be read). Ask your user; if "
                      "they agree, export again with public: true.")
    if not audit(f"{target}: {held.id}, {_picture_word(picture is not None)}" + (" (public)" if public else "")
                 + (f"; {checked}" if checked else "")):
        raise Refused("not_allowed", NO_AUDIT)
    try:
        _send(held, repo, title, body, texts["repro.json"], picture)
    except Refused as e:
        audit(f"{target}: {held.id}, failed ({e.code})")
        raise
    left = "" if picture is not None or not there else " The private picture wasn't sent (export with_picture: true sends it)."
    return (f"Exported: an issue in {repo}, and repro.json{' and the picture' if picture is not None else ''} on the "
            f"{journey_export.BRANCH} branch under {issue_base(held)}/.{left}" + (f" Checked for: {checked}." if checked else ""))


def _send(held: bugstore.Kept, repo: str, title: str, body: str, script: str, picture: bytes | None) -> None:
    j = journey_export
    default = j._json(["api", f"repos/{repo}"]).get("default_branch")
    if not default:
        raise Refused("unsupported", f"couldn't read {repo} (is `gh` signed in, and is the repository there?)")
    try:
        j.run_gh(["api", f"repos/{repo}/branches/{j.BRANCH}"])
    except Refused:
        sha = (j._json(["api", f"repos/{repo}/git/ref/heads/{default}"]).get("object") or {}).get("sha")
        if not sha:
            raise Refused("unsupported", f"couldn't read {default} in {repo} to make the {j.BRANCH} branch") from None
        j.run_gh(["api", "--method", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{j.BRANCH}", "-f", f"sha={sha}"])
    base = issue_base(held)
    files = {f"{base}/repro.json": script.encode("utf-8")}
    if picture is not None:
        files[f"{base}/picture.jpg"] = picture
    j._commit(repo, j.BRANCH, files, f"Tapwright bug report: {held.id}")
    j.run_gh(["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"], stdin=body)


def export(held: bugstore.Kept, spec: dict, gate: Gate, audit: Audit, home: Path | None = None, checked: str = "") -> str:
    """The export; the line for the answer saying what was written where. Raises Refused when it is refused or fails.
    `checked`: which values the gate held, said in the answer and the audit line when it isn't the daemon's whole set (the
    command line with no daemon: review R185 M3)."""
    if spec["to"] == "zip":
        return to_zip(held, spec, gate, audit, home, checked)
    return to_issue(held, spec, gate, audit, checked)
