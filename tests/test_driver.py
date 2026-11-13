"""The driver agent (PLAN-1.6 Part D): the handbook, the file for each host, the model override, and install."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tapwright import cli, connect, driver, skills
from tapwright.descriptions import SHARED, describe

from test_connect import FakeClaude, home


def toml(text: str) -> dict:
    """Parsed TOML; the test is skipped on Python 3.10, which has no tomllib."""
    return pytest.importorskip("tomllib").loads(text)


def split_claude(text: str) -> tuple[dict[str, str], str]:
    block, body = re.match(r"---\n(.*?)\n---\n\n(.*)", text, re.S).groups()
    return dict(re.findall(r"^([\w-]+): (.+)$", block, re.M)), body


def test_the_handbook_is_short_and_says_what_the_plan_asks():
    text = driver.handbook()
    assert len(text) / 4 <= 600, "about 600 tokens at most"
    for word in ("`map`", "`drive`", "`run`", "`expect`", "`if`", "`optional`", "Read every answer whole",
                 "Never cut it", "Write nothing between calls", "`needs_confirmation`", "`locked`", "`paused`",
                 "`off_route`", "Finish with five lines", "`account`", "`record`"):
        assert word in text, word
    assert len(re.findall(r"^[1-5]\. ", text.split("Finish with five lines")[1], re.M)) == 5


def test_the_handbook_never_names_a_brand_of_model():
    text = driver.handbook().lower()
    for brand in ("haiku", "claude", "gpt", "codex", "sonnet", "opus", "openai", "anthropic"):
        assert brand not in text, brand
    assert "small, fast model" in text


def test_the_handbook_is_the_skills_reference_and_the_two_copies_match():
    listed = Path(__file__).parent.parent / "skills" / "tapwright" / "references" / "driver.md"
    assert listed.read_bytes() == (skills.SOURCE / "references" / "driver.md").read_bytes()
    assert driver.handbook().strip() == (skills.SOURCE / "references" / "driver.md").read_text(encoding="utf-8").strip()


def test_the_claude_file_has_the_model_low_effort_and_both_servers_driving_tools():
    meta, body = split_claude(driver.render("claude"))
    assert meta["name"] == "phone-driver" and meta["model"] == "haiku" and meta["effort"] == "low"
    tools = [t.strip() for t in meta["tools"].split(",")]
    for tool in ("look", "tap", "type_text", "scroll", "toggle", "back", "swipe", "wait_for", "run", "drive", "map",
                 "open_app", "open_url", "alert", "session_report"):
        assert f"mcp__tapwright__{tool}" in tools and f"mcp__tapline__{tool}" in tools, tool
    assert all(re.fullmatch(r"mcp__(tapwright|tapline)__[a-z_]+", t) for t in tools)
    assert not [t for t in tools if t.split("__")[2] in ("journey", "media", "press_button", "viewer", "close_app")]
    assert body == driver.handbook()
    assert meta["description"].startswith('"') and "small, fast model" in meta["description"]
    assert "Haiku" not in meta["description"] and "haiku" not in meta["description"].lower()


def test_the_driving_tools_are_real_tools_and_their_text_fits_the_budget():
    from tapwright import mcp_server
    import asyncio
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(driver.TOOLS) <= names
    spent = sum(len(describe(t)) for t in driver.TOOLS) / 4  # about four characters a token
    assert spent <= 3000, spent  # PLAN-1.6 Part D item 7: the driver's tool list at most 3,000 tokens
    assert set(driver.TOOLS) - {"alert", "open_url"} <= set(SHARED)  # the shared tools: both servers have them


def test_the_codex_file_is_valid_toml_with_the_same_text():
    data = toml(driver.render("codex"))
    assert data["name"] == "phone-driver" and data["model"] == "gpt-6.1-terra" and data["model_reasoning_effort"] == "low"
    assert data["developer_instructions"] == driver.handbook()
    assert "small, fast model" in data["description"]


def test_a_model_overrides_every_host_or_one():
    assert driver.models_for(["fable"], ["claude", "codex"]) == {"claude": "fable", "codex": "fable"}
    assert driver.models_for(["claude=sonnet", "codex=gpt-6.1-terra"], ["claude", "codex"]) == \
        {"claude": "sonnet", "codex": "gpt-6.1-terra"}
    assert split_claude(driver.render("claude", "sonnet"))[0]["model"] == "sonnet"
    assert toml(driver.render("codex", "gpt-x"))["model"] == "gpt-x"
    for bad in ("", "a b", 'x"y', "claude="):
        with pytest.raises(ValueError, match="isn't a model name"):
            driver.models_for([bad], ["claude"])


def test_install_writes_each_host_under_the_project_or_the_home(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    written = driver.install(["claude", "codex"], str(project))
    assert written == [project / ".claude/agents/phone-driver.md", project / ".codex/agents/phone-driver.toml"]
    assert all(b"\r" not in p.read_bytes() for p in written)  # (Path.read_text takes newline= only from Python 3.13)
    h = home(tmp_path)
    assert driver.install(["claude"], None, h) == [h / ".claude/agents/phone-driver.md"]
    assert not (h / ".codex").exists()


def test_install_replaces_its_own_file_and_leaves_someone_elses(tmp_path):
    h = home(tmp_path)
    driver.install(["claude"], None, h)
    driver.install(["claude"], None, h, ["sonnet"])  # a rerun with another model replaces it
    assert split_claude((h / ".claude/agents/phone-driver.md").read_text(encoding="utf-8"))[0]["model"] == "sonnet"
    theirs = h / ".codex/agents/phone-driver.toml"
    theirs.parent.mkdir(parents=True)
    theirs.write_text('name = "phone-driver"\n', encoding="utf-8")
    with pytest.raises(FileExistsError, match="Move it away first; nothing was written"):
        driver.install(["claude", "codex"], None, h, ["haiku"])
    assert theirs.read_text(encoding="utf-8") == 'name = "phone-driver"\n'
    assert split_claude((h / ".claude/agents/phone-driver.md").read_text(encoding="utf-8"))[0]["model"] == "sonnet"


def test_install_into_a_missing_project_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="is not a folder"):
        driver.install(["claude"], str(tmp_path / "missing"))


def test_install_client_claude_writes_the_driver_and_says_where(tmp_path):
    h = home(tmp_path)
    lines = connect.install("claude", ["a"], False, home=h, runner=FakeClaude(), which=lambda n: "claude")
    assert (h / ".claude/agents/phone-driver.md").is_file() and not (h / ".codex").exists()
    assert any("Driver agent installed" in x for x in lines) and any("(model haiku)" in x for x in lines)
    assert lines[-1].startswith("Next:")


def test_install_client_codex_writes_the_codex_agent_and_a_model_override(tmp_path):
    h = home(tmp_path)
    lines = connect.install("codex", ["a"], False, home=h, driver_model=["gpt-6.2-terra"])
    assert toml((h / ".codex/agents/phone-driver.toml").read_text(encoding="utf-8"))["model"] == "gpt-6.2-terra"
    assert not (h / ".claude/agents").exists() and any("(model gpt-6.2-terra)" in x for x in lines)


def test_install_client_cursor_has_no_agent_file_and_points_at_the_handbook(tmp_path):
    h = home(tmp_path)
    lines = connect.install("cursor", ["a"], False, home=h)
    assert not (h / ".claude/agents").exists() and not (h / ".codex/agents").exists()
    assert any("references/driver.md" in x for x in lines)


def test_a_bad_driver_model_changes_nothing(tmp_path):
    h = home(tmp_path)
    fake = FakeClaude()
    with pytest.raises(connect.ConnectError, match="nothing was changed"):
        connect.install("claude", ["a"], False, home=h, runner=fake, which=lambda n: "claude", driver_model=["a b"])
    assert fake.calls == [] and not list(h.iterdir())


def test_a_foreign_agent_file_stops_the_driver_but_not_the_server(tmp_path):
    h = home(tmp_path)
    theirs = h / ".claude/agents/phone-driver.md"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("mine", encoding="utf-8")
    with pytest.raises(connect.ConnectError, match="driver agent wasn't"):
        connect.install("claude", ["a"], False, home=h, runner=FakeClaude(), which=lambda n: "claude")
    assert theirs.read_text(encoding="utf-8") == "mine"


def test_skill_install_writes_both_agents_with_the_model_given(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tapwright", "skill", "install", "--project", str(tmp_path),
                                      "--driver-model", "claude=sonnet"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert split_claude((tmp_path / ".claude/agents/phone-driver.md").read_text(encoding="utf-8"))[0]["model"] == "sonnet"
    assert toml((tmp_path / ".codex/agents/phone-driver.toml").read_text(encoding="utf-8"))["model"] == "gpt-6.1-terra"
    assert "Driver agent installed" in out and out.index("Installed the tapwright skill:") < out.index("Driver agent")


def test_skill_install_with_a_bad_model_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tapwright", "skill", "install", "--project", str(tmp_path), "--driver-model", "x y"])
    assert cli.main() == 1
    assert "isn't a model name" in capsys.readouterr().out
    assert not (tmp_path / ".claude").exists()


def test_the_flag_is_in_both_commands_help():
    for command in (["skill"], ["install"]):
        out = subprocess.run([sys.executable, "-m", "tapwright", *command, "--help"], capture_output=True, text=True)
        assert out.returncode == 0 and "--driver-model" in out.stdout, command
