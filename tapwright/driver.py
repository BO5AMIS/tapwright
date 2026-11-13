"""The driver agent (PLAN-1.6 Part D): a subagent of the user's own agent, on that host's fastest small model.

No model runs inside Tapwright and no API key is asked for. `install` writes an agent definition the host
starts as a subagent: Claude Code's `.claude/agents/phone-driver.md`, Codex's `.codex/agents/phone-driver.toml`.
The instructions are the handbook in the skill (`references/driver.md`), so a host with no agent files can paste
the same text as a brief. The text never names a brand: "a small, fast model".

Claude Code lists an agent's tools as `mcp__<server>__<tool>`. Names of a server that isn't connected are dropped
when the agent starts (only the /agents screen warns), so one file lists both Tapwright's and Tapline's tools
and is the same file `tapline install` writes."""
import re
from pathlib import Path

from . import skills

NAME = "phone-driver"
SERVERS = ("tapwright", "tapline")  # the names `tapwright install` and `tapline install` register
HOSTS = ("claude", "codex")
MODELS = {"claude": "haiku", "codex": "gpt-6.1-terra"}  # each host's fastest small model today
EFFORT = "low"
# The driving tools only; `record` is there because the handbook has the driver save routes.
TOOLS = ("look", "tap", "type_text", "scroll", "toggle", "back", "swipe", "wait_for", "run", "drive", "map",
         "open_app", "open_url", "alert", "record", "session_report")
DESCRIPTION = ("Drives an iPhone app through Tapwright or Tapline for the main agent, on a small, fast model: give it a goal "
               "and the app, and it reaches it and answers in five lines. Keep one for the whole session. Use it on "
               "ground the map doesn't know; known ground is one `drive` call.")
MARKER = re.compile(r"^(?:# )?managed-by: (?:tapwright|tapline) install", re.M)


def handbook() -> str:
    """The driver's instructions: the skill's references/driver.md, the one copy of this text."""
    return (skills.SOURCE / "references" / "driver.md").read_text(encoding="utf-8").strip() + "\n"


def tool_names() -> list[str]:
    """Every driving tool under each server's prefix, in one list."""
    return [f"mcp__{server}__{tool}" for server in SERVERS for tool in TOOLS]


def render(host: str, model: str | None = None) -> str:
    """The agent file for `host` (`claude` or `codex`), on `model` or the host's own fast one."""
    model = model or MODELS[host]
    if host == "claude":
        return ("---\n"
                f"name: {NAME}\n"
                f"description: \"{DESCRIPTION}\"\n"
                f"model: {model}\n"
                f"effort: {EFFORT}\n"
                f"tools: {', '.join(tool_names())}\n"
                "managed-by: tapwright install (tapline install writes the same file)\n"
                "---\n\n" + handbook())
    if host == "codex":
        text = handbook()
        if '"""' in text or "\\" in text:  # a TOML multi-line basic string: the handbook must need no escapes
            raise ValueError("the handbook can't be written as a TOML multi-line string")
        return ("# managed-by: tapwright install (tapline install writes the same file)\n"
                f"name = \"{NAME}\"\n"
                f"description = \"{DESCRIPTION}\"\n"
                f"model = \"{model}\"\n"
                f"model_reasoning_effort = \"{EFFORT}\"\n"
                f"developer_instructions = \"\"\"\n{text}\"\"\"\n")
    raise ValueError(f"unknown host {host!r}; the hosts are {', '.join(HOSTS)}")


def target(host: str, project: str | None = None, home: Path | None = None) -> Path:
    """Where the host reads the agent: under the project's folder, else the user's home."""
    base = Path(project).expanduser() if project else (home or Path.home())
    return base / (".claude/agents/phone-driver.md" if host == "claude" else ".codex/agents/phone-driver.toml")


def is_ours(path: Path) -> bool:
    """Is this file one that `tapwright install` (or `tapline install`) wrote? Only such a file is replaced."""
    try:
        return MARKER.search(path.read_text(encoding="utf-8")) is not None
    except OSError:
        return False


def models_for(spec: list[str] | None, hosts: list[str]) -> dict[str, str]:
    """The model per host from `--driver-model` values: `NAME` for every host written, `HOST=NAME` for one."""
    models: dict[str, str] = {}
    for value in spec or []:
        host, sep, name = value.partition("=")
        host, name = (host.strip().lower(), name.strip()) if sep and host.strip().lower() in HOSTS else ("", value.strip())
        if not name or not re.fullmatch(r"[\w.:/+-]+", name):
            raise ValueError(f"--driver-model {value!r} isn't a model name (like haiku, or codex=gpt-6.1-terra)")
        for h in ([host] if host else hosts):
            models[h] = name
    return models


def install(hosts: list[str], project: str | None = None, home: Path | None = None,
            driver_model: list[str] | None = None) -> list[Path]:
    """Write the driver for each host and return the files written. A file of ours is replaced; a file of that
    name that is something else is left alone, with an error, before anything is written. Raises OSError
    (FileNotFoundError, FileExistsError) with the sentence to print, and ValueError for a bad model name."""
    models = models_for(driver_model, hosts)
    if project and not Path(project).expanduser().is_dir():
        raise FileNotFoundError(f"{Path(project).expanduser()} is not a folder. Pass the project's folder to --project.")
    where = [(host, target(host, project, home)) for host in hosts]
    for _, path in where:
        if path.exists() and not is_ours(path):
            raise FileExistsError(f"{path} exists and is not the {NAME} agent Tapwright writes. "
                                  "Move it away first; nothing was written.")
    written: list[Path] = []
    for host, path in where:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(host, models.get(host)), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def installed_lines(written: list[Path], hosts: list[str], driver_model: list[str] | None = None) -> list[str]:
    """What install prints about the driver: where it is and what model it is on."""
    models = models_for(driver_model, hosts)
    lines = ["Driver agent installed (a subagent on the host's fast small model; no model runs in Tapwright):"]
    lines.extend(f"  {path}  (model {models.get(host, MODELS[host])})" for host, path in zip(hosts, written))
    lines.append("  Ask your agent to hand it a goal; it is for ground the map doesn't know. --driver-model NAME changes the model.")
    return lines
