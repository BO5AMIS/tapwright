# Tapwright quickstart (1.7.5)

Follow [setup](setup.md) to install Tapwright, connect the iPhone and sign/install its runner. Windows, Linux and macOS are supported; no iOS simulator is required.

```sh
pip install tapwright==1.7.5
tapwright doctor
tapwright install --client codex
```

Use a pinned environment for unattended runs. If setup requires Apple trust, signing or an XCTest passcode prompt, complete that platform step before QA.

Register `tapwright mcp` in other MCP hosts. App allowlists and dedicated mode are no longer required. Existing `--apps`, `--dedicated`, `--policy` and `--no-policy` arguments are compatibility options and do not restrict actions.

Ask the agent to open an app, inspect it and complete your workflow. It should look, act, read the full response and verify the result. Stable ids and fresh refs work for ordinary controls; screenshot-grounded coordinates support UI missing from accessibility. Use `run` with expectations for known steps and `record`/`replay` for repeatable QA.

System alerts can be read and answered with `alert`, including an exact localized `name`. `dry_run` sends no input. Home, Settings and cross-app flows are available. Permission judgment belongs to the agent, based on your request and existing authorization.

Pause in the live view stops input. Missing or stale targets, concurrent phone use and genuine Apple platform gates still produce errors. On timeout inspect whether the action happened before repeating it.

See [the control contract and migration notes](agent-directed-control.md) for 1.7.5 behavior.
