"""Where tapwright keeps things (~/.tapwright, or $TAPWRIGHT_HOME), and the two
downloads it needs: WebDriverAgent (Apple's UI automation, prebuilt for real devices by
the Appium project) and rcodesign (Apple code signing that runs on any OS). Both are
pinned and checked by SHA-256."""
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("TAPWRIGHT_HOME") or Path.home() / ".tapwright")
SECRETS = HOME / "signing"  # private key, certificate, profile: never leave this machine
STATE = HOME / "state.json"

WDA_VERSION = "16.15.3"
WDA_URL = f"https://github.com/appium/WebDriverAgent/releases/download/v{WDA_VERSION}/WebDriverAgentRunner-Runner.zip"
WDA_SHA256 = "2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5"

RCODESIGN_VERSION = "0.29.0"
RCODESIGN_BASE = (f"https://github.com/indygreg/apple-platform-rs/releases/download/"
                  f"apple-codesign%2F{RCODESIGN_VERSION}/apple-codesign-{RCODESIGN_VERSION}-")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    return state


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tapwright"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def wda_app() -> Path:
    """The unsigned WebDriverAgent runner app, downloaded once and verified."""
    target = HOME / "wda" / WDA_VERSION / "WebDriverAgentRunner-Runner.app"
    if target.exists():
        return target
    data = _fetch(WDA_URL)
    if hashlib.sha256(data).hexdigest() != WDA_SHA256:
        raise RuntimeError(f"WebDriverAgent download doesn't match its pinned SHA-256 ({WDA_URL})")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(target.parent)
    return target


def _rcodesign_target() -> tuple[str, str]:
    system, machine = platform.system(), platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "Windows":
        return "x86_64-pc-windows-msvc", "zip"
    if system == "Darwin":
        return "macos-universal", "tar.gz"
    if system == "Linux":
        return ("aarch64" if arm else "x86_64") + "-unknown-linux-musl", "tar.gz"
    raise RuntimeError(f"no rcodesign build for {system} {machine}")


def rcodesign() -> Path:
    """The rcodesign executable, downloaded once and verified against the release's checksum."""
    target_name, ext = _rcodesign_target()
    exe = HOME / "tools" / f"rcodesign-{RCODESIGN_VERSION}" / ("rcodesign.exe" if os.name == "nt" else "rcodesign")
    if exe.exists():
        return exe
    url = f"{RCODESIGN_BASE}{target_name}.{ext}"
    data = _fetch(url)
    expected = _fetch(url + ".sha256").decode().split()[0].strip()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"rcodesign download doesn't match its published SHA-256 ({url})")
    exe.parent.mkdir(parents=True, exist_ok=True)
    name = "rcodesign.exe" if os.name == "nt" else "rcodesign"
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.endswith("/" + name) or m == name)
            exe.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/" + name) or m.name == name)
            with t.extractfile(member) as src, open(exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
        exe.chmod(0o755)
    return exe
