"""`tapwright install --client …`: the config edits, the refusal, idempotence, the fake `claude`."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tapwright import cli, connect

ARGS = ["mcp", "--apps", "com.example.app"]


class FakeClaude:
    """Stands in for `claude`: records each call, fails `add` when told to."""

    def __init__(self, add_code: int = 0):
        self.calls: list[tuple[list[str], Path | None]] = []
        self.add_code = add_code

    def __call__(self, cmd, cwd):
        self.calls.append((cmd, cwd))
        code = self.add_code if cmd[2] == "add" else 1  # `remove` fails when nothing is registered
        return subprocess.CompletedProcess(cmd, code, "", "boom" if code else "")


def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir(exist_ok=True)
    return tmp_path / "home"


def test_it_refuses_without_apps_or_dedicated(tmp_path):
    with pytest.raises(connect.ConnectError, match="--apps"):
        connect.install("claude", [], False, home=home(tmp_path), runner=FakeClaude(), which=lambda n: "claude")
    with pytest.raises(connect.ConnectError, match="together"):
        connect.install("codex", ["a"], True, home=home(tmp_path))
    assert not list(home(tmp_path).iterdir())  # nothing written


def test_claude_removes_then_adds_and_installs_the_skill(tmp_path):
    fake = FakeClaude()
    lines = connect.install("claude", ["com.example.app", "com.b"], False, home=home(tmp_path), runner=fake,
                            which=lambda n: "/bin/claude")
    assert [c[0] for c in fake.calls] == [
        ["/bin/claude", "mcp", "remove", "tapwright", "--scope", "user"],
        ["/bin/claude", "mcp", "add", "--scope", "user", "tapwright", "--", "tapwright", "mcp", "--apps",
         "com.example.app,com.b"]]
    assert (home(tmp_path) / ".claude" / "skills" / "tapwright" / "SKILL.md").is_file()
    assert any("claude mcp add" in ln for ln in lines) and any(".agents" in ln for ln in lines)


def test_claude_project_scope_runs_in_the_project(tmp_path):
    fake = FakeClaude()
    project = tmp_path / "proj"
    project.mkdir()
    connect.install("claude", [], True, str(project), home(tmp_path), fake, lambda n: "claude")
    assert all(call[1] == project for call in fake.calls)
    assert fake.calls[1][0][-2:] == ["mcp", "--dedicated"] and "project" in fake.calls[1][0]
    assert (project / ".claude" / "skills" / "tapwright").is_dir()


def test_claude_missing_prints_the_exact_command(tmp_path):
    with pytest.raises(connect.ConnectError) as e:
        connect.install("claude", ["com.example.app"], False, home=home(tmp_path), which=lambda n: None)
    assert "claude mcp add --scope user tapwright -- tapwright mcp --apps com.example.app" in str(e.value)


def test_claude_add_failure_is_reported(tmp_path):
    with pytest.raises(connect.ConnectError, match="boom"):
        connect.install("claude", ["a"], False, home=home(tmp_path), runner=FakeClaude(add_code=1),
                        which=lambda n: "claude")


def test_codex_appends_and_keeps_other_servers(tmp_path):
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    original = 'model = "x"\n\n# my servers\n[mcp_servers.other]\ncommand = "other"\nargs = ["--a"]\n'
    config.write_text(original, encoding="utf-8")
    connect.install("codex", ["com.example.app"], False, home=h)
    text = config.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.endswith('[mcp_servers.tapwright]\ncommand = "tapwright"\nargs = ["mcp", "--apps", "com.example.app"]\n')
    assert (h / ".agents" / "skills" / "tapwright").is_dir()


def test_codex_updates_in_place_without_duplicating(tmp_path):
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '[mcp_servers.first]\ncommand = "a"\n\n'
        '[mcp_servers.tapwright]\ncommand = "tapwright"\nargs = ["mcp", "--apps", "old"]\nstartup_timeout_sec = 30\n\n'
        '[mcp_servers.tapwright.env]\nX = "1"\n\n'
        '[mcp_servers.last]\ncommand = "z"\n', encoding="utf-8")
    connect.install("codex", ["new1", "new2"], False, home=h)
    connect.install("codex", ["new1", "new2"], False, home=h)  # twice: the same file
    text = config.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.tapwright]") == 1
    assert 'args = ["mcp", "--apps", "new1,new2"]' in text and "old" not in text
    assert "startup_timeout_sec = 30" in text and '[mcp_servers.tapwright.env]\nX = "1"' in text
    assert text.startswith('[mcp_servers.first]\ncommand = "a"\n\n[mcp_servers.tapwright]')
    assert text.endswith('[mcp_servers.last]\ncommand = "z"\n')
    assert "\n\n\n" not in text
    try:
        import tomllib
    except ImportError:
        return
    servers = tomllib.loads(text)["mcp_servers"]
    assert set(servers) == {"first", "tapwright", "last"} and servers["tapwright"]["env"] == {"X": "1"}


def test_codex_dedicated_and_empty_file(tmp_path):
    h = home(tmp_path)
    connect.install("codex", [], True, home=h)
    text = (h / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert text == '[mcp_servers.tapwright]\ncommand = "tapwright"\nargs = ["mcp", "--dedicated"]\n'


def test_codex_refuses_a_broken_file_and_leaves_it(tmp_path):
    pytest.importorskip("tomllib")
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("this is = = not toml\n", encoding="utf-8")
    with pytest.raises(connect.ConnectError, match="TOML"):
        connect.install("codex", ["a"], False, home=h)
    assert config.read_text(encoding="utf-8") == "this is = = not toml\n"


def test_cursor_keeps_other_servers_and_keys_and_is_idempotent(tmp_path):
    h = home(tmp_path)
    config = h / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "extra": 1}), encoding="utf-8")
    for _ in range(2):
        connect.install("cursor", ["com.example.app"], False, home=h)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["extra"] == 1 and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["tapwright"] == {"command": "tapwright", "args": ARGS}
    assert len(data["mcpServers"]) == 2


def test_cursor_project_file_and_refusing_bad_json(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    connect.install("cursor", [], True, str(project), home(tmp_path))
    assert json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["tapwright"][
        "args"] == ["mcp", "--dedicated"]
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".cursor").mkdir()
    (bad / ".cursor" / "mcp.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(connect.ConnectError, match="JSON"):
        connect.install("cursor", ["a"], False, str(bad), home(tmp_path))
    assert (bad / ".cursor" / "mcp.json").read_text(encoding="utf-8") == "{nope"


def test_the_command_refuses_and_prints(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tapwright", "install", "--client", "cursor"])
    assert cli.main() == 1
    assert "--apps" in capsys.readouterr().out


def test_install_command_is_listed():
    out = subprocess.run([sys.executable, "-m", "tapwright", "install", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and all(f in out.stdout for f in ("--client", "--apps", "--dedicated", "--project"))


# ---------- M5: policy flags, kept flags, file modes, symlinks ----------

def policy_file(tmp_path: Path) -> Path:
    p = tmp_path / "policy.json"
    p.write_text('{"confirm": [], "block": ["Delete"]}', encoding="utf-8")
    return p


def codex_args(config: Path) -> list[str]:
    import re
    m = re.search(r"^args = (\[.*\])$", config.read_text(encoding="utf-8"), re.M)
    assert m is not None
    return json.loads(m.group(1))


def test_policy_and_no_policy_are_exclusive(tmp_path):
    with pytest.raises(connect.ConnectError, match="together"):
        connect.install("codex", ["a"], False, home=home(tmp_path), policy=str(policy_file(tmp_path)), no_policy=True)
    with pytest.raises(connect.ConnectError, match="doesn't exist"):
        connect.install("codex", ["a"], False, home=home(tmp_path), policy=str(tmp_path / "missing.json"))


def test_install_passes_the_policy_through_for_every_client(tmp_path):
    pol = str(policy_file(tmp_path).resolve())
    fake = FakeClaude()
    connect.install("claude", ["a"], False, home=home(tmp_path), runner=fake, which=lambda n: "claude", policy=pol)
    assert fake.calls[1][0][-4:] == ["--apps", "a", "--policy", pol]
    connect.install("codex", ["a"], False, home=home(tmp_path), policy=pol)
    assert codex_args(home(tmp_path) / ".codex" / "config.toml") == ["mcp", "--apps", "a", "--policy", pol]
    connect.install("cursor", ["a"], False, home=home(tmp_path), no_policy=True)
    data = json.loads((home(tmp_path) / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["tapwright"]["args"] == ["mcp", "--apps", "a", "--no-policy"]


def test_a_rerun_keeps_policy_and_screenshots_for_codex(tmp_path):
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.tapwright]\ncommand = "tapwright"\n'
                      'args = ["mcp", "--apps", "old", "--policy", "/p/pol.json", "--screenshots", "/shots"]\n',
                      encoding="utf-8")
    lines = connect.install("codex", ["new"], False, home=h)
    assert codex_args(config) == ["mcp", "--apps", "new", "--policy", "/p/pol.json", "--screenshots", "/shots"]
    assert not any("replaced" in ln for ln in lines)


def test_a_rerun_keeps_no_policy_for_cursor(tmp_path):
    h = home(tmp_path)
    config = h / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"tapwright": {"command": "tapwright",
                                                               "args": ["mcp", "--apps", "x", "--no-policy"]}}}),
                      encoding="utf-8")
    connect.install("cursor", ["y"], False, home=h)
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["tapwright"]["args"] == [
        "mcp", "--apps", "y", "--no-policy"]


def test_replacing_a_policy_is_said_in_the_output(tmp_path):
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.tapwright]\ncommand = "tapwright"\n'
                      'args = ["mcp", "--apps", "a", "--policy", "/p/old.json"]\n', encoding="utf-8")
    lines = connect.install("codex", ["a"], False, home=h, no_policy=True)
    assert any("replaced the earlier --policy /p/old.json" in ln for ln in lines)
    assert codex_args(config) == ["mcp", "--apps", "a", "--no-policy"]


def test_claude_rerun_keeps_flags_from_the_existing_entry(tmp_path):
    h = home(tmp_path)
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {"tapwright": {
        "command": "tapwright", "args": ["mcp", "--apps", "a", "--policy", "/p/pol.json"]}}}), encoding="utf-8")
    fake = FakeClaude()
    connect.install("claude", ["b"], False, home=h, runner=fake, which=lambda n: "claude")
    assert fake.calls[1][0][-5:] == ["mcp", "--apps", "b", "--policy", "/p/pol.json"]


def test_merge_flags_unit():
    old = ["mcp", "--dedicated", "--no-policy", "--screenshots=/s"]
    args, notes = connect.merge_flags(old, [], True, None, False)
    assert args == ["mcp", "--dedicated", "--no-policy", "--screenshots", "/s"] and notes == []
    args, notes = connect.merge_flags(old, [], True, "/p.json", False)
    assert args == ["mcp", "--dedicated", "--policy", "/p.json", "--screenshots", "/s"]
    assert notes == ["replaced the earlier --no-policy"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_config_keeps_its_permission_mode(tmp_path):
    h = home(tmp_path)
    config = h / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "x"\n', encoding="utf-8")
    config.chmod(0o600)
    connect.install("codex", ["a"], False, home=h)
    assert (config.stat().st_mode & 0o777) == 0o600


def test_a_symlinked_config_is_written_through(tmp_path):
    h = home(tmp_path)
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text('model = "x"\n', encoding="utf-8")
    link = h / ".codex" / "config.toml"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks aren't allowed here")
    connect.install("codex", ["a"], False, home=h)
    assert link.is_symlink()
    assert "[mcp_servers.tapwright]" in real.read_text(encoding="utf-8")


def test_install_command_takes_policy_flags(monkeypatch):
    out = subprocess.run([sys.executable, "-m", "tapwright", "install", "--help"], capture_output=True, text=True)
    assert "--policy" in out.stdout and "--no-policy" in out.stdout
    monkeypatch.setattr(sys, "argv", ["tapwright", "install", "--client", "cursor", "--apps", "a",
                                      "--policy", "p.json", "--no-policy"])
    with pytest.raises(SystemExit):
        cli.main()


def test_install_keeps_and_replaces_the_ux_rules_file(tmp_path):
    rules = tmp_path / "ux.json"
    rules.write_text('{"rules": [{"way_back": true}]}', encoding="utf-8")
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    config = home(tmp_path) / ".codex" / "config.toml"
    connect.install("codex", ["a"], False, home=home(tmp_path), ux_rules=str(rules))
    assert codex_args(config) == ["mcp", "--apps", "a", "--ux-rules", str(rules.resolve())]
    connect.install("codex", ["b"], False, home=home(tmp_path))  # a rerun keeps it
    assert codex_args(config) == ["mcp", "--apps", "b", "--ux-rules", str(rules.resolve())]
    lines = connect.install("codex", ["b"], False, home=home(tmp_path), ux_rules=str(other))
    assert codex_args(config)[-1] == str(other.resolve()) and any("replaced the earlier --ux-rules" in x for x in lines)
    with pytest.raises(connect.ConnectError, match="ux rules file"):
        connect.install("codex", ["a"], False, home=home(tmp_path), ux_rules=str(tmp_path / "missing.json"))
