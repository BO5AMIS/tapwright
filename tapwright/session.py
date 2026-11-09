"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import io

from . import store, ui
from .control import Refused
from .device import ConnectionLost, Device, DeviceError, NotSetUp

KEYS = {"backspace": "\b", "enter": "\n"}  # what /wda/keys types for these keys


class Session:
    def __init__(self, device: Device):
        self.device = device
        self.sid = None
        self.screen = (390.0, 844.0)
        self.scale: float | None = None  # pixels per point, read once; None until known
        self.items: list[ui.Element] = []
        self.refs = True  # False while `items` is a silent re-read: its refs aren't the agent's, so lines omit them
        self.last_point: tuple[float, float] | None = None  # where the last tap, swipe or drag began, in points

    async def ensure(self) -> None:
        """Connect, mount and start WebDriverAgent when needed. Once there is a session this is
        free: a dead line shows up as ConnectionLost on the next request instead."""
        if self.device.rsd is None:
            await self.device.connect()
            await self.device.mount()
        if self.sid is not None:
            return
        runner = store.load_state().get("runner_id")
        if not runner:
            raise NotSetUp("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        await self.device.start_wda(runner)
        sid = (await self.device.http("POST", "/session", {"capabilities": {"alwaysMatch": {}}}))["sessionId"]
        size = (await self.device.http("GET", f"/session/{sid}/window/size"))["value"]
        self.screen = (float(size["width"]), float(size["height"]))
        self.sid = sid  # committed last: a failure above leaves no half-started session

    async def reset(self) -> None:
        """Drop the connection; the next request reconnects and starts WebDriverAgent again."""
        try:
            await asyncio.wait_for(self.device.close(), 10)
        except Exception:
            pass
        self.device.rsd = self.device.tunnel = self.device.runner = None
        self.sid = None
        self.scale = None
        self.items = []
        self.last_point = None

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        await self.ensure()
        try:
            return await self.device.http(method, path.replace("{sid}", self.sid), payload, timeout)
        except ConnectionLost:
            self.sid = None  # the next call starts WebDriverAgent again
            raise
        except DeviceError as e:
            if "invalid session" in str(e).lower():  # WebDriverAgent restarted: the session is gone
                self.sid = None
                raise ConnectionLost(str(e)) from e
            raise

    # ---- seeing ----
    async def _app(self) -> tuple[str, str] | None:
        """(name, bundle id) of the foreground app, or None when WebDriverAgent can't say."""
        try:
            info = (await self._call("GET", "/wda/activeAppInfo")).get("value") or {}
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        bundle = str(info.get("bundleId") or "")
        return (str(info.get("name") or bundle), bundle) if bundle else None

    async def _scale(self) -> float | None:
        if self.scale is None:
            try:
                value = (await self._call("GET", "/session/{sid}/wda/screen")).get("value") or {}
                self.scale = float(value["scale"])
            except ConnectionLost:
                raise
            except (DeviceError, KeyError, TypeError, ValueError):
                pass  # the header just leaves out "@3x"
        return self.scale

    async def look(self, screenshot: bool = True, keyboard: bool = False, max_width: int = 400):
        """(text, jpeg bytes or None): the screen as elements, and as a picture."""
        source = (await self._call("GET", ui.SOURCE_PATH, timeout=60))["value"]
        tree = ui.read(source, self.screen, keyboard=keyboard)
        self.items = tree.items
        app, scale = await self._app(), await self._scale()
        image = None
        if screenshot:
            png = base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.width > max_width:
                img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            image = buf.getvalue()
        return ui.describe(tree.items, self.screen, scale, app, tree.alert, tree.keyboard, tree.more), image

    async def refresh(self) -> ui.Tree:
        """Read the tree into `items` and nothing else: what resolving a target needs."""
        source = (await self._call("GET", ui.SOURCE_PATH, timeout=60))["value"]
        tree = ui.read(source, self.screen)
        self.items = tree.items
        return tree

    def _target(self, target: ui.Target) -> tuple[float, float, str]:
        """(x, y, what to say) for a target, on the elements of the last look."""
        hit = ui.resolve(self.items, target, self.refs)
        if isinstance(hit, tuple):
            return hit[0], hit[1], f"({round(hit[0])}, {round(hit[1])})"
        cx, cy = hit.center
        return cx, cy, hit.line(ref=self.refs)

    # ---- acting ----
    async def tap(self, target: ui.Target, hold_ms: int = 60) -> str:
        x, y, what = self._target(target)
        await self._call("POST", "/session/{sid}/actions", ui.tap(x, y, hold_ms))
        self.last_point = (x, y)
        return f"{'long-pressed' if hold_ms > 300 else 'tapped'} {what}"

    async def type_text(self, text: str, replace: bool = False, submit: bool = False,
                        target: ui.Target | None = None) -> str:
        """Type into the target (tapped first to focus it), or into whatever has focus. A point
        names the input under it, not a touch: with none there, nothing is typed."""
        if target is not None and target.point is not None:
            field = ui.at_point(self.items, target.point, "input")
            if field is None:
                x, y = target.point
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes typing.")
            target = ui.Target(ref=field.ref)
        if target is not None:
            await self.tap(target)
            await asyncio.sleep(0.4)
        if replace:
            focused = await self._call("GET", "/session/{sid}/element/active")
            element = (focused.get("value") or {}).get("ELEMENT") or next(iter((focused.get("value") or {}).values()), None)
            if element:
                await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
        await self._call("POST", "/session/{sid}/wda/keys", {"value": list(text + ("\n" if submit else ""))})
        return f"typed {len(text)} characters" + (" and pressed return" if submit else "")

    async def swipe(self, x0: float, y0: float, x1: float, y1: float, duration_ms: int = 350) -> str:
        await self._call("POST", "/session/{sid}/actions", ui.swipe(x0, y0, x1, y1, duration_ms))
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def path(self, points: list) -> str:
        """Replay a finger path: [(t_ms from 0, x, y), ...] in points."""
        await self._call("POST", "/session/{sid}/actions", ui.path(points))
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def scroll(self, direction: str = "down", amount: float = 0.5) -> str:
        await self.ensure()
        x0, y0, x1, y1 = ui.scroll_points(direction, self.screen, amount)
        await self.swipe(x0, y0, x1, y1, 600)
        return f"scrolled {direction}"

    async def press_button(self, name: str) -> str:
        if name == "home":  # pressButton "home" does nothing on iOS 26 (measured); this one waits for the home screen
            await self._call("POST", "/wda/homescreen", {})
        else:
            await self._call("POST", "/session/{sid}/wda/pressButton", {"name": name})
        return f"pressed {name}"

    async def key(self, name: str) -> str:
        """backspace or enter, into whatever field has focus."""
        if name not in KEYS:
            raise Refused("bad_params", f"key must be one of {', '.join(KEYS)}, not {name!r}")
        await self._call("POST", "/session/{sid}/wda/keys", {"value": [KEYS[name]]})
        return f"pressed {name}"

    async def settings(self, values: dict) -> None:
        """WebDriverAgent settings, e.g. the MJPEG stream's frame rate and size."""
        await self._call("POST", "/session/{sid}/appium/settings", {"settings": values})

    async def locked(self) -> bool:
        return bool((await self._call("GET", "/session/{sid}/wda/locked")).get("value"))

    async def alert(self, action: str) -> str:
        """accept, dismiss, or text: the system alert on screen (permission prompts and the like)."""
        if action == "text":
            return (await self._call("GET", "/session/{sid}/alert/text")).get("value") or "(no alert)"
        await self._call("POST", f"/session/{{sid}}/alert/{action}", {})
        return f"alert {action}ed"

    # ---- apps ----
    async def apps(self) -> list[dict]:
        await self.ensure()
        return await self.device.user_apps()

    async def launch(self, bundle_id: str) -> str:
        await self._call("POST", "/session/{sid}/wda/apps/launch", {"bundleId": bundle_id}, timeout=60)
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id: str) -> str:
        await self._call("POST", "/session/{sid}/wda/apps/terminate", {"bundleId": bundle_id})
        return f"closed {bundle_id}"

    async def open_url(self, url: str) -> str:
        await self._call("POST", "/session/{sid}/url", {"url": url})
        return f"opened {url}"

    async def close(self) -> None:
        await self.device.close()
