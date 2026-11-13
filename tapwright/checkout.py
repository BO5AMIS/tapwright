"""`tapwright doctor`'s look at where Tapwright runs from. A QA run imported the development folder in the middle of a
merge and died on `<<<<<<<` (1.3.1). A pip-installed package has no git folder above it and is fine; a package inside a
git work tree is warned about when it has uncommitted changes, and fails when a source file holds conflict markers."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Literal

MARKERS = ("<<<<<<< ", ">>>>>>> ")
GIT_WAIT = 5.0  # seconds git may take

Verdict = tuple[Literal["ok", "note", "fail"], str]


def work_tree(package: Path) -> Path | None:
    """The folder with a `.git` above the package (or holding it), else None: an installed package. A package in a
    venv's site-packages is installed even when the venv sits inside a git repository (review 1.3.1), and a source
    checkout has its pyproject.toml next to the package."""
    if {"site-packages", "dist-packages"} & {part.lower() for part in package.parts}:
        return None
    if not (package.parent / "pyproject.toml").exists():
        return None
    for folder in (package, *package.parents):
        if (folder / ".git").exists():
            return folder
    return None


def conflicted(package: Path) -> list[Path]:
    """The package's .py files with a line that starts a conflict marker."""
    found = []
    for path in sorted(package.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(line.startswith(MARKERS) for line in text.splitlines()):
            found.append(path)
    return found


def changes(root: Path, package: Path, run: Callable = subprocess.run) -> list[str] | None:
    """`git status --porcelain` lines for the package's folder; None when git can't say (not installed, too slow)."""
    try:
        done = run(["git", "-C", str(root), "status", "--porcelain", "--", str(package)], capture_output=True,
                   text=True, timeout=GIT_WAIT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [line for line in done.stdout.splitlines() if line.strip()]


def check(package: Path, run: Callable = subprocess.run) -> Verdict | None:
    """What doctor says of where Tapwright runs from, or None to say nothing."""
    root = work_tree(package)
    if root is None:
        return "ok", "running from an installed package"
    bad = conflicted(package)
    if bad:
        names = ", ".join(str(p.relative_to(package.parent)) for p in bad[:3]) + (" ..." if len(bad) > 3 else "")
        return "fail", (f"unresolved merge markers (<<<<<<< / >>>>>>>) in {names}: tapwright can't run from this "
                        "folder. Finish the merge, or install a release in its own venv: pip install tapwright==<version>")
    changed = changes(root, package, run)
    if changed:
        return "note", (f"tapwright runs from a development checkout ({root}) with {len(changed)} uncommitted "
                        f"change{'s' if len(changed) != 1 else ''} in the package: whatever the folder holds is what "
                        "runs. For QA or unattended runs, install a pinned release in its own venv: "
                        "pip install tapwright==<version>")
    if changed is None:
        return None  # no git to ask: say nothing
    return "ok", f"running from a clean development checkout ({root})"
