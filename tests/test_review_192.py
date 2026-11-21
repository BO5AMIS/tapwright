"""Review R192 (REVIEW-1.10.0-OPUS2.md), plug and forget 1.10.0: M1 the keys typed must lie inside the lock screen's own
window in the raw tree (any language, a window the tree doesn't list included), M2 the service saves flags that are
checked again at each automatic start, L3 one /status probe chain per reuse, L4 `--restart-gate`, L5 nothing saved
where no service is installed. Trees are WDA-shaped XML read through ui.read, as on the phone. Fakes only."""
import asyncio
import json
import os

import pytest
from test_service import Tasks, _svc
from test_unlock import PASSCODE, Screen, unlocker

from tapwright import client, device as device_module, service, store, ui, unlock

SB = unlock.SPRINGBOARD
SIZE = (430.0, 932.0)
KEYS = {"1": (60, 470), "2": (215, 470), "3": (370, 470), "4": (60, 560), "5": (215, 560), "6": (370, 560),
        "7": (60, 650), "8": (215, 650), "9": (370, 650), "0": (215, 740)}


def _keys() -> str:
    return "".join(f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{d}" label="{d}" enabled="true" '
                   f'visible="true" x="{x - 37}" y="{y - 37}" width="75" height="75"/>' for d, (x, y) in KEYS.items())


def _cover(inside: str = "") -> str:
    return ('<XCUIElementTypeWindow type="XCUIElementTypeWindow" name="SBCoverSheetWindow" x="0" y="0" width="430" '
            'height="932" visible="true"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="9:10" '
            f'label="9:10" x="67" y="82" width="293" height="156" visible="true"/>{inside}</XCUIElementTypeWindow>')


def _overlay(attrs: str, title: str, later: str) -> str:
    """The update passcode screen in a window of its own (the reviewer's repro, repro192/repro_unnamed_overlay.py)."""
    return (f'<XCUIElementTypeWindow type="XCUIElementTypeWindow" {attrs} x="0" y="0" width="430" height="932" '
            f'visible="true"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="{title}" label="{title}" '
            f'x="40" y="150" width="350" height="27" visible="true"/>{_keys()}'
            f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{later}" label="{later}" enabled="true" '
            'visible="true" x="120" y="820" width="190" height="44"/></XCUIElementTypeWindow>')


def _tree(*windows: str) -> ui.Tree:
    source = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="SpringBoard" '
              'label="SpringBoard" x="0" y="0" width="430" height="932" visible="true">' + "".join(windows) +
              '</XCUIElementTypeApplication></AppiumAUT>')
    tree = ui.read(source, SIZE)
    tree.app = (SB, SB)
    return tree


GERMAN = ("Code eingeben, um heute Nacht zu aktualisieren", "Später erinnern")


# ---- M1 ----
def test_the_real_lock_screens_keypad_inside_the_cover_sheet_is_found():
    tree = _tree(_cover(_keys()))
    keys = [e for e in tree.items if e.type == "Button"]
    assert keys and all(e.window == "SBCoverSheetWindow" for e in keys)
    assert unlock.find_entry(tree, SB, SIZE) is not None


@pytest.mark.parametrize("attrs", ['', 'name="Overlay" label="Overlay"', 'name="SBTransientOverlayWindow"'],
                         ids=["unnamed", "name-is-label", "named"])
def test_a_german_update_keypad_in_its_own_window_is_never_typed_on(attrs):
    """The reviewer's repro: the update keypad beside the cover sheet, its words in German, its window unnamed or not
    listed. Before: entry found, the passcode typed. Now the keys lie outside the cover sheet's window."""
    tree = _tree(_cover(), _overlay(attrs, *GERMAN))
    assert unlock.find_entry(tree, SB, SIZE) is None

    class Phone(Screen):
        async def lock_tree(self):
            self.calls.append("tree")
            return tree, SB
    u, _ = unlocker()
    screen = Phone()
    r = asyncio.run(u.unlock(screen, PASSCODE))
    assert r.code in ("not_proven", "update_prompt") and screen.pressed == [] and screen.secrets == []
    assert u.tries_left() == 2


def test_an_unnamed_windows_elements_are_marked_with_an_empty_name():
    tree = _tree(_cover(), _overlay("", *GERMAN))
    later = next(e for e in tree.items if e.label == GERMAN[1])
    assert later.window == ""  # in a window, an unnamed one: not the cover sheet's


def test_a_secure_field_outside_the_cover_sheet_is_not_used():
    field = ('<XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" name="Passcode field" '
             'label="Passcode field" x="0" y="220" width="430" height="13" visible="true"/>')
    inside = _tree(_cover(field))
    assert unlock.find_entry(inside, SB, SIZE) is not None
    beside = _tree(_cover(), f'<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" '
                             f'height="932" visible="true">{field}</XCUIElementTypeWindow>')
    assert unlock.find_entry(beside, SB, SIZE) is None


def test_keys_whose_window_is_not_known_are_never_typed_on():
    from test_unlock import keypad
    assert unlock.find_entry(ui.Tree(keypad(keys_in=None)), SB) is None


# ---- M2 ----
def test_a_moved_policy_file_falls_back_to_a_plain_serve_and_says_so(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    service._save(service.flags_from({"policy": str(policy)}))
    assert client.daemon_args()[:3] == ["serve", "--keep-alive", "--policy"]
    policy.unlink()  # moved away
    monkeypatch.setattr(client, "_SERVE_ARGS", [])
    assert client.daemon_args() == ["serve"]
    log = (store.HOME / "serve.log").read_text(encoding="utf-8")
    assert "the service's saved flags can't be used" in log and "no such file" in log
    text, _ = _svc(Tasks(), running=None).status()
    assert "saved flags can't be used" in text


def test_a_schedules_vault_is_kept_when_the_service_has_none(monkeypatch):
    service._save({"no_log": True})
    monkeypatch.setattr(client, "_SERVE_ARGS", ["--accounts", "/schedule/vault.json"])
    assert client.daemon_args() == ["serve", "--keep-alive", "--no-log", "--accounts", "/schedule/vault.json"]


def test_the_services_vault_wins_over_a_schedules(monkeypatch, tmp_path):
    vault = tmp_path / "v.json"
    vault.write_text("{}")
    service._save(service.flags_from({"accounts": str(vault)}))
    monkeypatch.setattr(client, "_SERVE_ARGS", ["--accounts", "/schedule/vault.json"])
    assert client.daemon_args() == ["serve", "--keep-alive", "--accounts", str(vault.resolve())]


def test_an_edited_file_is_never_run(monkeypatch):
    path = service.service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"args": ["serve", "--keep-alive", "--new-token"]}))
    monkeypatch.setattr(client, "_SERVE_ARGS", [])
    assert client.daemon_args() == ["serve"]


def test_a_file_from_an_earlier_build_is_read_back_as_flags():
    path = service.service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"args": ["serve", "--keep-alive", "--apps", "com.example.app", "--no-log"]}))
    assert service.saved_args() == ["serve", "--keep-alive", "--apps", "com.example.app", "--no-log"]


def test_a_start_that_exits_points_at_service_status(monkeypatch):
    service._save({})

    class Exited:
        def poll(self):
            return 2
    monkeypatch.setattr(client, "start_daemon", lambda: Exited())
    monkeypatch.setattr(client, "healthy", lambda info: None)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    with pytest.raises(client.DaemonError, match="tapwright service status"):
        client.find_or_start(wait=5)


# ---- L3 ----
def test_one_probe_chain_per_reuse(monkeypatch):
    from test_device import _fake_phone_side, _Service
    _fake_phone_side(monkeypatch, _Service)
    asks = []

    async def go():
        d = device_module.Device(log=lambda *a: None)
        d.rsd = object()

        async def alive():
            asks.append(1)
            return True
        d.wda_alive = alive

        async def lock_state():
            return None
        d.lock_state = lock_state
        d.runner = asyncio.create_task(asyncio.sleep(3600))
        assert await d._still_answers()  # the daemon's probe (_session_again_on_runner)
        kept = d.runner
        await d.start_wda("runner.id", wait=5)  # the start right after it trusts that answer
        assert d.runner is kept and len(asks) == 1
        await d.start_wda("runner.id", wait=5)  # a later start asks again (the answer is spent)
        assert len(asks) == 2
        await d.close()
    asyncio.run(go())


# ---- L4 ----
def test_serve_restart_gate_flag_sets_the_daemons_switch(monkeypatch):
    from tapwright import cli, server
    seen = {}

    async def fake_serve(**kw):
        seen["gate"] = os.environ.get("TAPWRIGHT_RESTART_GATE")
        return 0
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.delenv("TAPWRIGHT_RESTART_GATE", raising=False)
    monkeypatch.setattr("sys.argv", ["tapwright", "serve", "--restart-gate", "off"])
    assert cli.main() == 0 and seen["gate"] == "0" and not device_module.restart_gate_on()
    monkeypatch.setattr("sys.argv", ["tapwright", "serve", "--restart-gate", "on"])
    assert cli.main() == 0 and seen["gate"] == "1" and device_module.restart_gate_on()


def test_the_service_carries_the_restart_gate():
    flags = service.flags_from({"restart_gate": "off"})
    assert service.serve_args(flags) == ["serve", "--keep-alive", "--restart-gate", "off"]
    service._save(flags)
    assert service.saved_args() == ["serve", "--keep-alive", "--restart-gate", "off"]
    with pytest.raises(service.ServiceError, match="on or off"):
        service.serve_args({"restart_gate": "maybe"})


# ---- L5 ----
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_elsewhere_install_prints_and_saves_nothing(platform):
    text, code = _svc(Tasks(), platform=platform).install(["serve", "--keep-alive"], {})
    assert code == 0 and ("launchctl" in text or "systemctl" in text)
    assert not service.service_file().exists()
