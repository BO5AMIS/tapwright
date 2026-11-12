"""Exporting a journey's report to GitHub (docs/AGENT-TOOLS.md §17.5), only when the agent's `stop` carries `export`
(the user asked for it: it writes to their repository). It uses `gh` with the user's own sign-in:
- `{"to": "pr", "repo": "owner/name", "pr": 310}`: the report's files are committed to the PR's branch under
  `.tapwright/reports/<name>/` through the contents API, and the PR gets a comment with the summary and the pictures.
- `{"to": "issue", "repo": "owner/name"}`: the report's pictures go to the repository's `qa-reports` branch (made from the
  default branch if it isn't there) and an issue links them.
`gh` is run through `run_gh`, one place, so tests replace it."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

from .control import Refused

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = "qa-reports"
PICTURES = (".jpg", ".jpeg", ".png", ".gif")
GH_TIMEOUT = 120.0


def check(raw: object) -> dict:
    """The export argument, checked before the journey is stopped (`bad_params` otherwise)."""
    if not isinstance(raw, dict):
        raise Refused("bad_params", 'export is {"to": "pr", "repo": "owner/name", "pr": 310} or '
                                    '{"to": "issue", "repo": "owner/name"}')
    unknown = sorted(set(raw) - {"to", "repo", "pr"})
    if unknown:
        raise Refused("bad_params", f"export doesn't take {', '.join(unknown)}; it takes to, repo, pr")
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
    return {"to": raw["to"], "repo": repo, "pr": pr}


def run_gh(args: list[str], stdin: str | None = None) -> str:
    """Run `gh` and return its output; `Refused("unsupported")` when it isn't there or the call failed."""
    exe = shutil.which("gh")
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


def _json(args: list[str]) -> dict:
    try:
        return json.loads(run_gh(args) or "{}")
    except ValueError:
        raise Refused("unsupported", f"gh {' '.join(args[:2])} answered something that isn't JSON") from None


def _put(repo: str, branch: str, path: str, data: bytes, message: str) -> None:
    body = json.dumps({"message": message, "content": base64.b64encode(data).decode(), "branch": branch})
    run_gh(["api", "--method", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"], stdin=body)


def files_of(folder: Path, pictures_only: bool) -> list[Path]:
    keep = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    if pictures_only:
        keep = [p for p in keep if p.suffix.lower() in PICTURES or p.name == "report.md"]
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


def export(spec: dict, folder: Path, summary: str) -> str:
    """Do the export; returns the line for the answer saying what was written where. Raises Refused("unsupported")
    when `gh` is missing, signed out or refused."""
    repo, name = spec["repo"], folder.name
    if spec["to"] == "pr":
        pr = _json(["pr", "view", str(spec["pr"]), "--repo", repo, "--json", "headRefName,headRepository,headRepositoryOwner"])
        branch = pr.get("headRefName")
        owner = (pr.get("headRepositoryOwner") or {}).get("login")
        head = (pr.get("headRepository") or {}).get("name")
        if not branch or not owner or not head:
            raise Refused("unsupported", f"couldn't read PR #{spec['pr']} in {repo} (is it open, and is `gh` signed in?)")
        target = f"{owner}/{head}"
        base = f".tapwright/reports/{name}"
        for path in files_of(folder, pictures_only=False):
            _put(target, branch, f"{base}/{path.relative_to(folder).as_posix()}", path.read_bytes(),
                 f"Tapwright journey report: {name}")
        run_gh(["pr", "comment", str(spec["pr"]), "--repo", repo, "--body-file", "-"],
               stdin=_markdown(summary, name, target, branch, base, folder))
        return f"Exported: the report is committed to {target} branch {branch} under {base}/, and PR #{spec['pr']} in {repo} has a comment with it."
    # an issue: the pictures go to qa-reports
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
    for path in files_of(folder, pictures_only=True):
        _put(repo, BRANCH, f"{base}/{path.relative_to(folder).as_posix()}", path.read_bytes(),
             f"Tapwright journey report: {name}")
    title = summary.splitlines()[0][:200]
    url = run_gh(["issue", "create", "--repo", repo, "--title", f"QA report: {title}", "--body-file", "-"],
                 stdin=_markdown(summary, name, repo, BRANCH, base, folder)).strip().splitlines()
    return (f"Exported: the pictures are on the {BRANCH} branch of {repo} under {base}/, and an issue links them"
            + (f": {url[-1]}" if url else "."))
