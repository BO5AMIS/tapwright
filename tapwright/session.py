"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import io

from . import store, ui
from .device import Device, DeviceError


class Session:
    def __init__(self, device: Device):
        self.device = device
        self.sid = None
        self.screen = (390.0, 844.0)
        self.items: list[ui.Element] = []

    async def ensure(self) -> None:
        if self.device.rsd is None:
            await self.device.connect()
            await self.device.mount()
        runner = store.load_state().get("runner_id")
        if not runner:
            raise DeviceError("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        if self.sid and await self.device.wda_alive():
            return
        await self.device.start_wda(runner)
        self.sid = (await self.device.http("POST", "/session", {"capabilities": {"alwaysMatch": {}}}))["sessionId"]
        size = (await self.device.http("GET", f"/session/{self.sid}/window/size"))["value"]
        self.screen = (float(size["width"]), float(size["height"]))

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        await self.ensure()
        return await self.device.http(method, path.replace("{sid}", self.sid), payload, timeout)

    # ---- seeing ----
    async def look(self, screenshot: bool = True, keyboard: bool = False, max_width: int = 400):
        """(text, jpeg bytes or None): the screen as elements, and as a picture."""
        source = (await self._call("GET", "/source?format=json", timeout=60))["value"]
        self.items = ui.elements(source, self.screen, keyboard=keyboard)
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
        return ui.describe(self.items, self.screen), image

    def _target(self, ref=None, label=None, id=None, x=None, y=None, index=0):
        if x is not None and y is not None:
            return float(x), float(y), f"({round(x)}, {round(y)})"
        if not self.items:
            raise DeviceError("look first: targets refer to the elements of the last look")
        e = ui.find(self.items, ref=ref, label=label, id=id, index=index)
        cx, cy = e.center
        return cx, cy, e.line()

    # ---- acting ----
    async def tap(self, hold_ms: int = 60, **target) -> str:
        x, y, what = self._target(**target)
        await self._call("POST", "/session/{sid}/actions", ui.tap(x, y, hold_ms))
        return f"{'long-pressed' if hold_ms > 300 else 'tapped'} {what}"

    async def type_text(self, text: str, replace: bool = False, submit: bool = False, **target) -> str:
        """Type into the target (tapped first to focus it), or into whatever has focus."""
        if any(v is not None for v in target.values()):
            await self.tap(**target)
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
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def scroll(self, direction: str = "down", amount: float = 0.5) -> str:
        await self.ensure()
        x0, y0, x1, y1 = ui.scroll_points(direction, self.screen, amount)
        await self.swipe(x0, y0, x1, y1, 600)
        return f"scrolled {direction}"

    async def press_button(self, name: str) -> str:
        await self._call("POST", "/session/{sid}/wda/pressButton", {"name": name})
        return f"pressed {name}"

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
