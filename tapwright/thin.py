"""`tapwright call <tool> '<json>'` (docs/AGENT-TOOLS.md §18.10): one agent tool, sent to the running `tapwright serve`,
for a host that can't keep an MCP connection open.

This module loads the Python standard library and nothing else, not even the rest of the package: a host that starts a
process for every call pays for each import, and a test (tests/test_thin.py) fails the build if anything more is
imported. So the few lines it shares with store.py (where serve.json is) and client.py (the headers) are copied here.

The call never starts a daemon: that is `tapwright serve`'s job, and an agent writing a command line must not be able
to start one with a wider scope than its owner chose (the daemon's flags are a ceiling a call can only narrow).
Exit codes: 0 the answer; 2 a refusal or a mistake in the command line; 3 the daemon couldn't be reached."""
import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import json
import math
import os
import secrets
import socket
import sys
import tempfile
import urllib.parse
from pathlib import Path

EXIT_REFUSED, EXIT_UNREACHABLE = 2, 3
CALL_TIMEOUT = 120.0  # seconds `tapwright call` waits for an answer by default (as mcp_server.TIMEOUT)
MEDIA_CALL_TIMEOUT = 900.0  # media: a video is copied, then imported (as mcp_server.MEDIA_TIMEOUT)
# the longer waits mcp_server._timeout gives (copied: see the docstring; tests/test_thin.py checks they still agree)
LONG_CALL_TIMEOUT = 200.0  # run, replay, drive (mcp_server.LONG_TIMEOUT)
EXPORT_CALL_TIMEOUT = 600.0  # a journey's stop with export (mcp_server.EXPORT_TIMEOUT)
APPLY_CALL_TIMEOUT = 480.0  # replay with repair "apply": the run, then the repaired file's own replay (scriptrepair.APPLY_TIMEOUT)
EXPLORE_MINUTES = 10.0  # explore's default budget (explore.MINUTES)
EXPLORE_MARGIN = 60 + 5 * 60  # a minute for the report and a person's waits (exploretool.timeout_for)
FLOOR_TOOLS = ("explore", "replay", "bug_report")  # the calls whose own moves pass the skip floor, as server.FLOOR_TOOLS (replay with repair)
BUG_BASE, BUG_RUN, BUG_RUNS, BUG_EXPORT = 120.0, 60.0 + 60.0 + 2 * 30.0, 3, 600.0  # bug_report: bugcheck.timeout_for (3 runs
# by default; each run with its two launch waits, review R185b M1)
NO_DAEMON = "tapwright serve isn't running (or its address file is unreadable): start it with `tapwright serve`."


def default_timeout(tool: str, args: dict) -> float:
    """How long a call waits for its answer when `--timeout` isn't given: as long as the MCP server waits for the same call
    (mcp_server._timeout), so an explore of 10 minutes or a repair's apply isn't cut off at 2 minutes while it goes on
    (review R180b M3)."""
    if tool == "journey" and args.get("stop") and args.get("export") is not None:
        return EXPORT_CALL_TIMEOUT
    if tool == "explore":  # its time budget, a minute for the report, and a person's waits
        minutes = args.get("minutes")
        finite = isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and math.isfinite(minutes)
        return (float(minutes) if finite else EXPLORE_MINUTES) * 60 + EXPLORE_MARGIN
    if tool == "bug_report":  # its `verify` runs and its export (bugcheck.timeout_for)
        verify = args.get("verify")
        runs = BUG_RUNS if verify is True else verify if isinstance(verify, int) and not isinstance(verify, bool) else 0
        if verify is None and (args.get("finding") is not None or args.get("from") == "failure"):
            runs = BUG_RUNS
        return BUG_BASE + runs * BUG_RUN + (BUG_EXPORT if args.get("export") is not None else 0.0)
    if tool == "replay" and args.get("repair") == "apply":
        return APPLY_CALL_TIMEOUT
    if tool == "media":
        return MEDIA_CALL_TIMEOUT
    return LONG_CALL_TIMEOUT if tool in ("run", "replay", "drive") else CALL_TIMEOUT


def floor_call(tool: str, args: dict) -> bool:
    """Does this call's own moves pass the skip floor (explore; replay with repair), so `--policy` reaches it (as server.py
    reads X-Tapwright-Policy for these calls only)?"""
    return tool in FLOOR_TOOLS and (tool != "replay" or args.get("repair") is not None)


def _home() -> Path:
    """Where tapwright keeps things, as store.HOME does (read at the call, not at import)."""
    return Path(os.environ.get("TAPWRIGHT_HOME") or Path.home() / ".tapwright")


def _serve_info() -> dict:
    try:
        info = json.loads((_home() / "serve.json").read_text())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _json_line(path: str, what: str) -> str:
    """A JSON file as one line of ASCII (fit for a header). The daemon checks that it is a policy, or house rules."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
        return json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=True)
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read the {what} file {path}: {getattr(e, 'strerror', None) or e}") from None
    except (ValueError, RecursionError) as e:
        raise ValueError(f"the {what} file {path} isn't JSON: {e}") from None


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tapwright call", description="Run one tool in the running `tapwright serve` and print its answer.",
        epilog="Exit codes: 0 the answer, 2 a refusal (its error line is on stderr) or a mistake in the command, "
               "3 tapwright serve can't be reached. The scope, policy and the other flags of `tapwright serve` are a "
               "ceiling: a call may narrow them (a smaller --apps, a stricter --policy), never widen them.")
    ap.add_argument("tool", help="the tool's name, e.g. look, tap, run")
    ap.add_argument("arguments", nargs="?", default="{}", metavar="JSON",
                    help="the tool's arguments as one JSON object (default {}); - reads them from standard input")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--apps", metavar="APP[,APP]",
                       help='Deprecated compatibility metadata; all apps and system screens are accessible.')
    scope.add_argument("--dedicated", action="store_true",
                       help='Deprecated compatibility flag; whole-phone control is the default.')
    scope.add_argument("--phone", action="store_true",
                       help='Compatibility mode metadata; configure automatic unlock on the daemon with tapwright serve --phone.')
    ap.add_argument("--policy", metavar="FILE", help='Deprecated compatibility option; policies and grants no longer restrict actions.')
    ap.add_argument("--ux-rules", metavar="FILE", help="the user's house rules for ux_check and journeys")
    ap.add_argument("--maps", metavar="DIR", help="where the per-app maps (<DIR>/<bundle id>/map.json) are kept, to share "
                                                   "them with a project; never another folder than serve's --maps")
    ap.add_argument("--accounts", metavar="FILE", help="the test accounts file serve was started with; naming another is "
                                                       "refused (it is set when serve starts, not per call)")
    ap.add_argument("--client", metavar="NAME", default=None,
                    help="this agent's name, so its report and journey stay its own (default: the parent process id)")
    ap.add_argument("--no-hints", action="store_true",
                    help="1.8.5's answers and behaviour: no Known: or Hint: lines, no route that saves itself, no Done press "
                         "(also TAPWRIGHT_NO_HINTS=1)")
    ap.add_argument("--json", action="store_true", help='print {"text", "image_path"} instead of the text')
    ap.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                    help=f"how long to wait for the answer (default {CALL_TIMEOUT:g}; media {MEDIA_CALL_TIMEOUT:g}: a "
                         "video is copied to the phone, then imported)")
    return ap


def _headers(a: argparse.Namespace, token: str, floor: bool = False) -> dict[str, str]:
    """`floor`: the call is one whose own moves pass the skip floor (`floor_call`): a `--policy` file's words reach it (they
    only add to what those moves never touch, over serve's own); on any other call `--policy` restricts nothing."""
    client = a.client or str(os.getppid())
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
         "X-Agent-Client": client, "X-Tapwright-Client": client,  # the shared name; the old one for an older daemon
         "X-Tapwright-Via": "call"}  # session_report counts these: each is a new process
    if getattr(a, "phone", False):
        h["X-Tapwright-Mode"] = "phone"
    elif a.dedicated:
        h["X-Tapwright-Mode"] = "dedicated"
    else:
        apps = a.apps if a.apps is not None else os.environ.get("TAPWRIGHT_APPS", "")
        if apps.strip():
            h["X-Tapwright-Apps"] = apps
    if a.policy and floor:
        h["X-Tapwright-Policy"] = _json_line(a.policy, "policy")
    elif a.policy:
        print('Tapwright provides whole-phone control. --apps, --dedicated, --policy and --no-policy are compatibility options, not access restrictions; permission decisions belong to the agent.', file=sys.stderr)
    if getattr(a, "no_hints", False) or os.environ.get("TAPWRIGHT_NO_HINTS", "").strip().lower() in ("1", "true", "yes", "on"):
        h["X-Tapwright-Hints"] = "off"  # (1.9.0, AGENT-TOOLS §30.4)
    if a.ux_rules:
        h["X-Tapwright-Ux-Rules"] = _json_line(a.ux_rules, "ux rules")
    if a.maps:
        h["X-Tapwright-Maps"] = str(Path(a.maps).expanduser().resolve())
    if a.accounts:
        h["X-Tapwright-Accounts"] = str(Path(a.accounts).expanduser().resolve())
    if not all(v.isascii() for v in h.values()):  # HTTP carries Latin-1 at best: percent-encode them all, and say so
        h = {k: v if k in ("Authorization", "Content-Type") else urllib.parse.quote(v, safe="") for k, v in h.items()}
        h["X-Tapwright-Encoding"] = "percent"
    return h


PROOF_PREFIX = b"tapwright-proof-v2\x00"  # as server.PROOF_PREFIX and client.PROOF_PREFIX (copied: see the docstring)


def _proved(port: int, token: str, timeout: float) -> http.client.HTTPConnection | None:
    """Does whatever listens on `port` hold `token`? A random challenge goes out without the token; the answer must be
    HMAC-SHA256(token, prefix + challenge), compared in constant time. OSError when nothing answers.
    On success returns the connection that was proved, still open: the token goes over that same connection and never
    over a new one (a process that bound the port in between would get it). It never reconnects by itself (auto_open
    off). None when the proof is wrong, or the line was closed after it."""
    challenge = secrets.token_bytes(32)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", "/api/prove", body=json.dumps({"challenge": challenge.hex()}).encode(),
                     headers={"Content-Type": "application/json"})
        sock = conn.sock
        reply = conn.getresponse()
        raw = reply.read()
        given = None
        if reply.status == 200:
            try:
                given = json.loads(raw or b"{}").get("proof")
            except (ValueError, AttributeError):
                given = None
        # signed with the port this client dialled: a proof relayed from a daemon on another port never matches
        # (the address this call dials: the literal 127.0.0.1, never a name that could resolve elsewhere)
        signed = PROOF_PREFIX + challenge + b"\x00127.0.0.1\x00" + str(int(port)).encode()
        want = hmac.new(token.encode(), signed, hashlib.sha256).hexdigest()
        if not (isinstance(given, str) and hmac.compare_digest(given.encode(), want.encode())):
            conn.close()
            return None
        if sock is None or conn.sock is not sock:  # closed after the proof: not the proved peer any more
            conn.close()
            return None
        conn.auto_open = 0
        return conn
    except BaseException:
        conn.close()
        raise


def _picture(answer: dict) -> str | None:
    """The answer's picture (base64 JPEG) in a temporary file; its path."""
    raw = answer.get("image")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    fd, path = tempfile.mkstemp(prefix="tapwright-", suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _utf8() -> None:
    """The console's code page (cp1252 on Windows) can't write every answer; the text is UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


def _stdin_text() -> str:
    """Standard input as UTF-8 (a leading BOM dropped), whatever the console's code page: on Windows `sys.stdin` decodes
    with cp1252, so piped Arabic or Japanese arrived as mojibake and was typed that way."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # already text (a test's StringIO, an embedding host)
        return sys.stdin.read()
    return buffer.read().decode("utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    _utf8()
    a = _parser().parse_args(argv)
    try:
        raw = _stdin_text() if a.arguments == "-" else a.arguments
    except UnicodeDecodeError as e:
        print(f"tapwright call: standard input isn't UTF-8 ({e.reason} at byte {e.start}); send the JSON as UTF-8",
              file=sys.stderr)
        return EXIT_REFUSED
    try:
        args = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError) as e:
        print(f"tapwright call: the arguments aren't JSON: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if not isinstance(args, dict):
        print("tapwright call: the arguments must be one JSON object, like '{\"target\": {\"id\": \"save\"}}'",
              file=sys.stderr)
        return EXIT_REFUSED
    if a.timeout is None:
        a.timeout = default_timeout(a.tool, args)
    info = _serve_info()
    port, token = info.get("port"), info.get("token")
    if not isinstance(port, int) or not isinstance(token, str) or not token:
        print(f"tapwright call: {NO_DAEMON}", file=sys.stderr)
        return EXIT_UNREACHABLE
    try:
        headers = _headers(a, token, floor_call(a.tool, args))
    except ValueError as e:
        print(f"tapwright call: {e}", file=sys.stderr)
        return EXIT_REFUSED
    try:  # the token goes only to a process that has proved it holds it (serve.json may be stale: a port is reused)
        conn = _proved(port, token, min(a.timeout, 10.0))
    except (OSError, http.client.HTTPException) as e:
        print(f"tapwright call: can't reach tapwright serve ({e}). {NO_DAEMON}", file=sys.stderr)
        return EXIT_UNREACHABLE
    if conn is None:
        print("tapwright call: whatever listens on tapwright serve's port did not prove it holds the token (a stale "
              "address file, or an older tapwright serve), or the proved connection was closed; the token was not "
              "sent. Stop it and start it again: `tapwright serve --stop`, then `tapwright serve`.", file=sys.stderr)
        return EXIT_UNREACHABLE
    conn.timeout = a.timeout  # (the proof used the short one)
    if conn.sock is not None:
        conn.sock.settimeout(a.timeout)
    try:  # one request, never sent twice: an action that was delivered must not be repeated
        conn.request("POST", f"/api/{a.tool}", body=json.dumps(args).encode(), headers=headers)
        reply = conn.getresponse()
        status, body = reply.status, reply.read()
    except (TimeoutError, socket.timeout):
        print(f"tapwright call: no answer from tapwright serve within {a.timeout:g} s; look at the screen before "
              "retrying", file=sys.stderr)
        return EXIT_UNREACHABLE
    except (OSError, http.client.HTTPException) as e:
        print(f"tapwright call: can't reach tapwright serve ({e}). {NO_DAEMON}", file=sys.stderr)
        return EXIT_UNREACHABLE
    finally:
        conn.close()
    try:
        answer = json.loads(body or b"{}")
    except ValueError:
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    if status >= 400:
        err = answer.get("error") if isinstance(answer.get("error"), dict) else {}
        print(f"Error {err.get('code', f'http-{status}')}: {err.get('message', f'HTTP {status}')}", file=sys.stderr)
        return EXIT_REFUSED
    text = answer.get("text") if isinstance(answer.get("text"), str) else ""
    path = _picture(answer)
    if a.json:
        print(json.dumps({"text": text, "image_path": path}, ensure_ascii=False))
    else:
        print(text)
        if path:
            print(f"Picture: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
