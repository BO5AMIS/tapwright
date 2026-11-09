"""Tests that need no phone. Run: pytest  (TAPWRIGHT_SIGN_TEST=1 also signs WebDriverAgent for real,
downloading it and rcodesign.)"""
import asyncio
import base64
import json
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from tapwright import ui

# A trimmed WebDriverAgent tree, shaped like /source?format=json on iOS 26.
SOURCE = {"type": "XCUIElementTypeApplication", "label": "Example", "rect": {"x": 0, "y": 0, "width": 402, "height": 874},
          "isVisible": "1", "children": [
              {"type": "XCUIElementTypeWindow", "rect": {"x": 0, "y": 0, "width": 402, "height": 874}, "children": [
                  {"type": "XCUIElementTypeOther", "label": "Welcome Log in", "rect": {"x": 0, "y": 0, "width": 402, "height": 874},
                   "children": [
                       {"type": "XCUIElementTypeStaticText", "label": "Welcome", "name": "Welcome", "isVisible": "1",
                        "rect": {"x": 24, "y": 96, "width": 200, "height": 30}},
                       {"type": "XCUIElementTypeTextField", "label": "Email", "name": "email-input", "value": "",
                        "rect": {"x": 24, "y": 500, "width": 354, "height": 44}},
                       {"type": "XCUIElementTypeButton", "label": "Log in", "name": "login-button", "isEnabled": "1",
                        "rect": {"x": 24, "y": 610, "width": 354, "height": 48}},
                       {"type": "XCUIElementTypeButton", "label": "Log in with Apple", "name": "apple-login", "isEnabled": "0",
                        "rect": {"x": 24, "y": 670, "width": 354, "height": 48}},
                       {"type": "XCUIElementTypeButton", "label": "Hidden", "isVisible": "0",
                        "rect": {"x": 24, "y": 730, "width": 354, "height": 48}},
                       {"type": "XCUIElementTypeButton", "label": "Below", "rect": {"x": 24, "y": 1200, "width": 354, "height": 48}},
                   ]},
                  {"type": "XCUIElementTypeKeyboard", "rect": {"x": 0, "y": 600, "width": 402, "height": 274}, "children": [
                      {"type": "XCUIElementTypeKey", "label": "q", "rect": {"x": 4, "y": 610, "width": 36, "height": 42}}]},
              ]}]}
SCREEN = (402.0, 874.0)


def test_elements_are_flattened_in_reading_order():
    items = ui.elements(SOURCE, SCREEN)
    assert [e.label for e in items] == ["Welcome", "Email", "Log in", "Log in with Apple"]
    assert [e.ref for e in items] == ["e1", "e2", "e3", "e4"]
    assert items[1].name == "email-input" and items[1].type == "TextField"
    assert items[0].name == ""  # a name that only mirrors the label isn't shown
    assert not items[3].enabled


def test_keyboard_keys_only_on_request():
    assert "q" not in [e.label for e in ui.elements(SOURCE, SCREEN)]
    assert "q" in [e.label for e in ui.elements(SOURCE, SCREEN, keyboard=True)]


def test_direction_marks_are_dropped():
    # iOS Calculator, measured: the result is a StaticText labelled "\u200e42".
    node = {"type": "XCUIElementTypeStaticText", "label": "\u200e42", "name": "\u200e42", "value": "\u200e42",
            "rect": {"x": 326, "y": 294, "width": 88, "height": 87}}
    items = ui.elements(node, SCREEN)
    assert items[0].line() == 'e1 StaticText "42" @326,294 88x87'
    assert ui.find(items, label="42") is items[0]


def test_describe_is_compact():
    text = ui.describe(ui.elements(SOURCE, SCREEN), SCREEN)
    assert text.splitlines()[0] == "screen 402x874 pt, 4 elements"
    assert 'e3 Button "Log in" id=login-button @24,610 354x48' in text
    assert "disabled" in text.splitlines()[4]


def test_find_by_id_label_ref_and_index():
    items = ui.elements(SOURCE, SCREEN)
    assert ui.find(items, id="login-button").label == "Log in"
    assert ui.find(items, label="log in").name == "login-button"  # exact before substring
    assert ui.find(items, label="apple").name == "apple-login"  # substring
    assert ui.find(items, ref="e2").name == "email-input"
    with pytest.raises(ui.NotFound):
        ui.find(items, label="nothing like this")
    with pytest.raises(ui.NotFound):
        ui.find(items, ref="e99")


def test_gestures():
    tap = ui.tap(201, 634)["actions"][0]["actions"]
    assert tap[0] == {"type": "pointerMove", "duration": 0, "x": 201, "y": 634}
    assert [s["type"] for s in tap] == ["pointerMove", "pointerDown", "pause", "pointerUp"]
    x0, y0, x1, y1 = ui.scroll_points("down", SCREEN, 0.5)
    assert y0 > y1 and x0 == x1  # content moves up: the finger goes up
    with pytest.raises(ValueError):
        ui.scroll_points("sideways", SCREEN)


def test_path_and_wheel_scroll():
    steps = ui.path([(0, 1.4, 2.6), (5, 3.0, 4.0), (40, 30.0, 40.0)])["actions"][0]["actions"]
    assert steps[0] == {"type": "pointerMove", "duration": 0, "x": 1, "y": 3}
    assert [s.get("duration") for s in steps[2:4]] == [10, 35]  # at least 10 ms per move
    x0, y0, x1, y1 = ui.scroll_swipe(0.5, 0.5, 0.0, 0.25, SCREEN)
    assert (x0, y0) == (201.0, 437.0) and x1 == x0 and y1 == y0 - 0.25 * 874  # dy > 0: the finger moves up
    assert ui.scroll_swipe(0.5, 0.99, 0.0, -0.8, SCREEN)[3] == 873.0  # kept inside the screen
    _, y0, _, y1 = ui.scroll_swipe(0.5, 0.99, 0.0, -0.8, SCREEN)
    assert y1 - y0 == pytest.approx(0.8 * 874)  # the start moved inward: full distance kept
    sx0, sy0, sx1, sy1 = ui.scroll_swipe(0.5, 0.5, 0.0, 0.001, SCREEN)
    assert sx0 == sx1 and abs(sy1 - sy0) == pytest.approx(20.0)  # never short enough to be a tap
    with pytest.raises(ValueError):
        ui.scroll_swipe(0.5, 0.5, 0, 0, SCREEN)


def _profile(app_id="TEAM123456.com.example.tapwright.*", devices=("UDID1",)):
    plist = plistlib.dumps({"Name": "test", "TeamIdentifier": ["TEAM123456"], "ProvisionedDevices": list(devices),
                            "ExpirationDate": datetime.now() + timedelta(days=300),
                            "Entitlements": {"application-identifier": app_id, "get-task-allow": True}})
    return b"0\x82CMS-signature-bytes" + plist + b"\x00more-signature"


def test_profile_and_runner_ids(tmp_path):
    from tapwright import signing
    path = tmp_path / "p.mobileprovision"
    path.write_bytes(_profile())
    profile = signing.read_profile(path)
    assert signing.runner_ids(profile) == ("TEAM123456", "com.example.tapwright.WebDriverAgentRunner.xctrunner",
                                           "com.example.tapwright.WebDriverAgentRunner")
    path.write_bytes(_profile(app_id="TEAM123456.com.example.app"))
    with pytest.raises(RuntimeError, match="wildcard"):
        signing.runner_ids(signing.read_profile(path))


def test_app_store_connect_token_verifies(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from tapwright.signing import AppStoreConnect
    key = ec.generate_private_key(ec.SECP256R1())
    key_file = tmp_path / "AuthKey.p8"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    token = AppStoreConnect("KEYID", "issuer-uuid", str(key_file))._token()
    head, body, sig = token.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "KEYID", "typ": "JWT"}
    claims = json.loads(pad(body))
    assert claims["iss"] == "issuer-uuid" and claims["aud"] == "appstoreconnect-v1"
    raw = pad(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


class _FakeConn:
    def __init__(self, response: bytes):
        self.response, self.sent = response, b""

    async def sendall(self, data):
        self.sent += data

    async def recv_any(self, n=65536):
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def recvall(self, n):
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def close(self):
        pass


class _FakeRsd:
    def __init__(self, response: bytes):
        self.conn = _FakeConn(response)

    async def create_service_connection(self, port):
        assert port == 8100
        return self.conn


def test_wda_http_over_the_tunnel():
    from tapwright.device import Device, DeviceError
    body = json.dumps({"value": {"ready": True}, "sessionId": "S1"}).encode()
    d = Device(log=lambda *a: None)
    d.rsd = _FakeRsd(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    data = asyncio.run(d.http("POST", "/session", {"capabilities": {}}))
    assert data["sessionId"] == "S1"
    assert d.rsd.conn.sent.startswith(b"POST /session HTTP/1.1\r\n") and d.rsd.conn.sent.endswith(b'{"capabilities": {}}')
    err = json.dumps({"value": {"error": "no such element", "message": "gone"}}).encode()
    d.rsd = _FakeRsd(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n\r\n" % len(err) + err)
    with pytest.raises(DeviceError, match="no such element"):
        asyncio.run(d.http("GET", "/x"))


def test_mcp_server_lists_its_tools():
    proc = subprocess.Popen([sys.executable, "-m", "tapwright", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        def send(msg):
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(proc.stdout.readline())["result"]["serverInfo"]["name"] == "tapwright"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"] for t in json.loads(proc.stdout.readline())["result"]["tools"]}
        assert {"look", "list_apps", "launch_app", "tap", "type_text", "scroll", "swipe", "alert"} <= tools
    finally:
        proc.kill()


@pytest.mark.skipif(not os.environ.get("TAPWRIGHT_SIGN_TEST"), reason="downloads WebDriverAgent and rcodesign")
def test_signing_pipeline_with_a_throwaway_identity(tmp_path, monkeypatch):
    """The real pipeline (pinned downloads, rcodesign, entitlements) with a self-signed certificate."""
    from tapwright import signing, store
    monkeypatch.setattr(signing, "PROFILE_FILE", tmp_path / "p.mobileprovision")
    monkeypatch.setattr(signing, "KEY_FILE", tmp_path / "key.pem")
    monkeypatch.setattr(signing, "CERT_FILE", tmp_path / "cert.cer")
    signing.PROFILE_FILE.write_bytes(_profile())
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "throwaway")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(datetime.now()).not_valid_after(datetime.now() + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    signing.KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
    signing.CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    ipa, runner = signing.sign_wda(tmp_path / "build")
    assert runner == "com.example.tapwright.WebDriverAgentRunner.xctrunner" and ipa.stat().st_size > 100_000
    exe = tmp_path / "build" / "Payload" / "WebDriverAgentRunner-Runner.app" / "WebDriverAgentRunner-Runner"
    info = subprocess.run([str(store.rcodesign()), "print-signature-info", str(exe)], capture_output=True, text=True).stdout
    assert "Entitlements (5)" in info and "get-task-allow" in info and runner in info
