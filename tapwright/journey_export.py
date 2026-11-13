"""Exporting a journey's report to GitHub (docs/AGENT-TOOLS.md §17.5), only when the agent's `stop` carries `export`
with `confirm: true` (the agent says its user asked for it: it writes to their repository). It uses `gh` with the user's
own sign-in:
- `{"to": "pr", "repo": "owner/name", "pr": 310, "confirm": true}`: the report's files are committed, as one commit, to the
  PR's branch under `.tapwright/reports/<name>/`, and the PR gets a comment with the summary and the pictures. When the PR's
  head repository is not `repo` (a fork), nothing is written unless `fork: true` is also given.
- `{"to": "issue", "repo": "owner/name", "confirm": true}`: the report's pictures go, as one commit, to the repository's
  `qa-reports` branch (made from the default branch if it isn't there) and an issue links them.
A public repository is refused unless `public: true` is also given.
Only report.md, report.html, journey.gif, filmstrip.jpg, the findings' pictures and the screens report.md links are uploaded, as
one commit through the git data API (blobs, a tree, a commit, a ref), so a PR branch gets one commit, not one per file.
`gh` is run through `run_gh`, one place, so tests replace it."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .control import Refused

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH_NAME = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")
BRANCH = "qa-reports"
FILES = ("report.md", "report.html", "journey.gif", "filmstrip.jpg")  # and findings/*.jpg
SCREEN_LINK = re.compile(r"\]\((screens/[A-Za-z0-9_.-]+\.jpg)\)")  # a screen picture report.md links
GH_TIMEOUT = 120.0
KEYS = ("to", "repo", "pr", "confirm", "public", "fork")
NEED_CONFIRM = ("export writes to the user's GitHub repository. Ask your user first whether the report may be exported "
                "there, then call again with export.confirm: true.")


def check(raw: object) -> dict:
    """The export argument, checked before the journey is stopped (`bad_params` otherwise)."""
    if not isinstance(raw, dict):
        raise Refused("bad_params", 'export is {"to": "pr", "repo": "owner/name", "pr": 310, "confirm": true} or '
                                    '{"to": "issue", "repo": "owner/name", "confirm": true}')
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise Refused("bad_params", f"export doesn't take {', '.join(unknown)}; it takes {', '.join(KEYS)}")
    if raw.get("to") not in ("pr", "issue"):
        raise Refused("bad_params", f'export to must be "pr" or "issue", not {raw.get("to")!r}')
    repo = raw.get("repo")
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise Refused("bad_params", f'export repo must be "owner/name", not {repo!r}')
    pr = raw.get("pr")
    if raw["to"] == "pr":
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
            raise Refused("bad_params", 'export to "pr" needs the pull request number: "pr": 310')
    elif pr is not None:
        raise Refused("bad_params", 'export to "issue" takes no pr')
    for key in ("confirm", "public", "fork"):
        if raw.get(key) is not None and not isinstance(raw[key], bool):
            raise Refused("bad_params", f"export {key} must be true or false, not {raw[key]!r}")
    if raw.get("confirm") is not True:
        raise Refused("bad_params", NEED_CONFIRM)
    if raw.get("fork") and raw["to"] != "pr":
        raise Refused("bad_params", 'export fork is for to "pr"')
    return {"to": raw["to"], "repo": repo, "pr": pr, "public": raw.get("public") is True, "fork": raw.get("fork") is True}


def run_gh(args: list[str], stdin: str | None = None) -> str:
    """Run `gh` and return its output; `Refused("unsupported")` when it isn't there or the call failed. On Windows only
    `gh.exe` is run: a `.cmd` shim would pass the arguments (a goal's text) through cmd.exe."""
    exe = shutil.which("gh.exe" if os.name == "nt" else "gh")
    if exe is None:
        raise Refused("unsupported", "export needs the GitHub command line `gh`, signed in (`gh auth login`); it isn't "
                                     "installed here")
    try:
        done = subprocess.run([exe, *args], input=stdin, capture_output=True, text=True, timeout=GH_TIMEOUT,
                              encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused("unsupported", f"gh didn't run: {e}") from None
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        raise Refused("unsupported", f"gh {' '.join(args[:2])} failed: {said[-1] if said else done.returncode}")
    return done.stdout


def _json(args: list[str], stdin: str | None = None) -> dict:
    try:
        value = json.loads(run_gh(args, stdin) or "{}")
    except ValueError:
        raise Refused("unsupported", f"gh {' '.join(args[:2])} answered something that isn't JSON") from None
    return value if isinstance(value, dict) else {}


def files_of(folder: Path) -> list[Path]:
    """What is uploaded: the report's two documents, the GIF, the filmstrip, the findings' pictures and the screen
    pictures report.md links (only those: the documents must not show broken images)."""
    keep = [folder / name for name in FILES if (folder / name).is_file()]
    findings = folder / "findings"
    if findings.is_dir():
        keep += sorted(p for p in findings.glob("*.jpg") if p.is_file())
    try:
        text = (folder / "report.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for name in sorted(set(SCREEN_LINK.findall(text))):
        p = folder / name
        if p.is_file():
            keep.append(p)
    return keep


def raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://github.com/{repo}/raw/{branch}/{path}"


def _markdown(summary: str, folder_name: str, repo: str, branch: str, base: str, folder: Path) -> str:
    """The comment or issue body: the summary, then the pictures linked from where they were pushed."""
    def img(name: str, alt: str) -> list[str]:
        return [f"![{alt}]({raw_url(repo, branch, f'{base}/{name}')})", ""] if (folder / name).is_file() else []
    lines = [summary, ""]
    lines += img("journey.gif", "The journey") + img("filmstrip.jpg", "Filmstrip")
    for p in sorted((folder / "findings").glob("*.jpg")) if (folder / "findings").is_dir() else []:
        lines += img(f"findings/{p.name}", p.stem)
    lines.append(f"Full report: `{base}/report.md` (and `report.html`) on `{branch}`.")
    return "\n".join(lines)


def _check_visibility(repo: str, public: bool) -> None:
    """Refuse a public repository unless `public: true` was given; refuse when the visibility can't be read."""
    seen = str(_json(["repo", "view", repo, "--json", "visibility"]).get("visibility") or "").upper()
    if not seen:
        raise Refused("unsupported", f"couldn't tell whether {repo} is public or private; nothing was written")
    if seen == "PUBLIC" and not public:
        raise Refused("not_allowed", f"{repo} is a public repository, and the report shows the app's screens. Ask your "
                                     "user; if they agree, export again with public: true.")


def _commit(repo: str, branch: str, files: dict[str, bytes], message: str) -> None:
    """All `files` ({path: bytes}) as one commit on `branch`, through the git data API."""
    if not BRANCH_NAME.fullmatch(branch):
        raise Refused("unsupported", f"won't write to a branch named {branch!r}")
    head = (_json(["api", f"repos/{repo}/git/ref/heads/{branch}"]).get("object") or {}).get("sha")
    tree = ((_json(["api", f"repos/{repo}/git/commits/{head}"]).get("tree") or {}).get("sha")) if head else None
    if not head or not tree:
        raise Refused("unsupported", f"couldn't read branch {branch} of {repo}")
    entries = []
    for path, data in files.items():
        blob = _json(["api", "--method", "POST", f"repos/{repo}/git/blobs", "--input", "-"],
                     stdin=json.dumps({"content": base64.b64encode(data).decode(), "encoding": "base64"})).get("sha")
        if not blob:
            raise Refused("unsupported", f"couldn't upload {path} to {repo}")
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
    new_tree = _json(["api", "--method", "POST", f"repos/{repo}/git/trees", "--input", "-"],
                     stdin=json.dumps({"base_tree": tree, "tree": entries})).get("sha")
    commit = _json(["api", "--method", "POST", f"repos/{repo}/git/commits", "--input", "-"],
                   stdin=json.dumps({"message": message, "tree": new_tree, "parents": [head]})).get("sha") \
        if new_tree else None
    if not commit:
        raise Refused("unsupported", f"couldn't make the commit in {repo}")
    run_gh(["api", "--method", "PATCH", f"repos/{repo}/git/refs/heads/{branch}", "--input", "-"],
           stdin=json.dumps({"sha": commit, "force": False}))


def export(spec: dict, folder: Path, summary: str) -> str:
    """Do the export; returns the line for the answer saying what was written where. Raises Refused("unsupported" or
    "not_allowed") when `gh` is missing, signed out or refused, or the target needs a `public` or `fork` the agent
    didn't give."""
    repo, name = spec["repo"], folder.name
    message = f"Tapwright journey report: {name}"
    if spec["to"] == "pr":
        pr = _json(["pr", "view", str(spec["pr"]), "--repo", repo, "--json", "headRefName,headRepository,headRepositoryOwner"])
        branch = pr.get("headRefName")
        owner = (pr.get("headRepositoryOwner") or {}).get("login")
        head = (pr.get("headRepository") or {}).get("name")
        if not branch or not owner or not head:
            raise Refused("unsupported", f"couldn't read PR #{spec['pr']} in {repo} (is it open, and is `gh` signed in?)")
        target = f"{owner}/{head}"
        if target.lower() != repo.lower() and not spec.get("fork"):
            raise Refused("not_allowed", f"PR #{spec['pr']}'s branch is in {target}, not in {repo}: the report would be "
                                         f"written to {target}. Ask your user; if they agree, export again with fork: true.")
        for where in dict.fromkeys((repo, target)):
            _check_visibility(where, spec.get("public", False))
        base = f".tapwright/reports/{name}"
        _commit(target, branch, {f"{base}/{p.relative_to(folder).as_posix()}": p.read_bytes() for p in files_of(folder)},
                message)
        run_gh(["pr", "comment", str(spec["pr"]), "--repo", repo, "--body-file", "-"],
               stdin=_markdown(summary, name, target, branch, base, folder))
        return (f"Exported: the report is committed to {target} branch {branch} under {base}/, and PR #{spec['pr']} "
                f"in {repo} has a comment with it.")
    # an issue: the pictures go to qa-reports
    _check_visibility(repo, spec.get("public", False))
    info = _json(["api", f"repos/{repo}"])
    default = info.get("default_branch")
    if not default:
        raise Refused("unsupported", f"couldn't read {repo} (is `gh` signed in, and is the repository there?)")
    try:
        run_gh(["api", f"repos/{repo}/branches/{BRANCH}"])
    except Refused:
        sha = (_json(["api", f"repos/{repo}/git/ref/heads/{default}"]).get("object") or {}).get("sha")
        if not sha:
            raise Refused("unsupported", f"couldn't read {default} in {repo} to make the {BRANCH} branch") from None
        run_gh(["api", "--method", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={sha}"])
    base = f"reports/{name}"
    _commit(repo, BRANCH, {f"{base}/{p.relative_to(folder).as_posix()}": p.read_bytes() for p in files_of(folder)}, message)
    title = summary.splitlines()[0][:200]
    url = run_gh(["issue", "create", "--repo", repo, "--title", f"QA report: {title}", "--body-file", "-"],
                 stdin=_markdown(summary, name, repo, BRANCH, base, folder)).strip().splitlines()
    return (f"Exported: the pictures are on the {BRANCH} branch of {repo} under {base}/, and an issue links them"
            + (f": {url[-1]}" if url else "."))
