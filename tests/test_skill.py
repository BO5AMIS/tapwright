"""The agent skill: the Agent Skills format's rules, the two copies being one set of bytes, and the installer."""
import re
from pathlib import Path

import pytest

from tapwright import cli, skills
from tapwright.descriptions import SHARED
from tapwright.errors import CODES

REPO = Path(__file__).parent.parent
SHIPPED, LISTED = skills.SOURCE, REPO / "skills" / "tapwright"
FILES = ("SKILL.md", "references/setup.md", "references/personas.md", "agents/openai.yaml")


def frontmatter(text: str) -> dict:
    block = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    return dict(re.findall(r"^(\w[\w-]*): (.+)$", block, re.M))


def test_the_packaged_skill_and_the_repositorys_are_the_same_bytes():
    """`skills/tapwright` is what skill installers read; `tapwright/skill` is what pip ships. One text."""
    for name in FILES:
        assert (SHIPPED / name).read_bytes() == (LISTED / name).read_bytes(), name
    assert sorted(p.relative_to(SHIPPED).as_posix() for p in SHIPPED.rglob("*") if p.is_file()) == sorted(FILES)
    assert sorted(p.relative_to(LISTED).as_posix() for p in LISTED.rglob("*") if p.is_file()) == sorted(FILES)


def test_the_skill_follows_the_agent_skills_format():
    text = (SHIPPED / "SKILL.md").read_text(encoding="utf-8")
    assert "\r" not in text
    meta = frontmatter(text)
    assert meta["name"] == "tapwright" == SHIPPED.parent.name == LISTED.name  # the name is the folder's
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"]) and len(meta["name"]) <= 64
    assert 0 < len(meta["description"]) <= 1024 and meta["description"].startswith("Use when")
    assert len(meta["compatibility"]) <= 500
    assert len(text.splitlines()) < 500  # the format's advice: longer material goes to references/
    for link in re.findall(r"\]\(([^)]+)\)", text):
        assert (SHIPPED / link).is_file(), link  # every file the skill points to ships with it


def test_the_skill_names_only_tools_and_codes_the_tool_has():
    """A skill that teaches a call the server refuses is worse than none."""
    text = (SHIPPED / "SKILL.md").read_text(encoding="utf-8") + (SHIPPED / "references/setup.md").read_text(encoding="utf-8")
    from tapwright.descriptions import EXTRA
    tools = set(SHARED) | set(EXTRA)
    called = set(re.findall(r"`(look|tap|long_press|type_text|scroll|toggle|back|open_url|wait|wait_for|apps|open_app"
                            r"|viewer|close_app|alert|press_button|swipe|logs|reload|pair|record|replay)`", text))
    assert called <= tools, called - tools
    table = text.split("## 5. Errors")[1].split("## 6.")[0]
    named = set(re.findall(r"`([a-z_]+)`", "\n".join(line.split("|")[1] for line in table.splitlines() if line.startswith("| `"))))
    assert named <= set(CODES), named - set(CODES)
    assert set(CODES) - named <= {"not_connected", "unsupported"}  # (said in setup.md's table and by the message)
    assert 'to: "target"' in text and "--apps" in text and "not_allowed" in text


def test_install_writes_both_homes_and_replaces_only_its_own_folder(tmp_path, capsys, monkeypatch):
    other = tmp_path / ".claude" / "skills" / "another-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("theirs", encoding="utf-8")
    stale = tmp_path / ".agents" / "skills" / "tapwright"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("from an older version", encoding="utf-8")
    (stale / "SKILL.md").write_text("---\nname: tapwright\ndescription: an older copy\n---\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tapwright", "skill", "install", "--project", str(tmp_path)])
    assert cli.main() == 0
    for home in (".claude/skills", ".agents/skills"):
        for name in FILES:
            assert (tmp_path / home / "tapwright" / name).read_bytes() == (SHIPPED / name).read_bytes()
    assert not (stale / "old.md").exists() and (other / "SKILL.md").read_text(encoding="utf-8") == "theirs"
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Installed the tapwright skill:"  # these three lines are the same shape in Tapline
    assert out[1] == "  " + str(tmp_path / ".claude" / "skills" / "tapwright")
    assert out[2] == "  " + str(tmp_path / ".agents" / "skills" / "tapwright")
    assert out[3] == "" and out[4].startswith("Claude Code reads the first (type /tapwright)")
    assert any("[mcp_servers.tapwright]" in line and "--apps" in line for line in out[5:])  # never without the app list


def test_install_leaves_a_folder_of_that_name_that_is_not_the_skill(tmp_path, capsys, monkeypatch):
    """Someone's own folder called `tapwright` under .agents/skills: nothing is written, in either place."""
    theirs = tmp_path / ".agents" / "skills" / "tapwright"
    theirs.mkdir(parents=True)
    (theirs / "notes.md").write_text("mine", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tapwright", "skill", "install", "--project", str(tmp_path)])
    assert cli.main() == 1
    said = capsys.readouterr().out
    assert "exists and is not the tapwright skill. Move it away first; nothing was written." in said
    assert (theirs / "notes.md").read_text(encoding="utf-8") == "mine"
    assert not (tmp_path / ".claude" / "skills" / "tapwright").exists()


def test_install_for_the_user_goes_under_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(skills.Path, "home", classmethod(lambda cls: tmp_path))
    written = skills.install()
    assert written == [tmp_path / ".claude/skills/tapwright", tmp_path / ".agents/skills/tapwright"]
    assert all((path / "SKILL.md").is_file() for path in written)


def test_install_into_a_folder_that_is_not_there_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["tapwright", "skill", "install", "--project", str(tmp_path / "missing")])
    assert cli.main() == 1 and "couldn't install the skill" in capsys.readouterr().out
    with pytest.raises(FileNotFoundError):
        skills.install(str(tmp_path / "missing"))

def test_the_personas_guide_is_tool_neutral_and_has_the_six_examples():
    """The file is shared byte for byte with the sibling tool: no tool's own words in it."""
    text = (SHIPPED / "references/personas.md").read_text(encoding="utf-8")
    for word in ("Tapwright", "Tapline", "open_app", "app_command"):
        assert word not in text, word
    assert "pass these as `conditions` on `journey { start }`; each tool applies them its own way" in " ".join(text.split())
    for persona in ("First-timer", "Busy regular", "Older user with large text", "Right-to-left speaker",
                    "Poor connection", "One-handed on a big phone"):
        assert f"**{persona}.**" in text, persona
    assert "`language: ar · region: QA`" in text
    skill = (SHIPPED / "SKILL.md").read_text(encoding="utf-8")
    assert "(references/personas.md)" in skill and "`ux_check`" in skill and "`media`" in skill
