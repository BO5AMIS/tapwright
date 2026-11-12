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

# Where the WebDriverAgent runner comes from: (repo, release tag, SHA-256, cache folder).
# Tapwright's own build (Appium's 16.15.3 plus a fast tap and drag, built on GitHub Actions) is the default since 1.1:
# 29% faster taps over USB, and over Wi-Fi a tap within its bar. TAPWRIGHT_WDA=appium selects Appium's prebuilt runner.
# tw-16.15.3.2 adds a fast drag (scroll 1.16 s against 1.44 over USB, 1.62 against 2.00 over Wi-Fi, 2026-11-12): 19%
# at the median, below the 25% bar, so it isn't the row yet. The session uses the drag only when the runner has it.
WDA_SOURCES = {
    "appium": ("appium/WebDriverAgent", "v16.15.3",
               "2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5", "16.15.3"),
    "fork": ("bo5amis/WebDriverAgent", "tw-16.15.3.2", "f39234fcdf4471939bec76c6932d54ef39afc5a5e3004f312545d9a07dd2fea6", "fork-tw-16.15.3.2"),
}
WDA_DEFAULT = "fork"
WDA_VERSION = WDA_SOURCES[WDA_DEFAULT][3]
WDA_SHA256 = WDA_SOURCES[WDA_DEFAULT][2]

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


def wda_source() -> tuple[str, str, str, str]:
    """(repo, tag, SHA-256, cache folder) of the runner in use: $TAPWRIGHT_WDA picks the table row."""
    name = (os.environ.get("TAPWRIGHT_WDA") or WDA_DEFAULT).strip().lower()
    if name not in WDA_SOURCES:
        raise RuntimeError(f"TAPWRIGHT_WDA={name!r} isn't one of {', '.join(WDA_SOURCES)}")
    return WDA_SOURCES[name]


def wda_app() -> Path:
    """The unsigned WebDriverAgent runner app, downloaded once and verified."""
    repo, tag, sha256, folder = wda_source()
    target = HOME / "wda" / folder / "WebDriverAgentRunner-Runner.app"
    done = target.parent / ".verified"  # written last: a folder without it was cut short, and is made again
    if target.exists() and done.exists() and done.read_text(encoding="utf-8").strip() == sha256:
        return target
    data = _release_file(repo, tag, "WebDriverAgentRunner-Runner.zip")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"the WebDriverAgent {tag} download doesn't match its pinned SHA-256")
    if target.exists():
        shutil.rmtree(target)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(target.parent)
    done.write_text(sha256, encoding="utf-8")
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
