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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("TAPWRIGHT_HOME") or Path.home() / ".tapwright")
SECRETS = HOME / "signing"  # private key, certificate, profile: never leave this machine
STATE = HOME / "state.json"
SERVE_FILE = HOME / "serve.json"  # the running `tapwright serve`: port, pid, token (kept after it stops)

WDA_VERSION = "16.15.3"
WDA_SHA256 = "2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5"

RCODESIGN_VERSION = "0.29.0"


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


def read_serve_info() -> dict:
    try:
        return json.loads(SERVE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_serve_info(info: dict) -> None:
    SERVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SERVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2))
    if os.name != "nt":
        tmp.chmod(0o600)  # it holds the live view's token
    os.replace(tmp, SERVE_FILE)


def _fetch(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tapwright", "Accept": accept})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _release_file(repo: str, tag: str, name: str) -> bytes:
    """A file from a GitHub release. The download link sometimes fails with a 5xx for a
    while; then the same file comes through GitHub's API. Callers check the SHA-256."""
    tag = urllib.parse.quote(tag, safe="")
    link = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    try:
        return _fetch(link)
    except (urllib.error.URLError, TimeoutError) as e:
        try:
            release = json.loads(_fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}"))
            asset = next(a for a in release["assets"] if a["name"] == name)
            return _fetch(asset["url"], accept="application/octet-stream")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, StopIteration):
            raise RuntimeError(f"couldn't download {link}: {e}") from None


def wda_app() -> Path:
    """The unsigned WebDriverAgent runner app, downloaded once and verified."""
    target = HOME / "wda" / WDA_VERSION / "WebDriverAgentRunner-Runner.app"
    if target.exists():
        return target
    data = _release_file("appium/WebDriverAgent", f"v{WDA_VERSION}", "WebDriverAgentRunner-Runner.zip")
    if hashlib.sha256(data).hexdigest() != WDA_SHA256:
        raise RuntimeError(f"the WebDriverAgent {WDA_VERSION} download doesn't match its pinned SHA-256")
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
    tag, asset = f"apple-codesign/{RCODESIGN_VERSION}", f"apple-codesign-{RCODESIGN_VERSION}-{target_name}.{ext}"
    data = _release_file("indygreg/apple-platform-rs", tag, asset)
    expected = _release_file("indygreg/apple-platform-rs", tag, asset + ".sha256").decode().split()[0].strip()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"the rcodesign download doesn't match its published SHA-256 ({asset})")
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
