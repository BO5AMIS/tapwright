"""The agent skill that ships with Tapwright (`tapwright/skill/`, the same files as `skills/tapwright/` in the
repository), and where agents look for skills.

The skill is in the open Agent Skills format (a folder with a SKILL.md). Claude Code reads `.claude/skills` in
a project and in the home folder; Codex and other agents read `.agents/skills` in both places. `install` copies
the skill into both, for one project or for the user. `tapline skill install` behaves and prints the same."""
import re
import shutil
from pathlib import Path

NAME = "tapwright"
SOURCE = Path(__file__).parent / "skill"
HOMES = (".claude/skills", ".agents/skills")  # under a project's folder, or under the user's home


def targets(project: str | None = None) -> list[Path]:
    """The folders the skill is written to: under `project`, else under the user's home."""
    base = Path(project).expanduser() if project else Path.home()
    return [base / home / NAME for home in HOMES]


def is_this_skill(folder: Path) -> bool:
    """Does the folder hold this skill (a SKILL.md whose frontmatter names it)? Only such a folder is replaced."""
    try:
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(rf"^name: {NAME}\s*$", text.split("\n---", 1)[0], re.M) is not None


def install(project: str | None = None) -> list[Path]:
    """Copy the skill into each target and return the folders written. A copy of this skill already there is
    replaced; a folder of that name that is something else is left alone, with an error, before anything is
    written. Raises OSError (FileNotFoundError, FileExistsError) with the sentence to print."""
    if project and not Path(project).expanduser().is_dir():
        raise FileNotFoundError(f"{Path(project).expanduser()} is not a folder. Pass the project's folder to --project.")
    where = targets(project)
    for target in where:  # check both before writing either, so a refusal leaves nothing half done
        if target.exists() and not is_this_skill(target):
            raise FileExistsError(f"{target} exists and is not the {NAME} skill. Move it away first; nothing was written.")
    for target in where:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(SOURCE, target)
    return where


def installed_text(written: list[Path]) -> str:
    """What `tapwright skill install` prints. The first lines are the same as `tapline skill install` prints."""
    return "\n".join([
        f"Installed the {NAME} skill:",
        *(f"  {path}" for path in written),
        "",
        f"Claude Code reads the first (type /{NAME}), Codex and other agents the second (in Codex: ${NAME}).",
        "Start a new agent session to pick it up. The skill needs Tapwright's MCP server too:",
        "  Claude Code:  claude mcp add tapwright -- tapwright mcp --apps <bundle id>",
        '  Codex:        in ~/.codex/config.toml:  [mcp_servers.tapwright]  command = "tapwright"  '
        'args = ["mcp", "--apps", "<bundle id>"]',
        "  Others:       register the stdio command `tapwright mcp --apps <bundle id>` under the name tapwright",
    ])
