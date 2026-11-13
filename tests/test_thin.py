"""`tapwright call <tool> '<json>'` (AGENT-TOOLS §18.10): the thin call. Standard library only, one request to the running
daemon, the answer's text on stdout. Exit 2 for a refusal, 3 when the daemon can't be reached."""
import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tapwright import thin
from tapwright.server import proof_of

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "k" * 43
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class Fake:
    """A stand-in for the daemon's /api route: records what it is sent, answers what the test set."""

    def __init__(self):
        self.seen: list[dict] = []
        self.status = 200
        self.reply: dict = {"text": "Tapped e1.", "image": None, "image_size": None}
        self.proofs = 0
        # (signed with this fake's own port, as a daemon signs the port it listens on; a test may swap in a wrong one)
        self.prove = lambda challenge: proof_of(TOKEN, bytes.fromhex(challenge), self.server.server_address[1])
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/prove":  # the daemon's proof that it holds the token (before the token is sent)
                    outer.proofs += 1
                    data = json.dumps({"proof": outer.prove(json.loads(body)["challenge"])}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": json.loads(body or b"{}")})
                data = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("TAPWRIGHT_APPS", raising=False)
    return tmp_path


@pytest.fixture
def fake(home):
    f = Fake()
    (home / "serve.json").write_text(json.dumps({"pid": 1, "port": f.port, "token": TOKEN}))
    yield f
    f.close()


def run(capsys, *argv):
    code = thin.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---- it loads nothing but the standard library ----

def test_the_thin_call_imports_only_the_standard_library():
    """The build fails if `tapwright.thin` pulls in anything else (pymobiledevice3, starlette, PIL, or the rest of
    tapwright): in a fresh interpreter, every module that importing it adds is the standard library's or its own."""
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "before = set(sys.modules)\n"
        "import tapwright.thin\n"
        "print('\\n'.join(sorted(set(sys.modules) - before)))\n")
    out = subprocess.run([sys.executable, "-c", probe, str(ROOT)], capture_output=True, text=True, check=True)
    added = [m for m in out.stdout.split() if m]
    assert "tapwright.thin" in added
    outside = [m for m in added if m.split(".")[0] not in sys.stdlib_module_names
               and m not in ("tapwright", "tapwright.thin")]
    assert outside == [], f"tapwright.thin imports beyond the standard library: {outside}"


@pytest.mark.skipif(bool(os.environ.get("CI")), reason="a wall-clock bound is flaky on shared CI machines")
def test_the_thin_call_starts_in_under_half_a_second():
    """`python -m tapwright call --help` doesn't load the heavy CLI: it dispatches before it. Best of three, since
    the first run may pay for cold disk caches."""
    best = 9.0
    for _ in range(3):
        began = time.perf_counter()
        out = subprocess.run([sys.executable, "-m", "tapwright", "call", "--help"], cwd=ROOT, capture_output=True,
                             text=True)
        best = min(best, time.perf_counter() - began)
        assert out.returncode == 0 and "--apps" in out.stdout
    assert best < 0.5, f"call --help took {best:.2f} s"


def test_the_main_entry_dispatches_call_before_the_heavy_cli_loads():
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['tapwright', 'call', '--help']\n"
             "from tapwright import entry as m\n"  # (the console script's entry, which `python -m tapwright` runs too)
             "try:\n    m.main()\nexcept SystemExit:\n    pass\n"
             "print('HEAVY' if any(x in sys.modules for x in ('tapwright.cli', 'pymobiledevice3', 'starlette')) else 'LIGHT')\n")
    out = subprocess.run([sys.executable, "-c", probe, str(ROOT)], capture_output=True, text=True)
    assert out.stdout.strip().endswith("LIGHT"), out.stdout + out.stderr


# ---- the call ----

def test_the_answers_text_goes_to_stdout_and_the_arguments_to_the_tool(fake, capsys):
    code, out, err = run(capsys, "tap", '{"target": {"id": "save"}}', "--apps", "com.example.app", "--client", "me")
    assert (code, out, err) == (0, "Tapped e1.\n", "")
    (seen,) = fake.seen
    assert seen["path"] == "/api/tap" and seen["body"] == {"target": {"id": "save"}}
    h = seen["headers"]
    assert h["authorization"] == f"Bearer {TOKEN}"
    assert h["x-agent-client"] == "me" and h["x-tapwright-client"] == "me"  # the new name, and the old one kept
    assert h["x-tapwright-apps"] == "com.example.app" and "x-tapwright-mode" not in h
    assert h["x-tapwright-via"] == "call"  # the daemon logs it: session_report names calls each from a new process


def test_no_arguments_send_an_empty_object(fake, capsys):
    assert run(capsys, "look", "--apps", "a.b")[0] == 0
    assert fake.seen[0]["body"] == {}


def test_arguments_can_come_from_standard_input(fake, capsys, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"text": "Saved"}'))
    assert run(capsys, "wait_for", "-", "--apps", "a.b")[0] == 0
    assert fake.seen[0]["body"] == {"text": "Saved"}


def test_the_client_defaults_to_the_parent_process(fake, capsys):
    run(capsys, "look", "--apps", "a.b")
    assert fake.seen[0]["headers"]["x-agent-client"] == str(os.getppid())


def test_apps_come_from_the_environment_when_not_given(fake, capsys, monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_APPS", "a.b,c.d")
    run(capsys, "look")
    assert fake.seen[0]["headers"]["x-tapwright-apps"] == "a.b,c.d"
    run(capsys, "look", "--apps", "e.f")  # the flag wins
    assert fake.seen[1]["headers"]["x-tapwright-apps"] == "e.f"


def test_dedicated_sends_the_mode_and_no_apps(fake, capsys):
    run(capsys, "look", "--dedicated")
    h = fake.seen[0]["headers"]
    assert h["x-tapwright-mode"] == "dedicated" and "x-tapwright-apps" not in h


def test_apps_and_dedicated_together_are_a_usage_error(fake, capsys):
    with pytest.raises(SystemExit) as e:
        thin.main(["look", "--apps", "a.b", "--dedicated"])
    assert e.value.code == 2 and fake.seen == []


def test_a_policy_and_ux_rules_file_are_sent_as_one_line_of_json(fake, capsys, tmp_path):
    pol = tmp_path / "policy.json"
    pol.write_text('{\n  "confirm": ["Buy", "Pay"],\n  "block": ["Wipe"]\n}', encoding="utf-8")
    ux = tmp_path / "ux.json"
    ux.write_text('{"rules": [{"max_taps_to_goal": 3}]}', encoding="utf-8")
    code, _, _ = run(capsys, "look", "--apps", "a.b", "--policy", str(pol), "--ux-rules", str(ux))
    assert code == 0
    h = fake.seen[0]["headers"]
    assert json.loads(h["x-tapwright-policy"]) == {"confirm": ["Buy", "Pay"], "block": ["Wipe"]}
    assert "\n" not in h["x-tapwright-policy"]
    assert json.loads(h["x-tapwright-ux-rules"]) == {"rules": [{"max_taps_to_goal": 3}]}


def test_a_policy_file_that_cannot_be_read_is_a_usage_error_before_any_request(fake, capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out, err = run(capsys, "look", "--apps", "a.b", "--policy", str(bad))
    assert code == 2 and out == "" and "policy" in err and fake.seen == []
    code, _, err = run(capsys, "look", "--apps", "a.b", "--policy", str(tmp_path / "missing.json"))
    assert code == 2 and "policy" in err and fake.seen == []


def test_a_picture_is_saved_and_its_path_printed(fake, capsys):
    fake.reply = {"text": "Screen 390x844.", "image": base64.b64encode(JPEG).decode(), "image_size": [390, 844]}
    code, out, _ = run(capsys, "look", '{"screenshot": true}', "--apps", "a.b")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "Screen 390x844."
    path = Path(lines[-1].removeprefix("Picture: "))
    try:
        assert lines[-1].startswith("Picture: ") and path.suffix == ".jpg" and path.read_bytes() == JPEG
    finally:
        path.unlink(missing_ok=True)


def test_json_prints_the_text_and_the_image_path(fake, capsys):
    fake.reply = {"text": "Screen.", "image": base64.b64encode(JPEG).decode(), "image_size": [1, 1]}
    code, out, _ = run(capsys, "look", "--apps", "a.b", "--json")
    answer = json.loads(out)
    try:
        assert code == 0 and set(answer) == {"text", "image_path"} and answer["text"] == "Screen."
        assert Path(answer["image_path"]).read_bytes() == JPEG
    finally:
        Path(answer["image_path"]).unlink(missing_ok=True)
    fake.reply = {"text": "No picture.", "image": None, "image_size": None}
    _, out, _ = run(capsys, "look", "--apps", "a.b", "--json")
    assert json.loads(out) == {"text": "No picture.", "image_path": None}


def test_non_ascii_text_is_written_as_utf8(fake, capsys):
    fake.reply = {"text": "زر «حفظ»", "image": None, "image_size": None}
    code, out, _ = run(capsys, "look", "--apps", "a.b")
    assert code == 0 and out == "زر «حفظ»\n"


def test_a_refusal_prints_its_error_line_on_stderr_and_exits_2(fake, capsys):
    fake.status = 409
    fake.reply = {"error": {"code": "not_found", "message": "no element matches the text 'Save'"}}
    code, out, err = run(capsys, "tap", '{"target": {"text": "Save"}}', "--apps", "a.b")
    assert (code, out) == (2, "")
    assert err == "Error not_found: no element matches the text 'Save'\n"


def test_a_refusal_with_no_body_still_says_the_status(fake, capsys):
    fake.status = 502
    fake.reply = {}
    code, _, err = run(capsys, "look", "--apps", "a.b")
    assert code == 2 and err.startswith("Error http-502")


def test_no_daemon_exits_3_and_says_to_start_one(home, capsys):
    code, out, err = run(capsys, "look", "--apps", "a.b")
    assert code == 3 and out == "" and "tapwright serve" in err
    assert not (home / "serve.json").exists()  # the thin call never starts a daemon, nor writes anything


def test_a_daemon_that_is_gone_exits_3(home, capsys):
    f = Fake()
    port = f.port
    f.close()  # the port is free again: connection refused
    (home / "serve.json").write_text(json.dumps({"port": port, "token": TOKEN}))
    code, _, err = run(capsys, "look", "--apps", "a.b")
    assert code == 3 and "tapwright serve" in err


def test_a_silent_daemon_times_out_with_exit_3_and_is_not_asked_again(fake, capsys):
    release = threading.Event()
    original = fake.server.RequestHandlerClass.do_POST

    def slow(self):
        if self.path != "/api/prove":  # (the proof answers at once; it is the action that is slow)
            release.wait(5)
        original(self)

    fake.server.RequestHandlerClass.do_POST = slow
    try:
        code, _, err = run(capsys, "tap", "--apps", "a.b", "--timeout", "0.3")
    finally:
        release.set()
    assert code == 3 and "look at the screen before retrying" in err
    time.sleep(0.2)
    assert len(fake.seen) <= 1  # one request, never two: an action isn't sent twice


def test_a_bad_json_argument_is_a_usage_error(fake, capsys):
    code, _, err = run(capsys, "tap", "{nope", "--apps", "a.b")
    assert code == 2 and "JSON" in err and fake.seen == []
    code, _, err = run(capsys, "tap", "[1]", "--apps", "a.b")
    assert code == 2 and "object" in err and fake.seen == []


def test_a_serve_file_without_a_token_exits_3(home, capsys):
    (home / "serve.json").write_text(json.dumps({"port": 9}))
    assert run(capsys, "look", "--apps", "a.b")[0] == 3


# ---- the real route ----

def test_a_real_daemon_route_answers_the_thin_call(home, capsys):
    """The thin call against the daemon's own app (a fake phone behind it), over a real socket."""
    import socket

    import uvicorn
    from fakes import Clock, FakeSession

    from tapwright.control import Controller
    from tapwright.daemon import Daemon
    from tapwright.server import create_app

    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    d.session.foreground = "com.example.app"
    app = create_app(d, TOKEN)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started
        (home / "serve.json").write_text(json.dumps({"port": sock.getsockname()[1], "token": TOKEN}))
        code, out, err = run(capsys, "look", '{"screenshot": false}', "--apps", "com.example.app", "--client", "t1")
        assert code == 0, err
        assert out.strip()
        code, _, err = run(capsys, "look", '{"screenshot": false}', "--apps", "com.example.other", "--client", "t1")
        assert code == 2 and err.startswith("Error not_allowed: outside_apps")
        code, _, err = run(capsys, "look", "--client", "t1")  # no apps at all: refused, as today
        assert code == 2 and "no_apps" in err
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


# ---- the token goes only to a process that proved it holds it (1.6 security, Tapline's finding) ----

def test_the_token_is_not_sent_to_a_process_that_cannot_prove_it_holds_it(fake, capsys):
    """serve.json outlives its daemon: another local process may now listen on that port. It must never get the token."""
    fake.prove = lambda challenge: "0" * 64
    code, out, err = run(capsys, "look", "--apps", "com.example.app")
    assert code == thin.EXIT_UNREACHABLE and out == "" and "did not prove" in err
    assert fake.seen == [] and fake.proofs == 1  # no request carried the token


def test_a_process_that_relays_the_challenge_to_a_live_daemon_gets_no_token(fake, capsys):
    """Tapline's Codex finding: a listener on serve.json's (stale) port forwards the challenge to a real daemon on
    another port and returns that daemon's proof. The proof names the port it was made on, so it never matches the port
    this client dialled, and the token is not sent."""
    real = Fake()  # a live daemon elsewhere, holding the same token
    try:
        fake.prove = real.prove  # the listener relays: the answer is the real daemon's, made for the real daemon's port
        code, out, err = run(capsys, "look", "--apps", "com.example.app")
        assert code == thin.EXIT_UNREACHABLE and out == "" and "did not prove" in err
        assert fake.seen == [] and real.seen == []  # no request carried the token, to either
    finally:
        real.close()


def test_a_process_with_no_proof_route_gets_no_token(fake, capsys):
    fake.prove = lambda challenge: None  # answers {"proof": null}, as a server that doesn't know the route might
    code, _, err = run(capsys, "look", "--apps", "com.example.app")
    assert code == thin.EXIT_UNREACHABLE and fake.seen == []


def test_the_proof_is_asked_before_the_token_goes_out_with_a_fresh_challenge(fake, capsys):
    asked = []
    real = fake.prove
    fake.prove = lambda c: (asked.append(c), real(c))[1]
    for _ in range(2):
        assert run(capsys, "look", "--apps", "com.example.app")[0] == 0
    assert len(asked) == 2 and asked[0] != asked[1] and all(len(c) == 64 for c in asked)
    assert len(fake.seen) == 2 and all(s["headers"]["authorization"] == f"Bearer {TOKEN}" for s in fake.seen)


def test_a_proof_made_for_another_address_on_the_same_port_gets_no_token(fake, capsys):
    """Tapline's Codex finding: a relay on another address with the same port (::1:P relaying to 127.0.0.1:P, or the
    reverse) returns a proof signed for that other address. The client dials the literal 127.0.0.1 and checks the proof
    against it, so the token is not sent."""
    fake.prove = lambda challenge: proof_of(TOKEN, bytes.fromhex(challenge), fake.port, "::1")
    code, out, err = run(capsys, "look", "--apps", "com.example.app")
    assert code == thin.EXIT_UNREACHABLE and out == "" and "did not prove" in err
    assert fake.seen == []
