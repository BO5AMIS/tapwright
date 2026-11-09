"""Signing WebDriverAgent for your iPhone.

Apple runs UI automation only from a development-signed test runner whose profile
includes your phone. Two ways to get the certificate and profile:

* App Store Connect API key (paid developer account): `tapwright setup --asc-key ...`
  registers the phone, creates an Apple Development certificate (the private key is
  made here and never leaves this machine), a wildcard App ID and a development profile.
* Your own files: an Apple Development certificate (.p12) and a development profile
  (.mobileprovision) that covers the phone and a wildcard App ID.

Then rcodesign re-signs the prebuilt WebDriverAgent with that identity, under bundle IDs
inside your App ID.
"""
import base64
import json
import plistlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import store

API = "https://api.appstoreconnect.apple.com/v1"
KEY_FILE = store.SECRETS / "development.key.pem"
CERT_FILE = store.SECRETS / "development.cer"
PROFILE_FILE = store.SECRETS / "development.mobileprovision"


# ---------- App Store Connect API ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class AppStoreConnect:
    """The few App Store Connect API calls setup needs, authenticated with an API key
    (App Store Connect > Users and Access > Integrations > Keys)."""

    def __init__(self, key_id: str, issuer_id: str, key_file: str):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        self.key_id, self.issuer_id = key_id, issuer_id
        self.key = load_pem_private_key(Path(key_file).read_bytes(), password=None)

    def _token(self) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        now = int(time.time())
        head = _b64url(json.dumps({"alg": "ES256", "kid": self.key_id, "typ": "JWT"}).encode())
        body = _b64url(json.dumps({"iss": self.issuer_id, "iat": now, "exp": now + 900,
                                   "aud": "appstoreconnect-v1"}).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{head}.{body}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {self._token()}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"App Store Connect {method} {path}: HTTP {e.code} {detail[:400]}") from None

    def device(self, udid: str, name: str) -> str:
        found = self.call("GET", f"/devices?filter[udid]={udid}&filter[platform]=IOS")["data"]
        if found:
            return found[0]["id"]
        return self.call("POST", "/devices", {"data": {"type": "devices", "attributes": {
            "name": name, "udid": udid, "platform": "IOS"}}})["data"]["id"]

    def development_certificate(self, csr_der: bytes) -> tuple[str, bytes]:
        csr = base64.b64encode(csr_der).decode()
        error = None
        for kind in ("DEVELOPMENT", "IOS_DEVELOPMENT"):  # Apple Development, then the older iOS-only type
            try:
                data = self.call("POST", "/certificates", {"data": {"type": "certificates", "attributes": {
                    "certificateType": kind, "csrContent": csr}}})["data"]
                return data["id"], base64.b64decode(data["attributes"]["certificateContent"])
            except RuntimeError as e:
                error = e
        raise RuntimeError(f"couldn't create a development certificate: {error}")

    def bundle_id(self, identifier: str, name: str) -> str:
        found = self.call("GET", f"/bundleIds?filter[identifier]={identifier}")["data"]
        exact = [b for b in found if b["attributes"]["identifier"] == identifier]
        if exact:
            return exact[0]["id"]
        return self.call("POST", "/bundleIds", {"data": {"type": "bundleIds", "attributes": {
            "identifier": identifier, "name": name, "platform": "IOS"}}})["data"]["id"]

    def development_profile(self, name: str, bundle_id: str, cert_id: str, device_id: str) -> bytes:
        rel = {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
               "certificates": {"data": [{"type": "certificates", "id": cert_id}]},
               "devices": {"data": [{"type": "devices", "id": device_id}]}}
        data = self.call("POST", "/profiles", {"data": {"type": "profiles", "attributes": {
            "name": name, "profileType": "IOS_APP_DEVELOPMENT"}, "relationships": rel}})["data"]
        return base64.b64decode(data["attributes"]["profileContent"])


def create_identity(asc: AppStoreConnect, udid: str, bundle_prefix: str) -> None:
    """Phone, certificate, wildcard App ID and profile, through the API. Reuses what exists."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    store.SECRETS.mkdir(parents=True, exist_ok=True)
    device_id = asc.device(udid, "tapwright test iPhone")
    state = store.load_state()
    if not (KEY_FILE.exists() and CERT_FILE.exists() and state.get("certificate_id")):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        csr = x509.CertificateSigningRequestBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tapwright")])).sign(key, hashes.SHA256())
        cert_id, cert = asc.development_certificate(csr.public_bytes(serialization.Encoding.DER))
        CERT_FILE.write_bytes(cert)
        state = store.save_state(certificate_id=cert_id)
    app_id = asc.bundle_id(f"{bundle_prefix}.*", "tapwright WebDriverAgent")
    PROFILE_FILE.write_bytes(asc.development_profile(
        f"tapwright {time.strftime('%Y%m%d-%H%M%S')}", app_id, state["certificate_id"], device_id))


def import_identity(p12: str, password: str, profile: str) -> None:
    """Use an existing Apple Development certificate (.p12) and development profile."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    key, cert, _ = pkcs12.load_key_and_certificates(Path(p12).read_bytes(), password.encode() if password else None)
    store.SECRETS.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    shutil.copy(profile, PROFILE_FILE)


# ---------- the profile, and signing WebDriverAgent ----------

def read_profile(path: Path | None = None) -> dict:
    """A .mobileprovision is a signed plist; the plist itself sits in it as plain XML."""
    path = path or PROFILE_FILE
    raw = path.read_bytes()
    start, end = raw.find(b"<?xml"), raw.find(b"</plist>")
    if start < 0 or end < 0:
        raise RuntimeError(f"{path} is not a provisioning profile")
    return plistlib.loads(raw[start:end + len(b"</plist>")])


def runner_ids(profile: dict) -> tuple[str, str, str]:
    """(team id, runner bundle id, test bundle id) inside the profile's wildcard App ID."""
    team = profile["TeamIdentifier"][0]
    app_id = profile["Entitlements"]["application-identifier"]  # "TEAM.com.example.*"
    prefix = app_id.split(".", 1)[1]
    if not prefix.endswith("*"):
        raise RuntimeError(f"the profile's App ID {app_id} isn't a wildcard (like TEAM.com.example.*); "
                           "WebDriverAgent needs two bundle IDs under one App ID")
    prefix = prefix.rstrip("*").rstrip(".")
    return team, f"{prefix}.WebDriverAgentRunner.xctrunner", f"{prefix}.WebDriverAgentRunner"


def sign_wda(work: Path) -> tuple[Path, str]:
    """Re-sign WebDriverAgent with the stored identity. Returns (ipa, runner bundle id)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    profile = read_profile()
    team, runner_id, test_id = runner_ids(profile)
    if work.exists():
        shutil.rmtree(work)
    app = work / "Payload" / "WebDriverAgentRunner-Runner.app"
    shutil.copytree(store.wda_app(), app)
    shutil.rmtree(app / "PlugIns" / "WebDriverAgentRunner.xctest.dSYM", ignore_errors=True)
    for rel, bundle_id in (("Info.plist", runner_id), ("PlugIns/WebDriverAgentRunner.xctest/Info.plist", test_id)):
        info = plistlib.loads((app / rel).read_bytes())
        info["CFBundleIdentifier"] = bundle_id
        (app / rel).write_bytes(plistlib.dumps(info))
    shutil.copy(PROFILE_FILE, app / "embedded.mobileprovision")
    entitlements = {"application-identifier": f"{team}.{runner_id}", "com.apple.developer.team-identifier": team,
                    "get-task-allow": True, "keychain-access-groups": [f"{team}.*"]}
    (work / "entitlements.plist").write_bytes(plistlib.dumps(entitlements))
    cert = x509.load_der_x509_certificate(CERT_FILE.read_bytes())
    (work / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    shutil.copy(KEY_FILE, work / "key.pem")
    # Relative paths: rcodesign reads "scope:value", and Windows paths contain a colon.
    # Unscoped entitlements go on the runner's executable, where XCTest needs get-task-allow.
    r = subprocess.run([str(store.rcodesign()), "sign", "--pem-file", "cert.pem", "--pem-file", "key.pem",
                        "--team-name", team, "--entitlements-xml-file", "entitlements.plist",
                        str(app.relative_to(work))], cwd=work, capture_output=True, text=True)
    (work / "key.pem").unlink()
    if r.returncode != 0:
        raise RuntimeError("signing WebDriverAgent failed: " + (r.stderr or r.stdout)[-1500:])
    ipa = work / "WebDriverAgent.ipa"
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted((work / "Payload").rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(work))
    return ipa, runner_id
