"""The AirPlay receiver for the H.264 live view: UxPlay, an open-source program run as its own process. It takes
the phone's Screen Mirroring and passes the phone's own H.264 on, undecoded, as RTP to video.VideoHub on
localhost. Tapwright doesn't bundle it: `find()` looks for an installed one.

How it is run (the size, 60 frames a second, a fixed device id) follows phoneuse (MIT), where it was worked
out on Windows; the numbers were measured again here on an iPhone 14 Pro Max, iOS 26.5 (2026-11-10)."""
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import store

NAME = "Tapwright"  # what the phone lists under Screen Mirroring
WINDOWS_BUILD = Path(r"C:\msys64\ucrt64\bin\uxplay.exe")  # where an MSYS2 build of UxPlay lands
STARTED = ("begin streaming", "starting mirroring")  # UxPlay's log lines when the phone starts mirroring...
ENDED = ("no longer true", "connection closed")  # ...and when it stops


def find() -> str | None:
    """The UxPlay to run: $TAPWRIGHT_UXPLAY, else `uxplay` on the PATH, else the MSYS2 build on Windows."""
    named = os.environ.get("TAPWRIGHT_UXPLAY")
    if named:
        return named if Path(named).is_file() else None
    found = shutil.which("uxplay")
    if found:
        return found
    return str(WINDOWS_BUILD) if os.name == "nt" and WINDOWS_BUILD.is_file() else None


def device_id() -> str:
    """A fixed AirPlay device id for this computer (a random, locally administered MAC address, made once), so
    the phone sees one receiver across runs. UxPlay would otherwise take an adapter's address, which can change."""
    path = store.HOME / "airplay-id"
    try:
        mac = path.read_text().strip()
        if len(mac) == 17:
            return mac
    except OSError:
        pass
    raw = bytearray(os.urandom(6))
    raw[0] = (raw[0] & 0xFC) | 0x02  # unicast, locally administered
    mac = ":".join(f"{b:02x}" for b in raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mac)
    return mac


def command(uxplay: str, rtp_port: int, pixels: tuple[int, int], name: str = NAME) -> list[str]:
    """UxPlay's command line: fixed ports, our name, the phone's H.264 as RTP to `rtp_port`, no audio."""
    return [uxplay, "-p", "-n", name, "-nh",
            "-vrtp", f"config-interval=1 pt=96 mtu=60000 ! udpsink host=127.0.0.1 port={rtp_port} sync=false",
            "-vsync", "no", "-s", f"{pixels[0]}x{pixels[1]}@60", "-fps", "60", "-m", device_id(), "-a"]


class Receiver:
    """UxPlay as a child process. `mirroring` follows what its log says about the phone's connection."""

    def __init__(self, uxplay: str, rtp_port: int, pixels: tuple[int, int],
                 log: Callable[[str], None] | None = None, name: str = NAME) -> None:
        self.args = command(uxplay, rtp_port, pixels, name)
        self.name = name
        self.mirroring = False
        self._log = log or (lambda message: None)
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(self.args[0]) + os.pathsep + env.get("PATH", "")  # its DLLs sit beside it
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(self.args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, creationflags=flags)
        threading.Thread(target=self._follow, args=(self._process,), name="uxplay-log", daemon=True).start()

    def _follow(self, process: subprocess.Popen) -> None:
        for raw in process.stdout:
            line = raw.decode(errors="replace").strip().lower()
            if any(mark in line for mark in STARTED):
                if not self.mirroring:
                    self._log("video: the phone is mirroring")
                self.mirroring = True
            elif any(mark in line for mark in ENDED):
                if self.mirroring:
                    self._log("video: the phone stopped mirroring")
                self.mirroring = False
        self.mirroring = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        process, self._process = self._process, None
        self.mirroring = False
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
