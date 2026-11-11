"""`tapwright install --client claude|codex|cursor`: connect an agent in one command.

It registers the MCP server (`tapwright mcp --apps …`) with the agent's own configuration and
installs the skill. Running it again updates the entry; it never adds a second one, and it keeps
every other server in the file.

* Claude Code: through its own `claude mcp add` (we remove our old entry first).
* Codex: the `[mcp_servers.tapwright]` table in `~/.codex/config.toml`, edited as text so the
  other tables, comments and order stay as they were.
* Cursor: the `tapwright` key of `mcpServers` in `~/.cursor/mcp.json` (`.cursor/mcp.json` in a project)."""
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

NAME = "tapwright"
CLIENTS = ("claude", "codex", "cursor")

Runner = Callable[[list[str], Path | None], "subprocess.CompletedProcess[str]"]


class ConnectError(RuntimeError):
    """Something the user can fix; the message says what."""


def mcp_args(apps: list[str], dedicated: bool) -> list[str]:
    """The arguments after `tapwright`: `mcp --apps a,b` or `mcp --dedicated`."""
    return ["mcp", "--dedicated"] if dedicated else ["mcp", "--apps", ",".join(apps)]


def check_scope(apps: list[str], dedicated: bool) -> None:
    """The rule of `tapwright mcp`: personal mode needs the apps the agent may use; without them it can do nothing."""
    if apps and dedicated:
        raise ConnectError("--apps and --dedicated can't be used together.")
    if not apps and not dedicated:
        raise ConnectError(
            "say which apps the agent may use: --apps com.example.app (bundle ids or exact names, comma-separated). "
            "Without it the agent can do nothing, which is deliberate: the phone may be yours. On a phone kept "
            "for testing, with no personal accounts on it, --dedicated lets it use every app.")


# ---------- Claude Code ----------

def claude_commands(args: list[str], project: str | None) -> tuple[list[str], list[str]]:
    """(remove, add) for `claude mcp`. User scope normally; with a project, that project's `.mcp.json`."""
    scope = ["--scope", "project" if project else "user"]
    return (["mcp", "remove", NAME, *scope], ["mcp", "add", *scope, NAME, "--", NAME, *args])


def _run(cmd: list[str], cwd: Path | None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)


def install_claude(args: list[str], project: str | None, runner: Runner = _run,
                   which: Callable[[str], str | None] = shutil.which) -> str:
    """Register with Claude Code. Without `claude` on the PATH, say the exact command instead."""
    remove, add = claude_commands(args, project)
    cwd = Path(project).expanduser() if project else None
    exe = which("claude")
    if exe is None:
        raise ConnectError("`claude` isn't on the PATH, so nothing was registered. Run this where Claude Code is "
                           "installed:\n  " + " ".join(["claude", *add]))
    runner([exe, *remove], cwd)  # fails when there is no entry yet: fine
    done = runner([exe, *add], cwd)
    if done.returncode != 0:
        raise ConnectError(f"`claude mcp add` failed: {(done.stderr or done.stdout).strip()[-400:]}\n"
                           "The command was:\n  " + " ".join(["claude", *add]))
    where = f"{cwd / '.mcp.json'}" if cwd else "your Claude Code settings (user scope)"
    return f"Claude Code: registered the tapwright server in {where} with `claude mcp add`."


# ---------- Codex (TOML, edited as text) ----------

def _toml_string(value: str) -> str:
    return json.dumps(value)  # a TOML basic string takes the same escapes as JSON for these inputs


def codex_table(args: list[str]) -> str:
    return ("[mcp_servers.tapwright]\n"
            f"command = {_toml_string(NAME)}\n"
            f"args = [{', '.join(_toml_string(a) for a in args)}]\n")


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _table_name(line: str) -> str | None:
    m = _HEADER.match(line)
    return m.group(1).replace('"', "").replace("'", "").replace(" ", "") if m else None


def update_codex_toml(text: str, args: list[str]) -> str:
    """`text` with the [mcp_servers.tapwright] table set to our command. The table's own `command`/`args` lines
    are replaced and anything else in it (like `env`, or a sub-table) stays; with no such table, ours is appended.
    Every other table is left byte for byte."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if _table_name(ln) == "mcp_servers.tapwright"), None)
    if start is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + codex_table(args)
    end = next((i for i in range(start + 1, len(lines)) if _table_name(lines[i]) is not None), len(lines))
    kept = [ln for ln in lines[start + 1:end] if not re.match(r"^\s*(command|args)\s*=", ln)]
    while kept and not kept[-1].strip():  # blank lines that separated this table from the next stay at its end
        kept.pop()
    body = codex_table(args).splitlines(keepends=True)
    gap = ["\n"] if end < len(lines) else []
    return "".join(lines[:start] + body + kept + gap + lines[end:])


def _check_toml(text: str) -> None:
    try:
        import tomllib
    except ImportError:  # Python 3.10: no parser to check with
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConnectError(f"the Codex config would not be valid TOML after the edit ({e}); nothing was changed.") from e


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tapwright-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def install_codex(args: list[str], config: Path) -> str:
    try:
        old = config.read_text(encoding="utf-8") if config.exists() else ""
    except OSError as e:
        raise ConnectError(f"can't read {config}: {e}") from e
    if old:
        _check_toml(old)  # never edit a file that already isn't valid
    new = update_codex_toml(old, args)
    _check_toml(new)
    _write_atomically(config, new)
    return f"Codex: wrote [mcp_servers.tapwright] in {config}. Start a new Codex session to pick it up."


# ---------- Cursor (JSON) ----------

def update_cursor_json(text: str, args: list[str]) -> str:
    """`text` (a mcp.json, or empty) with mcpServers.tapwright set. Other keys and servers stay; a file that
    isn't a JSON object is refused rather than overwritten."""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise ConnectError(f"the Cursor config isn't valid JSON ({e}); fix or move it, nothing was changed.") from e
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ConnectError("the Cursor config isn't the expected {\"mcpServers\": {...}} object; nothing was changed.")
    data.setdefault("mcpServers", {})[NAME] = {"command": NAME, "args": args}
    return json.dumps(data, indent=2) + "\n"


def install_cursor(args: list[str], config: Path) -> str:
    try:
        old = config.read_text(encoding="utf-8") if config.exists() else ""
    except OSError as e:
        raise ConnectError(f"can't read {config}: {e}") from e
    _write_atomically(config, update_cursor_json(old, args))
    return f"Cursor: wrote mcpServers.tapwright in {config}. Restart Cursor, or toggle the server in its MCP settings."


# ---------- the command ----------

def config_path(client: str, project: str | None, home: Path | None = None) -> Path | None:
    """Where the client's MCP config lives (None for Claude Code, which has its own command)."""
    base = Path(project).expanduser() if project else (home or Path.home())
    if client == "codex":
        return (home or Path.home()) / ".codex" / "config.toml"  # Codex reads only the user's file
    if client == "cursor":
        return base / ".cursor" / "mcp.json"
    return None


def install(client: str, apps: list[str], dedicated: bool, project: str | None = None, home: Path | None = None,
            runner: Runner = _run, which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Register the server with `client` and install the skill. Returns the lines to print."""
    from . import skills
    check_scope(apps, dedicated)
    args = mcp_args(apps, dedicated)
    if client == "claude":
        lines = [install_claude(args, project, runner, which)]
    elif client == "codex":
        lines = [install_codex(args, config_path("codex", project, home))]
    elif client == "cursor":
        lines = [install_cursor(args, config_path("cursor", project, home))]
    else:
        raise ConnectError(f"unknown client {client!r}; the clients are {', '.join(CLIENTS)}")
    lines.append(f"  server command: tapwright {' '.join(args)}")
    if project and client == "codex":
        lines.append("  (Codex reads one config, in your home folder, so --project only decides where the skill goes.)")
    try:
        written = skills.install(project, home)
    except OSError as e:
        raise ConnectError(f"the server is registered, but the skill wasn't installed: {e}") from e
    lines.append("Skill installed:")
    lines.extend(f"  {path}" for path in written)
    lines.append("Next: start a new session in your agent and ask it to open one of your apps on the phone.")
    return lines
