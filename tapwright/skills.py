"""The agent skill that ships with Tapwright (`tapwright/skill/`, the same files as `skills/tapwright/` in the
repository), and where agents look for skills.

The skill is in the open Agent Skills format (a folder with a SKILL.md). Claude Code reads `.claude/skills` in
a project and in the home folder; Codex and other agents read `.agents/skills` in both places. `install` copies
the skill into both, for one project or for the user."""
import shutil
from pathlib import Path

NAME = "tapwright"
SOURCE = Path(__file__).parent / "skill"
HOMES = (".claude/skills", ".agents/skills")  # under a project's folder, or under the user's home


def targets(project: str | None = None) -> list[Path]:
    """The folders the skill is written to: under `project`, else under the user's home."""
    base = Path(project).expanduser() if project else Path.home()
    return [base / home / NAME for home in HOMES]


def install(project: str | None = None) -> list[Path]:
    """Copy the skill into each target, replacing an older copy of this skill and nothing else. Returns the
    folders written. Raises FileNotFoundError when `project` isn't a folder."""
    if project and not Path(project).expanduser().is_dir():
        raise FileNotFoundError(f"{project} is not a folder")
    written = []
    for target in targets(project):
        if target.exists():
            shutil.rmtree(target)  # our own folder only: the name is ours
        shutil.copytree(SOURCE, target)
        written.append(target)
    return written
