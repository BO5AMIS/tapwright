"""tapwright: let LLM agents test apps on your real iPhone, from Windows, Linux or macOS.

  tapwright setup     one time, phone on USB: pair, sign and install WebDriverAgent,
                     pair for network use. Signing: --asc-key-id/--asc-issuer-id/--asc-key-file
                     with --bundle-prefix (App Store Connect API key), or --p12/--profile
  tapwright doctor [--fix] [--unattended [--accounts FILE]] [--policy FILE]
                     check the setup and say what's missing; --fix repairs what it can (the
                     developer disk image, WebDriverAgent and its signature, network pairing)
  tapwright smoke --app BUNDLE_ID
                     open an app, read its first screen, save a screenshot, close it
  tapwright probe [--host IP] [--watch]
                     does it work over the network? Learns the pairing port on the local
                     Wi-Fi; with --host (e.g. the phone's Tailscale IP) tries that address
  tapwright serve [--apps ID[,ID] | --dedicated | --phone] [--address IP] [--port N] [--keep-alive] [--new-token] [--stop]
                     the background process that owns the phone: the live view in your
                     browser, the agent tools and an HTTP API. Agent tools start it by
                     themselves; it stops after 30 minutes unused (--keep-alive: never)
  tapwright view     open the live view in your browser (starts the daemon if needed)
  tapwright record-video --out FILE.mp4 [--seconds N] [--fps N]
                     record the live view as an H.264 MP4 in real time, with FILE.frames.json (each frame's
                     capture time, on the action log's clock); needs `pip install "tapwright[video]"`
  tapwright report [--since TIME] [--client ID]
                     where an agent's calls went, from the action log: calls per tool, failures,
                     and waste to cut (the agent tool session_report answers the same)
  tapwright grants --policy FILE [--revoke-all]
                     the grants of a policy file, today's counts and the last uses; --revoke-all revokes
                     every one and stops a running route; `grants unlock --policy FILE` removes a stuck edit lock
  tapwright schedule add ROUTE (--at "09:00 daily" | --on-file DIR | --on-notification APP) [--with k=v|k=@file]
                     [--name NAME] [--policy FILE] [--accounts FILE] [--maps DIR]; schedule list; schedule remove NAME;
                     schedule unlock NAME (a stuck run lock); tapwright run-schedule NAME: one scheduled run, now, with
                     no agent (Windows: a time schedule is a Task Scheduler task that wakes the PC)
  tapwright unlock-reset
                     after the phone refused the vault's passcode, the passcode unlock types nothing until you check
                     phone.passcode and run this (at most 2 attempts an hour and 3 a day in any case)
  tapwright bug <report folder> <finding id> [--with-picture] [--verify N] [--accounts FILE]; bug list [--app ID];
                     bug verify <bug id> [--verify N]; bug export <bug id> --to zip|issue [--repo O/N] [--yes]
                     a one-page bug report of an explore finding under ~/.tapwright/bugs (the phone only to verify)
  tapwright mcp [--screenshots DIR]  the MCP server for agents (stdio); it talks to the daemon
  tapwright install --client claude|codex|cursor --apps ID[,ID] [--dedicated | --phone] [--project DIR]
                     [--policy FILE | --no-policy] [--ux-rules FILE]
                     connect an agent in one go: register the MCP server and install the skill
  tapwright starters install [--maps DIR] [--app ID]...
                     put the starter maps and routes for the phone's own apps (Reminders, Notes, ...) into your
                     maps; a route of yours with the same name is kept
  tapwright replay FILE|NAME... (--apps ID[,ID] | --dedicated | --phone) [--junit OUT] [--update-references]
                                [--repair suggest|apply [--adopt]]
                     run scripts saved with the record tool, no agent; exit 0 all held, 1 one stopped,
                     2 one couldn't start

Connection: USB when the phone is plugged in, else the local network; serve --address dials an
address such as the phone's Tailscale IP (experimental).
"""
import argparse
import asyncio
import os
import re
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import diagnostics, store
from .repair import Phone, run_fixes


def ok(msg):
    print(f"  ok    {msg}", flush=True)


def note(msg):
    print(f"  note  {msg}", flush=True)


def fail(msg):
    print(f"  FAIL  {msg}", flush=True)
    return 1


def _daemon_owns_phone() -> bool:
    from . import client
    return client.healthy(store.read_serve_info()) is not None


DAEMON_RUNNING = "tapwright serve is running and owns the phone: stop it first with `tapwright serve --stop`"
INSTALL_WAIT = 180.0  # seconds the phone gets to install WebDriverAgent (it takes about 20)


# ---------- setup ----------

async def wda_on_phone(lockdown, runner_id: str) -> bool:
    """Is the WebDriverAgent runner app installed on the phone under this bundle id?"""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    async with InstallationProxyService(lockdown=lockdown) as proxy:
        apps = await proxy.get_apps(application_type="User", bundle_identifiers=[runner_id])
    return runner_id in apps


async def install_wda(lockdown, udid: str) -> str:
    """Sign WebDriverAgent with the stored identity and install it. Returns the runner's bundle id. Raises
    RuntimeError with a sentence for the user (signing failed, or the phone didn't finish)."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    from . import repair, signing
    ipa, runner_id = signing.sign_wda(store.HOME / "build")
    try:
        async with InstallationProxyService(lockdown=lockdown) as proxy:
            await asyncio.wait_for(proxy.install_from_local(ipa), INSTALL_WAIT)
    except asyncio.TimeoutError:
        # iOS keeps an install open while the app it replaces still runs (seen 2026-11-11, IXErrorDomain 48)
        raise RuntimeError(f"the phone didn't finish installing WebDriverAgent in {INSTALL_WAIT:.0f} s. If a test "
                           "runner is still running on the phone (another tool, or Xcode), stop it, then run "
                           "this again.") from None
    store.save_state(udid=udid, runner_id=runner_id)
    repair.record_install(repair.profile_expiry())
    return runner_id


def network_paired(udid: str) -> bool:
    """Is there a saved network pairing record for this phone? False when it can't tell."""
    try:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
        return any(ident == udid for ident, _, _ in iter_remote_pair_records_by_identifier())
    except Exception:
        return False


NEXT_STEPS = ("\nNext:\n"
              "  1. tapwright smoke --app <bundle id of one of your apps>\n"
              "  2. tapwright install --client claude --apps <bundle id>     (or --client codex, --client cursor)\n"
              "Keep it working later with: tapwright doctor --fix")


async def wda_current(lockdown, runner_id: str) -> bool:
    """Is WebDriverAgent installed under `runner_id`, signed with a profile that lasts beyond REINSTALL_DAYS?"""
    from . import repair
    if store.load_state().get("runner_id") != runner_id or repair.recorded_install_expiry() is None:
        return False
    facts = repair.WdaFacts(installed=await wda_on_phone(lockdown, runner_id),
                            installed_expiry=repair.recorded_install_expiry(), profile=repair.profile_expiry(),
                            profile_has_phone=True, certificate=repair.certificate_expiry(), **repair.builds())
    return repair.decide_wda(facts, repair.now_utc()).verdict == "ok"


async def setup(a) -> int:
    """One time, and again whenever something needs redoing: a second run skips the steps already done
    (WebDriverAgent installed and signed for another 14 days, network pairing saved) and ends with the
    commands to run next. `--force` does every step again."""
    from pymobiledevice3 import usbmux
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService

    from . import repair, signing
    if _daemon_owns_phone():  # its WebDriverAgent is running: iOS would hold the new one's install open
        return fail(DAEMON_RUNNING)
    print("1. The iPhone, on USB")
    usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"]
    if not usb:
        return fail("no iPhone on USB. Plug it in and unlock it. (Windows: install Apple Devices from the "
                    "Microsoft Store; Linux: usbmuxd.)")
    udid = a.udid or usb[0].serial
    lockdown = await create_using_usbmux(serial=udid, autopair=True)  # the phone asks "Trust This Computer?"
    name = lockdown.all_values.get("DeviceName") or "iPhone"
    ok(f"{name} ({lockdown.display_name or lockdown.product_type}, iOS {lockdown.product_version}) {udid}")

    print("2. Developer Mode")
    if not await lockdown.get_developer_mode_status():
        from pymobiledevice3.services.amfi import AmfiService
        await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
        return fail("Developer Mode is off. On the iPhone: Settings > Privacy & Security > Developer Mode, turn "
                    "it on, restart, confirm Turn On. Then run setup again.")
    ok("on")

    print("3. Signing identity")
    new_identity = bool(a.asc_key_id or a.p12)
    if a.asc_key_id:
        if not a.bundle_prefix:
            return fail("--bundle-prefix is needed with an API key, e.g. com.yourname.tapwright "
                        "(the App ID com.yourname.tapwright.* is created for WebDriverAgent)")
        signing.create_identity(signing.AppStoreConnect(a.asc_key_id, a.asc_issuer_id, a.asc_key_file),
                                udid, a.bundle_prefix)
        ok("development certificate and profile from App Store Connect")
    elif a.p12:
        signing.import_identity(a.p12, a.p12_password or "", a.profile)
        ok("imported your certificate and profile")
    elif not signing.PROFILE_FILE.exists():
        return fail("no signing identity yet: pass an App Store Connect API key (--asc-key-id, --asc-issuer-id, "
                    "--asc-key-file, --bundle-prefix) or --p12 and --profile. See docs/setup.md.")
    else:
        ok("already have one (kept)")
    profile = signing.read_profile()
    if udid not in profile.get("ProvisionedDevices", []):
        return fail("the profile doesn't include this iPhone; make a new one that does (with an API key, "
                    "setup does it)")
    ok(f"profile '{profile['Name']}', expires {profile['ExpirationDate']:%Y-%m-%d}")
    stored = repair.profile_expiry()
    if not new_identity and stored and repair.expiry_lines([stored], repair.now_utc(), repair.REINSTALL_DAYS):
        return fail(f"the stored profile ends {stored.date()}. Run setup again with your App Store Connect key "
                    "(--asc-key-id, --asc-issuer-id, --asc-key-file, --bundle-prefix): it makes a new profile.")

    print("4. WebDriverAgent")
    runner_id = signing.runner_ids(profile)[1]
    if not new_identity and not a.force and await wda_current(lockdown, runner_id):
        ok(f"already installed and signed ({runner_id}); skipped (--force installs it again)")
    else:
        try:
            runner_id = await install_wda(lockdown, udid)
        except RuntimeError as e:
            return fail(str(e))
        ok(f"installed as {runner_id}")

    print("5. Network pairing (Wi-Fi and remote use)")
    if network_paired(udid) and not a.force:
        ok("already paired; skipped")
    else:
        service = await RemotePairingLockdownService.create(lockdown)
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            pass
        finally:
            await service.close()
        ok("paired")
    store.save_state(udid=udid, runner_id=runner_id, phone=name)
    print("\nDone." + NEXT_STEPS)
    return 0


# ---------- doctor ----------

class UsbPhone(Phone):
    """The repairs of `doctor --fix`, done on the phone over USB."""

    def __init__(self, udid: str):
        from .device import Device
        self.udid = udid
        self.device = Device(udid=udid, log=lambda msg: None)
        self._lockdown = None

    async def _connected(self):
        if self.device.rsd is None:
            await self.device.connect()
        return self.device

    async def _lock(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        if self._lockdown is None:
            self._lockdown = await create_using_usbmux(serial=self.udid, autopair=True)
        return self._lockdown

    async def image_mounted(self) -> bool:
        from .device import TEST_SERVICE
        device = await self._connected()
        return TEST_SERVICE in device.rsd.peer_info["Services"]

    async def mount_image(self) -> None:
        await (await self._connected()).mount()

    async def wda_installed(self) -> bool:
        from . import signing
        try:
            runner_id = signing.runner_ids(signing.read_profile())[1]
        except (OSError, RuntimeError, KeyError, ValueError):
            return False  # no usable profile: the decision says so
        return await wda_on_phone(await self._lock(), runner_id)

    async def install_wda(self) -> None:
        await install_wda(await self._lock(), self.udid)

    async def refresh_pairing_port(self) -> tuple[int | None, int | None]:
        from pymobiledevice3.bonjour import browse_remotepairing
        saved = store.load_state().get("pairing_port")
        found = await browse_remotepairing(timeout=6)
        if not found:
            return None, saved
        port = found[0].port
        if port != saved:
            store.save_state(pairing_port=port)
        return port, saved

    async def close(self) -> None:
        await self.device.close()


async def doctor_fix(problems: int) -> int:
    """The repairs of `doctor --fix`, with the phone on USB. Returns the problems found so far plus those left."""
    from pymobiledevice3 import usbmux
    if _daemon_owns_phone():
        return problems + fail(DAEMON_RUNNING)
    print("\nRepairs (phone on USB)")
    usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"]
    if not usb:
        return problems + fail("--fix needs the iPhone on USB. Plug it in and unlock it, then run it again.")
    phone = UsbPhone(configured_phone([d.serial for d in usb]))
    try:
        return problems + await run_fixes(phone, lambda kind, msg: {"ok": ok, "note": note, "fail": fail}[kind](msg))
    finally:
        await phone.close()


def configured_phone(usb: list[str]) -> str:
    """The phone doctor means among those on USB: the stored one when it is plugged in, else the first."""
    udid = store.load_state().get("udid")
    return udid if udid in usb else usb[0]


async def lockdown_needs(udid: str, connect=None) -> list[str]:
    """What the phone on USB says only a person can clear (needs.from_lockdown). Reads nothing it can't, and asks for
    no pairing: an untrusted computer is told, not paired."""
    from pymobiledevice3.exceptions import (NotPairedError, PairingDialogResponsePendingError, PasswordRequiredError,
                                            UserDeniedPairingError)

    from . import needs
    if connect is None:
        from pymobiledevice3.lockdown import create_using_usbmux

        def connect(serial):
            return create_using_usbmux(serial=serial, autopair=False)
    try:
        lockdown = await asyncio.wait_for(connect(udid), 10)
    except (NotPairedError, PairingDialogResponsePendingError, UserDeniedPairingError):
        return needs.from_lockdown(False, False, None)
    except PasswordRequiredError:
        return needs.from_lockdown(True, True, None)
    except Exception:  # can't be read: not listed
        return []
    if not lockdown.paired:  # autopair=False hands back an unpaired client instead of raising
        return needs.from_lockdown(False, False, None)
    try:
        mode = await asyncio.wait_for(lockdown.get_developer_mode_status(), 10)
    except Exception:  # an iOS without the setting, or no answer
        mode = None
    return needs.from_lockdown(True, False, mode if isinstance(mode, bool) else None)


async def lockdown_facts(udid: str, connect=None) -> dict:
    """What the phone on USB says for `doctor --unattended`, as the fields of needs.Facts it could read (and no more):
    trusted, locked, Developer Mode, the battery. Asks for no pairing. A phone that can't be read gives {}."""
    from pymobiledevice3.exceptions import (NotPairedError, PairingDialogResponsePendingError, PasswordRequiredError,
                                            UserDeniedPairingError)
    if connect is None:
        from pymobiledevice3.lockdown import create_using_usbmux

        def connect(serial):
            return create_using_usbmux(serial=serial, autopair=False)
    try:
        lockdown = await asyncio.wait_for(connect(udid), 10)
    except (NotPairedError, PairingDialogResponsePendingError, UserDeniedPairingError):
        return {"trusted": False}
    except PasswordRequiredError:
        return {"trusted": True, "locked": True}
    except Exception:  # can't be read: nothing is claimed
        return {}
    if getattr(lockdown, "paired", True) is False:  # autopair=False hands back an unpaired client instead of raising
        return {"trusted": False}
    try:
        mode = await asyncio.wait_for(lockdown.get_developer_mode_status(), 10)
    except Exception:  # an iOS without the setting, or no answer
        mode = None
    try:
        battery = await asyncio.wait_for(lockdown.get_value("com.apple.mobile.battery", "BatteryCurrentCapacity"), 10)
    except Exception:
        battery = None
    percent = battery if isinstance(battery, int) and not isinstance(battery, bool) and 0 <= battery <= 100 else None
    return {"trusted": True, "locked": False, "developer_mode": mode if isinstance(mode, bool) else None,
            "battery": percent}


def passcode_given(accounts: str | None) -> bool:
    """Is there an accounts file (`--accounts FILE`) with a `phone.passcode`? Only whether: the value is never read out."""
    if not accounts:
        return False
    from . import accounts as accountlib
    passcode = accountlib.load(str(Path(accounts).expanduser().resolve())).passcode  # (a version 1 or 2 file, protected too)
    return isinstance(passcode, str) and bool(passcode.strip())


def vault_problem(accounts: str | None) -> str | None:
    """Why the vault file given to doctor can't be used (its own words, never a value), or None."""
    if not accounts:
        return None
    from . import accounts as accountlib
    return accountlib.load(str(Path(accounts).expanduser().resolve())).problem


def runner_facts() -> dict:
    """`runner` and `runner_why` for needs.Facts: whether the runner setup recorded is installed and current (its
    build is this Tapwright's, its signature lasts beyond the re-sign margin), read from what setup recorded."""
    from . import repair
    state = store.load_state()
    if not state.get("runner_id"):
        return {"runner": "missing", "runner_why": "setup hasn't installed it"}
    try:
        facts = repair.WdaFacts(installed=True, installed_expiry=repair.recorded_install_expiry(),
                                profile=repair.profile_expiry(), profile_has_phone=True,
                                certificate=repair.certificate_expiry(), **repair.builds())
        decision = repair.decide_wda(facts, repair.now_utc())
    except Exception as e:  # (a profile that can't be read)
        return {"runner": "setup", "runner_why": f"its signing can't be read: {type(e).__name__}"}
    return {"runner": decision.verdict, "runner_why": decision.reason}


def wake_timers(run=None, platform: str | None = None) -> bool | None:
    """Windows: does the power plan let a scheduled task wake the PC (Sleep > Allow wake timers = Enable on mains
    power)? Read only (`powercfg /query`). None elsewhere, or when it can't be read."""
    if (platform or sys.platform) != "win32":
        return None
    if run is None:
        def run(argv):
            done = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            return done.returncode, done.stdout
    try:
        code, out = run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"])
    except Exception:
        return None
    values = re.findall(r"0x([0-9a-fA-F]+)", out or "") if code == 0 else []
    if not values:
        return None
    return int(values[0], 16) == 1  # the AC index (the DC one follows): 0 Disable, 1 Enable, 2 important ones only


def scheduled_tasks(module=None) -> dict:
    """`tasks` and `tasks_missing` for needs.Facts: the schedules (§22) and whether each has its Task Scheduler task.
    {} when this Tapwright has no schedules, or they can't be read."""
    if module is None:
        try:
            from . import schedules as module  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        names = list(module.names())
        scheduler = module.Scheduler()
        missing = tuple(n for n in names if scheduler.exists(n) is False)
    except Exception:
        return {}
    polling: list[str] = []
    for n in names:
        try:
            trigger = module.load(n).trigger
        except Exception:
            continue
        if trigger and "notification" in trigger:
            polling.append(n)
    return {"tasks": len(names), "tasks_missing": missing, "polling": tuple(polling)}


async def unattended_facts(usb: list[str], accounts: str | None, connect=None, status: dict | None = None,
                           machine: bool = False) -> dict:
    """The fields of needs.Facts doctor could read: the phone on USB, the running daemon's state (`status`, its health
    answer), the vault file (whether it gives the passcode; never the value), and with `machine` this computer's side
    (the runner setup recorded, the wake timers, the scheduled tasks)."""
    from . import needs
    facts: dict = {"passcode_given": passcode_given(accounts), "vault_problem": vault_problem(accounts)}
    if facts["passcode_given"]:
        from . import unlock
        facts["unlock_stopped"] = unlock.Ledger().stopped()
    if usb:
        facts.update(await lockdown_facts(configured_phone(usb), connect))
    if status is not None:
        if status.get("state") == "locked":
            facts["locked"] = True
        if needs.phone_moment(status.get("state"), status.get("detail")) is needs.PASSCODE_PROMPT:
            facts["ui_automation"] = False  # iOS asked for the passcode before the automation started
    if machine:
        facts["reachable"] = bool(usb) or (status is not None and status.get("state") in ("live", "locked"))
        facts.update(runner_facts())
        facts["wake_timers"] = wake_timers()
        facts.update(scheduled_tasks())
    return facts


async def unattended_stops(usb: list[str], accounts: str | None, connect=None, status: dict | None = None,
                           machine: bool = False) -> list:
    """What would stop a run with nobody at the phone (needs.unattended), from what the computer can read."""
    from . import needs
    return needs.unattended(needs.Facts(**await unattended_facts(usb, accounts, connect, status, machine)))


def print_unattended(stops: list, ready: list[str] | None = None, notes: list[str] | None = None) -> None:
    if ready:
        print("\nReady for an unattended run:")
        for line in ready:
            print(f"  - {line}", flush=True)
    if notes:
        print("\nGood to know:")
        for line in notes:
            print(f"  - {line}", flush=True)
    print("\nWhat would stop an unattended run:" if stops else "\nNothing doctor can see would stop an unattended run.")
    for stop in stops:
        print(f"  - {stop.what}\n    Fix: {stop.fix}", flush=True)


async def needs_person(usb: list[str]) -> list[str]:
    """The things only the phone's owner can clear, as far as they can be read: from the running daemon when there
    is one (it owns the phone), else from the phone on USB."""
    from . import client, needs
    status = client.healthy(store.read_serve_info())
    if status is not None:
        return needs.from_state(status.get("state"), status.get("detail"))
    return await lockdown_needs(configured_phone(usb)) if usb else []


def print_needs(found: list[str]) -> None:
    if found:
        print("\nNeeds a person:")
        for item in found:
            print(f"  - {item}", flush=True)


def report_checkout(package: Path | None = None) -> int:
    """doctor's line on where Tapwright runs from (see checkout.py). Returns the problems it found."""
    from . import checkout
    verdict = checkout.check(package or Path(__file__).resolve().parent)
    if verdict is None:
        return 0
    kind, message = verdict
    {"ok": ok, "note": note, "fail": fail}[kind](message)
    return 1 if kind == "fail" else 0


def report_accounts(path: str | None) -> int:
    """`doctor --accounts FILE`: whether the test accounts file can be used (AGENT-TOOLS §18.6). The number of problems."""
    if not path:
        return 0
    from . import accounts
    loaded = accounts.load(str(Path(path).expanduser().resolve()))
    if loaded.note is not None:
        for line in loaded.note.split("Note: "):
            if line.strip():
                note(line.strip())
    if loaded.problem is not None:
        return fail(loaded.problem)
    ok(f"test accounts: {', '.join(loaded.names) or 'none'} (values are never shown)")
    if loaded.version == accounts.VERSION2:
        kept = {"dpapi-user": "protected with Windows DPAPI", "keychain": "protected in the macOS keychain"}
        ok(f"vault: {', '.join(loaded.entry_names) or 'no entries'}; {kept.get(loaded.protection or '', 'plain')}"
           + (f"; one-time codes on for {', '.join(loaded.otp)}" if loaded.otp else ""))
    return 0


def report_grants(policy: str | None) -> int:
    """`doctor --policy FILE`: the grants this policy file refuses (AGENT-TOOLS §19.1) and a stuck edit lock, with its
    path (§19.5). The number of problems."""
    if not policy:
        return 0
    from . import grants, grantscmd
    path = Path(policy).expanduser().resolve()
    problems = 0
    book = grants.Book(path, None)
    for line in book.notes():
        note(line.removeprefix("Note: "))
    stuck = grantscmd.stuck_lock(str(path))
    for lock, why in stuck:
        problems += fail(f"the policy file's edit lock {lock} is stuck: {why}. Remove it with: "
                         f"tapwright grants unlock --policy {path}")
    if not book.notes() and not stuck:
        ok(f"policy file {path}: grants load, no stuck edit lock")
    return problems


def report_schedules() -> int:
    """`doctor`: a schedule's run lock that is stuck, with its path and the command that removes it (§22.3). Problems."""
    from . import schedules
    stuck = schedules.stuck_locks()
    for n, lock, why in stuck:
        fail(f"the run lock {lock} of schedule \"{n}\" is stuck: {why}. Remove it with: tapwright schedule unlock {n}")
    phone = schedules.stuck_phone_lock()
    for lock, why in phone:
        fail(f"the schedules' phone lock {lock} is stuck: {why}. Remove it with: tapwright schedule unlock --phone")
    return len(stuck) + len(phone)


async def doctor(a) -> int:
    """Check the setup. With `--fix`, repair what can be repaired with the signing material already stored."""
    from importlib.metadata import version
    problems = 0
    ok(f"Python {sys.version.split()[0]}, pymobiledevice3 {version('pymobiledevice3')}")
    problems += report_checkout()
    if problems:  # before the signing modules are imported: a conflicted one would raise SyntaxError first
        return problems
    problems += report_accounts(getattr(a, "accounts", None))
    problems += report_grants(getattr(a, "policy", None))
    problems += report_schedules()
    from pymobiledevice3 import usbmux

    from . import repair, signing
    usb: list[str] = []
    try:
        devices = await usbmux.list_devices()
        usb = [d.serial for d in devices if d.connection_type == "USB"]
        ok(f"device service reachable; on USB: {usb or 'none'}")
    except Exception as e:
        problems += fail(f"device service unreachable ({e}). Windows: install Apple Devices (Microsoft Store); "
                         "Linux: usbmuxd")
    if signing.PROFILE_FILE.exists():
        p = signing.read_profile()
        expired = p["ExpirationDate"].timestamp() < time.time()
        (fail if expired else ok)(f"profile '{p['Name']}' expires {p['ExpirationDate']:%Y-%m-%d}, "
                                  f"{len(p.get('ProvisionedDevices', []))} device(s)")
        problems += expired
    else:
        problems += fail("no signing identity: run tapwright setup")
    now = repair.now_utc()
    for e in (repair.profile_expiry(), repair.certificate_expiry()):
        for line in repair.expiry_lines([e], now) if e else []:
            if e.when > now:  # an expired profile has failed above
                note(f"{line}. Renew it before then: tapwright setup (App Store Connect key, phone on USB)")
            elif e.what == "certificate":
                problems += fail(f"{line}. Run tapwright setup again with your App Store Connect key")
    state = store.load_state()
    if state.get("runner_id"):
        ok(f"WebDriverAgent set up for {state.get('phone') or state.get('udid')}")
    elif a.fix:
        note("WebDriverAgent isn't recorded as installed; checking the phone below")
    else:
        problems += fail("WebDriverAgent not installed yet: run tapwright setup (phone on USB), or tapwright "
                         "doctor --fix if it is set up before")
    if state.get("pairing_port"):
        ok(f"network pairing port known: {state['pairing_port']} (tapwright probe refreshes it)")
    else:
        note("network pairing port unknown: run tapwright probe with the phone on the same Wi-Fi")
    if a.fix:
        problems = await doctor_fix(problems)
    elif problems:
        note("tapwright doctor --fix repairs what it can (phone on USB)")
    waiting = await needs_person(usb) if not a.fix else []
    print_needs(waiting)
    if a.unattended:  # each thing that would stop a run without a person counts as a problem
        from . import client, needs
        facts = needs.Facts(**await unattended_facts(usb, a.accounts, status=client.healthy(store.read_serve_info()),
                                                     machine=True))
        stops = needs.unattended(facts)
        print_unattended(stops, needs.unattended_ready(facts), needs.unattended_notes(facts))
        problems += len(stops)
    print(f"\n{problems} problem(s)." if problems else
          "\nSet up, but the phone needs a person (above)." if waiting else "\nAll set.")
    return 1 if problems else 0


# ---------- probe: does it work over the network / a VPN? ----------

def _tcp(host: str, port: int, timeout: float = 3.0) -> str:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.settimeout(timeout)
    try:
        s.connect((host.split("%")[0], port))
        return "open"
    except socket.timeout:
        return "no answer"
    except OSError:
        return "refused"
    finally:
        s.close()


async def probe(a) -> int:
    if _daemon_owns_phone():
        return fail(DAEMON_RUNNING)
    from pymobiledevice3.bonjour import browse_remotepairing

    from .device import Device
    from .session import Session
    print("1. The phone's network pairing service on this Wi-Fi (mDNS)")
    found = await browse_remotepairing(timeout=6)
    if found:
        port = found[0].port
        store.save_state(pairing_port=port)
        ok(f"port {port} at {[x.full_ip for x in found[0].addresses]}")
    else:
        port = store.load_state().get("pairing_port")
        note(f"not found on this network; using the last known port {port}" if port else
             "not found, and no known port: put the phone on the same Wi-Fi once")
        if not port:
            return 1
    if a.host:
        print(f"2. Reachable at {a.host}?")
        reach = _tcp(a.host, port)
        if reach != "open":
            return fail(f"{a.host}:{port} {reach}: the phone doesn't offer its developer service at that address")
        ok(f"{a.host}:{port} open")
    print("3. Developer tunnel, WebDriverAgent, a screenshot")
    session = Session(Device(host=a.host, port=port if a.host else None))
    try:
        t0 = time.time()
        text, image = await session.look()
        ok(f"over {session.device.via} in {time.time() - t0:.1f} s; {text.splitlines()[0]}")
        shot = Path(f"tapwright-probe-{int(time.time())}.jpg")
        shot.write_bytes(image)
        ok(f"screenshot: {shot}")
        if a.watch:
            print("4. Watching (Ctrl+C stops). Now change the phone's network (e.g. Wi-Fi off).")
            while True:
                alive = await session.device.wda_alive()
                print(f"  {time.strftime('%H:%M:%S')}  {'alive' if alive else 'LOST'}", flush=True)
                await asyncio.sleep(2)
        return 0
    finally:
        await session.close()


# ---------- smoke test ----------

async def smoke(a) -> int:
    if _daemon_owns_phone():
        return fail(DAEMON_RUNNING)
    from .device import Device
    from .session import Session
    session = Session(Device(host=a.host))
    try:
        print(f"Opening {a.app} ...")
        t0 = time.time()
        await session.launch(a.app)
        await asyncio.sleep(a.wait)
        text, image = await session.look()
        print(text)
        shot = Path(f"tapwright-smoke-{int(time.time())}.jpg")
        shot.write_bytes(image)
        ok(f"screen read in {time.time() - t0:.1f} s; screenshot {shot}")
        if not a.keep_open:
            await session.terminate(a.app)
        return 0
    finally:
        await session.close()


# ---------- the daemon ----------

def stop_daemon() -> int:
    from . import client
    info = store.read_serve_info()
    if not client.healthy(info):
        print("tapwright serve isn't running")
        return 0
    client.shutdown(info)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.healthy(store.read_serve_info()) is None:
            print("stopped")
            return 0
        time.sleep(0.3)
    print("asked it to stop; it's still running, see ~/.tapwright/serve.log")
    return 1


def view() -> int:
    import webbrowser

    from . import client
    try:
        url = client.viewer_url(client.find_or_start())
    except client.DaemonError as e:
        print(f"couldn't start the live view: {e}")
        return 1
    # The link carries the key, and is never printed: an app around the terminal that hides keys in what it shows
    # (T3 Code shows ?token=SECRET) opened that hidden copy as a tab, which can only fail (2026-11-12).
    print(f"Tapwright live view: opening {url.split('?', 1)[0]} in your browser (the key is in the link, not shown)")
    webbrowser.open(url)
    return 0


def skill(a) -> int:
    """`tapwright skill install`: put the agent skill where Claude Code, Codex and other agents look for skills,
    and the driver agent where Claude Code and Codex look for agents."""
    from . import driver, skills
    if a.action == "path":
        print(skills.SOURCE)
        return 0
    try:
        driver.models_for(a.driver_model, list(driver.HOSTS))
        written = skills.install(a.project)
    except (OSError, ValueError) as e:
        print(f"couldn't install the skill: {e}")
        return 1
    print(skills.installed_text(written))
    try:
        wrote = driver.install(list(driver.HOSTS), a.project, None, a.driver_model)
    except (OSError, ValueError) as e:
        print(f"couldn't install the driver agent: {e}")
        return 1
    print("\n".join(["", *driver.installed_lines(wrote, list(driver.HOSTS), a.driver_model)]))
    return 0


def install(a) -> int:
    """`tapwright install --client …`: register the MCP server with an agent and install the skill."""
    from . import connect
    apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
    try:
        lines = connect.install(a.client, apps, a.dedicated, a.project, policy=a.policy, no_policy=a.no_policy,
                                ux_rules=a.ux_rules, driver_model=a.driver_model, phone=a.phone)
    except connect.ConnectError as e:
        print(f"tapwright install: {e}")
        return 1
    print("\n".join(lines))
    return 0



START_CODES = ("bad_params", "not_connected", "reconnecting", "not_allowed", "unsupported", "internal")
PHONE_MODE = "\x00phone-mode"  # what `serve --phone` with no value gives: the mode, not an address


def serve_phone(given: str | None, address: str | None) -> tuple[bool, str | None]:
    """(phone mode, the address to dial) from `serve --phone [ADDRESS]` and `--address ADDRESS`. `--phone` alone is the
    1.7 mode (§24.4); `--phone ADDRESS` is the dial address of earlier versions, kept working (it says so)."""
    if given is None:
        return False, address
    if given == PHONE_MODE:
        return True, address
    if address is not None:
        raise SystemExit("tapwright serve: --phone ADDRESS and --address both name an address; use --address alone")
    print(f"tapwright serve: --phone {given} is the address of earlier versions; it now means --address {given}. "
          "(--phone alone is the 1.7 mode for a phone dedicated to automation.)", file=sys.stderr)
    return False, given


def report(a) -> int:
    """`tapwright report`: the same answer as the `session_report` tool, built from the action log files."""
    from . import actionlog, report as reporting
    entries = reporting.read_entries(actionlog.default_folder())
    if not entries:
        print("tapwright report: the action log has no calls yet (it is off with serve --no-log).")
        return 1
    client = a.client or reporting.last_client(entries)
    try:
        since = reporting.parse_since(a.since, round(time.time() * 1000)) if a.since else reporting.client_start(entries, client)
    except ValueError as e:
        print(f"tapwright report: {e}")
        return 2
    print(reporting.build(entries, client, since))
    return 0


def replay(a) -> int:
    """`tapwright replay FILE…`: each script through the daemon's `replay` tool, its answer printed. Exit 0 when every
    script held, 1 when one stopped at a step, 2 when one couldn't start (no phone, no app, a bad file)."""
    from . import batch, client, scriptrepair
    from .control import Refused
    apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
    whole = getattr(a, "phone", False)
    if sum((bool(apps), a.dedicated, whole)) > 1:
        print("tapwright replay: --apps, --dedicated and --phone can't be used together.")
        return 2
    repair, adopt = getattr(a, "repair", None), bool(getattr(a, "adopt", False))
    if adopt and repair != "apply":  # adopting is a person's choice, and acts only on a run that passed (AGENT-TOOLS §26.5)
        print("tapwright replay: --adopt goes with --repair apply: it adopts what that same run repaired and checked")
        return 2
    if repair and getattr(a, "update_references", False):
        print("tapwright replay: --repair can't be used with --update-references")
        return 2
    results: list[tuple[str, str, str, float]] = []  # (file, "ok" | "failed" | "error", message, seconds)
    for f in a.files:
        path = str(Path(f).resolve()) if Path(f).exists() else str(batch.named(f))  # a file, else a saved name
        began = time.monotonic()
        try:
            loaded = batch.load_script(path)  # a file that can't be read stops here, before the phone is asked
        except Refused as e:
            print(f"{f}: Error {e.code}: {e}")
            results.append((f, "error", str(e), 0.0))
            continue
        if adopt and (why := scriptrepair.why_not_fresh(loaded)) is not None:
            # (a repair is adopted only after its file replayed green from the script's start: this one has no such start)
            message = "--adopt can't be used: " + scriptrepair.apply_needs_fresh(why)
            print(f"{f}: Error bad_params: {message}")
            results.append((f, "error", message, 0.0))
            continue
        if whole:
            client.set_scope(apps, a.dedicated, True)
        else:
            client.set_scope(apps, a.dedicated)
        try:
            # (a failed picture check writes its diff next to the script, and its message ends `(diff: <path>)`)
            answer = client.call("replay", {"path": path, **({"update_references": True} if getattr(a, "update_references", False) else {}),
                                            **({"repair": repair} if repair else {})},
                                 "cli-replay", timeout=scriptrepair.APPLY_TIMEOUT if repair == "apply" else 200.0)
        except client.DaemonError as e:
            print(f"{f}: Error {e.code}: {e}")
            kind = "error" if e.code in START_CODES and not str(e).startswith("Ran ") else "failed"
            message = f"{e.code}: {e}"
            if str(e).startswith("Ran ") and any(ln.startswith("Note: " + diagnostics.CRASHED) for ln in str(e).splitlines()):
                # the app crashed: its report is pulled next to the script and named (AGENT-TOOLS §15.4)
                if saved := pull_crash_beside(path):
                    message += "\n" + diagnostics.FAILURE_CRASH_FILE.format(path=saved)
                    print(f"{f}: {diagnostics.FAILURE_CRASH_FILE.format(path=saved)}")
            results.append((f, kind, message, time.monotonic() - began))
            continue
        print(f"{f}: {answer.get('text', '')}")
        if adopt:
            adopted, said = adopt_repair(path, answer.get('text', ''))
            print(f"{f}: {said}")
            if adopted is False:  # (a checked repair that couldn't be adopted fails the command; a run with no repair doesn't)
                results.append((f, "error", f"--adopt: {said}", time.monotonic() - began))
                continue
        results.append((f, "ok", "", time.monotonic() - began))
    if a.junit:
        write_junit(a.junit, results)
    kinds = {r[1] for r in results}
    return 2 if "error" in kinds else 1 if "failed" in kinds else 0


def adopt_repair(path: str, answer: str) -> tuple[bool | None, str]:
    """`--repair apply --adopt`, after the run passed: the script is replaced by the repaired one it just wrote, the original kept
    as `<script>.orig.json` once (AGENT-TOOLS §26.5). Done here, by the person's command, and never by the daemon: no tool call
    can adopt. (adopted?, the line for the person): None when the run made no repair, so there is nothing to adopt; False when
    a checked repair couldn't be adopted (the command then fails, CodeRabbit PR #20). Only the bytes the run checked are
    adopted: the `Checked:` line names their digest."""
    from . import auditlog, batch, scriptrepair
    script = Path(path)
    if f"Wrote {script.stem}.repaired.json" not in answer:
        return None, "nothing to adopt: that run made no repair, so it wrote no repaired script"
    if not any(line.startswith(scriptrepair.CHECKED) for line in answer.splitlines()):
        # (§26.4, Tapline's lesson: only a repaired file that replayed green from the script's start is adopted)
        return False, (f"nothing was adopted: that run didn't say {script.stem}.repaired.json replayed green from the script's "
                       "fresh start, so it isn't adopted")
    expected = scriptrepair.checked_digest(answer)
    if expected is None:
        return False, (f"nothing was adopted: that run didn't name the bytes of {script.stem}.repaired.json it checked, so "
                       "they can't be told from another run's")
    try:
        data = batch.load_script(path)
    except Exception as e:  # noqa: BLE001 - a script that can't be read again isn't adopted over
        return False, f"nothing was adopted: {e}"
    name, app = str(data.get("name") or script.stem), data.get("app") if isinstance(data.get("app"), str) else None
    log = auditlog.AuditLog()
    return scriptrepair.adopt(script, lambda detail: log.write("repair", name=name, app=app, detail=detail), expected)


EXPLORE_WAIT = 600.0  # seconds `explore` waits for a phone in use before it starts (AGENT-TOOLS §22.3, §25.2)
EXPLORE_RETRY = 15.0


def explore(a, sleep=time.sleep, clock=time.monotonic) -> int:
    """`tapwright explore`: the same search as the `explore` tool (AGENT-TOOLS §25.2), from the command line, with the same
    report. Exit 0: it ended and found nothing. 1: it ended with findings, a crash included. 2: it couldn't start (no phone,
    no app, a bad flag), or a person or a pause stopped it before the end (the report is still written). When the phone is
    busy with another agent's call before the run starts it waits up to 10 minutes, then says `skipped: the phone was in use`
    and exits 2; a person on the phone at the start ends the run `busy` at once (exit 2, review R180 L10)."""
    import re
    from . import client, exploretool, policy as policies
    apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
    whole = getattr(a, "phone", False)
    if sum((bool(apps), a.dedicated, whole)) > 1:
        print("tapwright explore: --apps, --dedicated and --phone can't be used together.")
        return 2
    if len(apps) > 1:  # (1.7.5: the phone is the agent's whole; --apps names the ONE app explore searches, §25.1)
        print("tapwright explore: --apps names the one app to search; give one app, or none for the app in front")
        return 2
    try:
        rules = policies.load(a.policy) if getattr(a, "policy", None) else None
    except (OSError, ValueError) as e:
        print(f"tapwright explore: the policy file can't be used: {e}")
        return 2
    args: dict = {k: v for k, v in (("minutes", a.minutes), ("steps", a.steps), ("depth", a.depth), ("from", a.start),
                                    ("focus", a.focus), ("fill", a.fill)) if v is not None}
    if a.keep_going:
        args["continue"] = True
    client.set_scope(apps, a.dedicated, True) if whole else client.set_scope(apps, a.dedicated)
    client.set_policy(rules)
    client.set_maps(getattr(a, "maps", None))
    client.set_accounts(getattr(a, "accounts", None))
    deadline = clock() + EXPLORE_WAIT  # (the calls' own time counts too, not only the sleeps: CodeRabbit, PR #20)
    while True:
        try:
            answer = client.call("explore", args, "cli-explore", timeout=exploretool.timeout_for(args))
            break
        except client.DaemonError as e:
            left = deadline - clock()
            if e.code == "busy" and "Done: " not in str(e) and left > 0:  # the phone was in use before it began
                sleep(min(EXPLORE_RETRY, left))
                continue
            if e.code == "busy" and "Done: " not in str(e):
                print("skipped: the phone was in use")
                return 2
            print(f"Error {e.code}: {e}")
            return 2
    text = answer.get("text", "")
    print(text)
    found = re.search(r"(\d+) findings?\.", text.splitlines()[0] if text else "")
    return 1 if found and int(found.group(1)) > 0 else 0


def _daemon_alive() -> bool:
    """Is a `tapwright serve` running now (it answers with its token)? Never starts one."""
    import json as jsonlib
    from . import client, store as storelib
    try:
        info = jsonlib.loads((storelib.HOME / "serve.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(info, dict) and client.healthy(info) is not None


BUG_VALUES_VAULT = "the vault values of --accounts only (no daemon is running: not the session's typed values or codes)"
BUG_VALUES_NONE = "no values (no daemon is running and no --accounts was given)"
BUG_ISSUE_NEEDS_VALUES = ("Error not_allowed: no daemon is running, so the values a bug must never hold (the session's typed "
                          "values, the codes) can't be checked, and no --accounts names the vault's. Start tapwright serve, or "
                          "give --accounts FILE; nothing was sent.")


def bug_cmd(a, out=print, ask=None, call=None, alive=None) -> int:
    """`tapwright bug` (AGENT-TOOLS §29.3):
    - `bug <report folder> <finding id> [--with-picture] [--verify N]` makes a bug of an explore report's finding with no phone;
      with `--verify` the daemon then checks it on the phone (§29.4);
    - `bug list [--app ID]` lists the bugs kept;
    - `bug verify <bug id> [--verify N]` checks a kept bug again, on the phone (through the daemon, as the tool does);
    - `bug export <bug id> --to zip|issue [--repo owner/name] [--public] [--with-picture] [--yes] [--app ID]` (§29.7), with no
      phone: GitHub asks `Send to GitHub? [y/N]`, and without a terminal `--yes` answers it.
    Exit 0: a bug was made or updated and it reproduced or wasn't checked (or the export was written); 1: it was made and
    didn't reproduce (`0 of N`); 2: it couldn't be made or sent, or a person, a pause or a refusal stopped it. Every file is
    grepped before it is written or sent (§29.6). While a daemon runs, the make and the export go through it (review R185
    M3), so its live values hold (the vault's, the codes, the session's typed values); without one the redactor holds only
    the values of `--accounts`, the answer says so, and an export to GitHub is refused without `--accounts`. `alive`: is a
    daemon running (tests)."""
    from . import accounts as accountlib, bugmake, bugstore, mapkeeper, store as storelib
    words = list(a.what)
    bugs = bugstore.BugStore(storelib.HOME / "bugs")
    if words[:1] == ["list"]:
        if len(words) != 1:
            out("tapwright bug list takes no other word; --app ID narrows it to one app")
            return 2
        lines = bugs.listing(a.app)
        out("\n".join(lines) if lines else "No bugs are kept.")
        return 0
    verify = getattr(a, "verify", None)
    if verify is not None and not 1 <= verify <= 10:
        out("Error bad_params: --verify is how many runs check it: 1 to 10")
        return 2
    if words[:1] == ["verify"]:
        if len(words) != 2:
            out("usage: tapwright bug verify <bug id> [--verify N]")
            return 2
        return _bug_call({"bug": words[1], "verify": verify or 3}, out, call, app=getattr(a, "app", None))
    loaded = accountlib.load(a.accounts) if a.accounts else accountlib.NONE
    if loaded.problem is not None:
        out(f"tapwright bug: {loaded.problem}")
        return 2
    keep = accountlib.Store()
    keep.remember(loaded)
    running = (alive or _daemon_alive)()
    if running and a.accounts:
        # (PR #21) the daemon makes and exports with its own vault; a file given here would be read by nobody, and its
        # values would go unchecked: refused, never ignored
        out(BUG_ACCOUNTS_WITH_DAEMON)
        return 2
    if words[:1] == ["export"]:
        return _bug_export_cmd(a, words, bugs, keep, out, ask, call, running)
    if len(words) != 2:
        out("usage: tapwright bug <report folder> <finding id> [--with-picture] [--verify N] | bug list [--app ID] | "
            "bug verify <bug id> | bug export <bug id> --to zip|issue")
        return 2
    if running:  # (the daemon makes it, with its live values: review R185 M3; it checks it too when asked)
        given = Path(words[0]).expanduser()
        made_args: dict = {"finding": words[1], "run": str(given if given.is_absolute() else Path.cwd() / given),
                           "verify": verify or False}
        if a.with_picture:
            made_args["with_picture"] = True
        return _bug_call(made_args, out, call)
    maps = mapkeeper.Maps(getattr(a, "maps", None))
    ctx = bugmake.Context(bugs, keep.bug_scrub, keep.bug_gate, lambda app: _map_or_none(maps, app))
    home = storelib.HOME / "explore"
    folder = Path(words[0]).expanduser()
    try:
        got = bugmake.read_run(home, folder if folder.is_absolute() else Path.cwd() / folder, words[1])
        made = bugmake.from_run(ctx, got, with_picture=a.with_picture,
                                reproduced={"checked": False, "why": "the check didn't finish" if verify else BUG_NOT_ASKED})
    except bugmake.BugError as e:
        out(f"Error {e.code}: {ctx.safe()(str(e))}")
        return 2
    except bugstore.BugGateError as e:
        out(f"Error not_allowed: the bug was not made: {e} Nothing was written.")
        return 2
    except (ValueError, OSError) as e:
        out(f"Error bad_params: {ctx.safe()(str(e))}")
        return 2
    if verify:  # the bug is made; the phone checks it (the daemon finds it by its id and its app)
        return _bug_call({"bug": made.meta["id"], "verify": verify}, out, call, app=made.finding["app"])
    out(bugmake.answer(made, ctx.scrub, home=Path.home()) + f"\nChecked for: {BUG_VALUES_VAULT if a.accounts else BUG_VALUES_NONE}.")
    return 0


BUG_NOT_ASKED = "not asked: tapwright bug verify <bug id> checks it on the phone"
BUG_ACCOUNTS_WITH_DAEMON = ("Error bad_params: --accounts is for when no daemon is running. tapwright serve is running, and "
                            "it makes and exports bugs with its own vault (the one it was started with: tapwright serve "
                            "--accounts FILE). Leave --accounts out, or start the daemon with that file; nothing was made "
                            "or sent.")


def _bug_call(args: dict, out, call=None, app: str | None = None) -> int:
    """A `bug_report` call to the daemon (the phone's side: the reproduction check), with the exit code of §29.3. `app`: the
    app whose bug `args["bug"]` is, sent as the call's app (PR #21: the daemon then takes that app's bug when two apps hold
    the id, never the one in front)."""
    from . import bugcheck, client
    call = call or client.call
    before = client.SCOPE.get("X-Tapwright-Apps")
    if app:
        client.SCOPE["X-Tapwright-Apps"] = app
    try:
        answer = call("bug_report", args, "cli-bug", timeout=bugcheck.timeout_for(args))
    except client.DaemonError as e:
        out(f"Error {e.code}: {e}")
        return 2
    finally:
        if app:
            if before is None:
                client.SCOPE.pop("X-Tapwright-Apps", None)
            else:
                client.SCOPE["X-Tapwright-Apps"] = before
    text = answer.get("text", "")
    out(text)
    if args.get("export") is not None and not args.get("verify"):
        return 0  # (PR #21) an export that answered was written: the saved check's result is no exit of it
    return bug_exit(text)


def bug_exit(text: str) -> int:
    """The exit code of an answer (§29.3): 1 when it didn't reproduce (`0 of N`), 2 when a person, a pause or a refusal
    stopped the check, else 0. A stop of this check comes before the page's saved result (PR #21: a check that paused
    keeps the last check's `0 of 3` on its page, and is still a stop)."""
    lines = text.splitlines()
    line = lines[1] if len(lines) > 1 else ""
    stopped = ("stopped (", "needs a person")
    if any(ln.startswith("Note: not checked this time (") and any(w in ln for w in stopped) for ln in lines):
        return 2
    if line.startswith("Reproduced: 0 of "):
        return 1
    if line.startswith("Reproduced: not checked (") and any(w in line for w in stopped):
        return 2
    return 0


def _bug_export_cmd(a, words: list[str], bugs, keep, out, ask=None, call=None, daemon: bool = False) -> int:
    """`tapwright bug export <bug id> --to zip|issue ...` (§29.7), no phone. `daemon`: one is running (`call` reaches it); the export goes
    through it when `daemon` (its live values, its audit line: review R185 M3). Without one it is made here with the values of
    `--accounts`, and an export to GitHub needs `--accounts`. A bug of an app that isn't one of the daemon's test apps needs
    a yes for any export (H2)."""
    import sys
    from . import auditlog, bugexport
    from .control import Refused
    if len(words) != 2 or getattr(a, "to", None) is None:
        out("usage: tapwright bug export <bug id> --to zip | --to issue --repo owner/name [--public] [--with-picture] [--yes]")
        return 2
    bug_id = words[1]
    holders = bugs.holders(bug_id)
    app = getattr(a, "app", None)
    if app is not None and app not in holders:
        holders = []
    elif app is not None:
        holders = [app]
    if not holders:
        out(f'Error not_found: no bug "{bug_id}" is kept{f" for {app}" if app else ""}; tapwright bug list names them.')
        return 2
    if len(holders) > 1:
        out(f"Error ambiguous: {bug_id} is a bug of {', '.join(holders)}: name the app with --app.")
        return 2
    held = bugs.read(holders[0], bug_id)
    if held is None:
        out(f'Error not_found: no bug "{bug_id}" is kept.')
        return 2
    raw: dict = {"to": a.to}
    other_app = held.meta.get("test_app") is not True
    if a.to == "issue" or other_app:
        if a.to == "issue":
            raw.update(repo=getattr(a, "repo", None), public=bool(getattr(a, "public", False)))
        confirmed = bool(getattr(a, "yes", False))
        if not confirmed and ask is None and sys.stdin is not None and sys.stdin.isatty():
            ask = input
        if not confirmed and ask is not None:
            question = "Send to GitHub? [y/N] " if a.to == "issue" else (
                "This bug's app isn't one of the daemon's test apps. Write the zip anyway? [y/N] ")
            try:
                confirmed = ask(question).strip().lower() in ("y", "yes")
            except EOFError:
                confirmed = False
        raw["confirm"] = confirmed
    if a.with_picture:
        raw["with_picture"] = True
    if daemon:
        try:
            bugexport.check(raw)
        except Refused as e:
            out(f"Error {e.code}: {e}")
            return 2
        return _bug_call({"bug": bug_id, "export": raw}, out, call, app=held.app)
    log = auditlog.AuditLog(scrub=keep.scrub)
    if a.to == "issue" and not a.accounts:  # (every export, a refused one too, has its audit line: §29.7)
        log.write("export", name="bug_report", app=held.app,
                  detail=f"issue {getattr(a, 'repo', None)}: {bug_id}, refused (no values to check)")
        out(BUG_ISSUE_NEEDS_VALUES)
        return 2
    checked = BUG_VALUES_VAULT if a.accounts else BUG_VALUES_NONE
    try:
        spec = bugexport.check(raw)
        said = bugexport.export(held, spec, keep.bug_gate, lambda detail: log.write(
            "export", name="bug_report", app=held.app, detail=detail), Path.home(), checked)
    except Refused as e:
        text = str(e)
        if "needs_confirmation" in text:
            text += " On the command line: answer y, or give --yes."
        out(f"Error {e.code}: {text}")
        return 2
    out(said)
    return 0


def _map_or_none(maps, app: str) -> dict | None:
    try:
        return maps.data(None, app)
    except Exception:  # noqa: BLE001 - no map: no names and no keys from it
        return None


def pull_crash_beside(script: str) -> str | None:
    """The app's newest crash report (the daemon's `crash` with pull), copied to
    `<script dir>/<script name>.diff/<script name>.crash.ips`; its path, or None when there is none to copy."""
    import re
    import shutil
    from . import client
    try:
        text = client.call("crash", {"pull": True}, "cli-replay", timeout=60.0).get("text", "")
        saved = re.search(r"^Saved: (.+)$", text, re.M)
        if saved is None:
            return None
        path = Path(script)
        target = path.parent / f"{path.stem}.diff" / f"{path.stem}.crash.ips"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(saved.group(1), target)
        return str(target)
    except (client.DaemonError, OSError):
        return None


def write_junit(out: str, results: list[tuple[str, str, str, float]]) -> None:
    """One <testcase> per script; a stop is a <failure>, a script that couldn't start an <error>."""
    from xml.etree import ElementTree as ET
    suite = ET.Element("testsuite", name="tapwright replay", tests=str(len(results)),
                       failures=str(sum(r[1] == "failed" for r in results)),
                       errors=str(sum(r[1] == "error" for r in results)),
                       time=f"{sum(r[3] for r in results):.2f}")
    for f, kind, message, seconds in results:
        case = ET.SubElement(suite, "testcase", classname="tapwright.replay", name=Path(f).stem, file=f,
                             time=f"{seconds:.2f}")
        if kind != "ok":
            lines = message.splitlines()
            at = next((n for n, line in enumerate(lines) if line.startswith("Repair (suggest")), None)
            # `--repair suggest --junit` puts the suggestion into the failure message (AGENT-TOOLS §26.5)
            text = " ".join([lines[0], *lines[at:]]) if at is not None else lines[0]
            node = ET.SubElement(case, "failure" if kind == "failed" else "error", message=text)
            node.text = message
            if output := diagnostics.junit_output(message):  # the app's log lines and crash, for whoever reads the report
                ET.SubElement(case, "system-out").text = output
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(out, encoding="utf-8", xml_declaration=True)


_POLICY_HINT = (". Since 1.8.0 the --policy file is read at start (its words reach explore's skip floor): fix or restore it, "
                "or run `tapwright install` again without --policy (or with another file) to rewrite the host's config")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "call":  # an installed `tapwright` script that predates __main__.main
        from .thin import main as call
        return call(sys.argv[2:])
    # pymobiledevice3's test-runner channel logs every message it drops while the runner shuts down as a warning
    # ("received connection termination: <DTXMessage …>", "No channel registered …"): expected, and only noise on a
    # new user's console (seen in a clean install's `tapwright smoke`). Its errors still show.
    logging.getLogger("pymobiledevice3.dtx").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(prog="tapwright", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--udid")
    p.add_argument("--asc-key-id")
    p.add_argument("--asc-issuer-id")
    p.add_argument("--asc-key-file")
    p.add_argument("--bundle-prefix")
    p.add_argument("--p12")
    p.add_argument("--p12-password")
    p.add_argument("--profile")
    p.add_argument("--force", action="store_true",
                   help="do every step again, even those already done (WebDriverAgent, network pairing)")
    p = sub.add_parser("doctor")
    p.add_argument("--unattended", action="store_true",
                   help="also list what would stop a run with nobody at the phone, and how to fix each: the phone locked "
                        "with no passcode given, Enable UI Automation, the computer not trusted, Developer Mode off, a "
                        "battery under 20%%")
    p.add_argument("--accounts", metavar="FILE",
                   help="also check this test accounts file: that it reads, that it isn't inside a git repo that "
                        "doesn't ignore it (then no account can be used), and, with --unattended, whether it gives "
                        "the phone's passcode (the value is never read out)")
    p.add_argument("--fix", action="store_true",
                   help="repair what it can with the phone on USB: mount the developer disk image, install or "
                        "re-sign WebDriverAgent with the stored profile, refresh the network pairing")
    p.add_argument("--policy", metavar="FILE",
                   help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    p = sub.add_parser("grants", help="the grants of a policy file, their counts and last uses; --revoke-all; unlock")
    p.add_argument("action", nargs="?", choices=["unlock"],
                   help="unlock: remove a stuck policy edit lock (refused while its holder is alive)")
    p.add_argument("--policy", metavar="FILE", help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    p.add_argument("--revoke-all", action="store_true",
                   help="revoke every grant in the file (it stays there, marked revoked) and stop any running route "
                        "between moves")
    p = sub.add_parser("schedule", help="routes that run on a time or a trigger, with no model (add, list, remove, unlock)")
    ss = p.add_subparsers(dest="schedule_cmd", required=True)
    q = ss.add_parser("add", help="schedule a saved route")
    q.add_argument("route")
    q.add_argument("--at", metavar="WHEN", help='"09:00 daily", "09:00 weekdays" or an ISO date-time (one run)')
    q.add_argument("--on-file", "--trigger-file", dest="on_file", metavar="DIR",
                   help="run when a file appears in this folder (checked while the PC is awake)")
    q.add_argument("--on-notification", metavar="APP", help="run when a notification from this app arrives (Tapwright only; "
                   "checked every 2 minutes while the PC is awake, never while the phone is in use)")
    q.add_argument("--with", dest="with_", action="append", metavar="NAME=VALUE|NAME=@FILE",
                   help="a value for one of the route's variables; @FILE is the text of that file, read at each run")
    q.add_argument("--name", help="the schedule's name (default: the route's)")
    q.add_argument("--policy", metavar="FILE", help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    q.add_argument("--accounts", metavar="FILE", help="the vault the run uses")
    q.add_argument("--maps", metavar="DIR", help="where the route's map is kept (default: ~/.tapwright/maps)")
    ss.add_parser("list", help="the schedules and their tasks")
    q = ss.add_parser("remove", help="remove a schedule and its task")
    q.add_argument("name")
    q = ss.add_parser("unlock", help="remove a stuck run lock, or with --phone the phone lock (refused while its holder is "
                                     "alive)")
    q.add_argument("name", nargs="?")
    q.add_argument("--phone", action="store_true", help="Remove the stale schedule phone lock; does not unlock the iPhone.")
    p = sub.add_parser("run-schedule", help="one run of a schedule, now (what the system's scheduler starts)")
    p.add_argument("name")
    p.add_argument("--if-triggered", action="store_true",
                   help="run only if a file appeared in the schedule's trigger folder since the last check")
    p = sub.add_parser("audit-log", help="the audit log (~/.tapwright/audit): what was allowed, used and sent, one line "
                                         "each, never a value; for you, not for an agent (§23)")
    q = p.add_mutually_exclusive_group()
    q.add_argument("--since", metavar="DATE|TIME", help="only the lines since then: 2026-11-14 or 2026-11-14T09:30")
    q.add_argument("--delete", action="store_true", help="delete the audit log")
    sub.add_parser("unlock-reset", help="after a refused passcode, let the passcode unlock try the vault's "
                                        "phone.passcode again (check it first); the limits of 2 an hour and 3 a day stay")
    p = sub.add_parser("vault", help="the vault file (--accounts): protect a plain one with this system's protection")
    p.add_argument("action", choices=["protect"],
                   help="protect: write FILE in this system's protected form (Windows DPAPI, the macOS keychain); "
                        "Tapline reads it too. The plain file stays until the protected copy reads back the same")
    p.add_argument("file", metavar="FILE")
    p = sub.add_parser("smoke")
    p.add_argument("--app", required=True, help="bundle id, e.g. com.example.app")
    p.add_argument("--wait", type=float, default=3.0, help="seconds to let the app start (default 3)")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--host")
    p = sub.add_parser("probe")
    p.add_argument("--host", help="an address of the phone to try, e.g. its Tailscale IP")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, help="default 8417; a free port if that one is taken")
    p.add_argument("--address", help="reach the phone at this address (e.g. its Tailscale IP) instead of USB or Wi-Fi")
    p.add_argument("--keep-alive", action="store_true", help="don't stop after 30 minutes without use")
    p.add_argument("--new-token", action="store_true", help="replace the live view's access token")
    p.add_argument("--stop", action="store_true", help="stop the running daemon")
    p.add_argument("--no-log", action="store_true",
                   help="don't keep the action log (~/.tapwright/log: one line per action, typed text never written)")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--apps", metavar="APP[,APP]",
                       help='Deprecated compatibility metadata; all apps and system screens are accessible.')
    scope.add_argument("--dedicated", action="store_true",
                       help='Deprecated compatibility flag; whole-phone control is the default.')
    p.add_argument("--phone", nargs="?", const=PHONE_MODE, default=None, metavar="",
                       help='Enable configured automatic unlock at call start; whole-phone access already works without this flag.')
    p.add_argument("--policy", metavar="FILE",
                   help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    p.add_argument("--no-dialog", action="store_true",
                   help="no person's OK in the host's dialog (for 1.6; kept as the daemon's setting)")
    p.add_argument("--accounts", metavar="FILE",
                   help="the test accounts file (JSON: named accounts with a username and password, and the phone's "
                        "passcode), kept outside any git repo or ignored by it. An agent names one with drive's "
                        "account; the values are typed, never shown. A call can't name another file")
    p.add_argument("--brief", action="store_true",
                   help="answer every action in brief unless the call says brief: false (the change block cut to 12 lines)")
    p.add_argument("--no-hints", action="store_true",
                   help="answers without the Known: and Hint: lines and instructions without the token rules: as in 1.8.5")
    p.add_argument("--maps", metavar="DIR",
                   help="where the per-app maps (<DIR>/<bundle id>/map.json and routes/) are kept, instead of "
                        "~/.tapwright/maps: keep them with a project so a team, CI and Tapline share one folder. A call "
                        "can't move it")
    p.add_argument("--settle-on-video", action="store_true",
                   help="experimental, with --video: actions watch the video's frames and the JPEG stream stays "
                        "closed (faster over Wi-Fi; a stalled mirror can make an action answer 'No visible change')")
    p.add_argument("--video", action="store_true",
                   help="show the live view as video: start the phone's Screen Mirroring to this computer (same "
                        "Wi-Fi; needs UxPlay and `pip install tapwright[video]`)")
    sub.add_parser("view")
    sub.add_parser("call", add_help=False,
                   help="run one tool in the running daemon and print its answer (standard library only: "
                        "tapwright call --help)")
    p = sub.add_parser("record-video", help="record the live view as an MP4 in real time, with each frame's time "
                                            "(needs tapwright[video]; it shows whatever is on the phone's screen)")
    p.add_argument("--out", required=True, metavar="FILE.mp4", help="the video to write (it must not exist yet); "
                   "FILE.frames.json is written next to it")
    p.add_argument("--seconds", type=float, metavar="N", help="stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--fps", type=int, metavar="N",
                   help="write at most N frames a second (default: every frame the live view sends, about 15 while "
                        "the screen moves). The video keeps real time either way")
    p = sub.add_parser("report",help="where an agent's calls went, from the action log (no phone needed)")
    p.add_argument("--since", metavar="TIME",
                   help="epoch ms, an ISO time or how long ago (30m, 2h, 1d); default: the start of the agent's session")
    p.add_argument("--client", metavar="ID", help="one agent's client id; default: the agent that called last")
    p = sub.add_parser("replay", help="run scripts saved with record (or written by hand) on the phone, with no agent")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--junit", metavar="OUT", help="also write the results as JUnit XML, one test case per script")
    p.add_argument("--apps", metavar="APP[,APP]",
                   help='Deprecated compatibility metadata; all apps and system screens are accessible.')
    p.add_argument("--dedicated", action="store_true",
                   help='Deprecated compatibility flag; whole-phone control is the default.')
    p.add_argument("--phone", action="store_true",
                   help='Enable configured automatic unlock at call start; whole-phone access already works without this flag.')
    p.add_argument("--update-references", action="store_true",
                   help="picture checks (matches, compare_screen) save the screen into the script's "
                        "<name>.references folder instead of comparing; exit 0 when every step ran")
    p.add_argument("--repair", choices=("suggest", "apply"),
                   help="when a step's target moved or was renamed: suggest the fix after the failure (the script runs; no candidate is tried), or "
                        "apply it as the run goes, checked, and write <script>.repaired.json and .repair.md beside the script")
    p.add_argument("--adopt", action="store_true",
                   help="with --repair apply: when that run passed, replace the script with the repaired one, keeping the "
                        "original as <script>.orig.json (once). Only a person can do this: no tool call can")
    p = sub.add_parser("bug", help="a one-page bug report from an explore report's finding (no phone needed); bug list, "
                                   "bug verify, bug export")
    p.add_argument("what", nargs="+", metavar="WORD",
                   help="<report folder> <finding id>, or list, or verify <bug id>, or export <bug id>")
    p.add_argument("--with-picture", action="store_true",
                   help="a private picture goes into the folder, or into the export (your own request)")
    p.add_argument("--verify", type=int, metavar="N", help="reproduction runs on the phone, 1 to 10 (bug verify: default 3)")
    p.add_argument("--app", metavar="ID", help="bug list: one app's bugs only; bug export: the app whose bug it is")
    p.add_argument("--to", choices=("zip", "issue"), help="bug export: a .zip next to the bug's folder, or a GitHub issue")
    p.add_argument("--repo", metavar="OWNER/NAME", help="bug export --to issue: the repository")
    p.add_argument("--public", action="store_true", help="bug export --to issue: the repository may be public")
    p.add_argument("--yes", action="store_true", help="bug export --to issue: answer Send to GitHub? with yes (no terminal)")
    p.add_argument("--accounts", metavar="FILE", help="the vault file whose values the bug keeps out (§29.6)")
    p.add_argument("--maps", metavar="DIR", help="the maps folder (default: the tool's home)")
    p = sub.add_parser("explore", help="search an app for what breaks, with no agent: the same search and report as the explore tool")
    p.add_argument("--minutes", type=float, metavar="N", help="time budget, 1 to 60 (default 10)")
    p.add_argument("--steps", type=int, metavar="N", help="move budget, 1 to 5000 (default 500)")
    p.add_argument("--depth", type=int, metavar="N", help="how deep from the start place it expands, 1 to 20 (default 8)")
    p.add_argument("--from", dest="start", metavar="PLACE", help="a place's name to start at")
    p.add_argument("--focus", metavar="PLACE", help="expand only places below this one")
    p.add_argument("--fill", metavar="MODE", help='"test-data" (a fake value into fields whose id ends -test-safe) or '
                                                  '"fixture:<name>.<field>"; default: type nothing')
    p.add_argument("--continue", dest="keep_going", action="store_true", help="go on after a crash")
    p.add_argument("--apps", metavar="APP", help="the one app to search, as a bundle id or exact name (default: the app in front)")
    p.add_argument("--dedicated", action="store_true", help="the phone is kept for testing")
    p.add_argument("--phone", action="store_true", help="the phone is dedicated to automation (1.7)")
    p.add_argument("--policy", metavar="FILE", help="the safety policy file (its confirm and block words are part of the skip floor)")
    p.add_argument("--accounts", metavar="FILE", help="the vault file")
    p.add_argument("--maps", metavar="DIR", help="where the app's map is kept")
    p = sub.add_parser("skill", help="the agent skill: how to test with Tapwright, for Claude Code, Codex and others")
    p.add_argument("action", choices=("install", "path"),
                   help="install: copy it to .claude/skills and .agents/skills; path: print where it ships")
    p.add_argument("--project", metavar="DIR", help="install into this project's folder instead of your home folder")
    p.add_argument("--driver-model", metavar="NAME", action="append", dest="driver_model",
                   help="the model of the driver agent written next to the skill: a name the host knows, for both "
                        "hosts, or host=NAME (claude=haiku, codex=gpt-6.1-terra) for one. Default: each host's "
                        "fastest small model")
    p = sub.add_parser("starters", help="starter maps and routes for the phone's own apps (Reminders, Notes, ...)")
    p.add_argument("action", choices=("install",),
                   help="install: add them to your maps, never over a route or place of yours")
    p.add_argument("--maps", metavar="DIR",
                   help="where the per-app maps are kept (default: ~/.tapwright/maps), as for tapwright mcp")
    p.add_argument("--app", metavar="ID", action="append",
                   help="only this app's starters (a bundle id; repeat for several)")
    p = sub.add_parser("install", help="connect an agent: register the MCP server with it and install the skill")
    p.add_argument("--client", required=True, choices=("claude", "codex", "cursor"),
                   help="the agent to connect: Claude Code, Codex or Cursor")
    p.add_argument("--apps", metavar="APP[,APP]",
                   help='Deprecated compatibility metadata; all apps and system screens are accessible.')
    p.add_argument("--dedicated", action="store_true",
                   help='Deprecated compatibility flag; whole-phone control is the default.')
    p.add_argument("--phone", action="store_true",
                   help='Enable configured automatic unlock at call start; whole-phone access already works without this flag.')
    p.add_argument("--project", metavar="DIR",
                   help="for this project only: its .mcp.json (Claude Code) or .cursor/mcp.json (Cursor), and its skill folders")
    rules = p.add_mutually_exclusive_group()
    rules.add_argument("--policy", metavar="FILE",
                       help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    rules.add_argument("--no-policy", action="store_true", help='Deprecated compatibility flag; actions no longer use label-based policies.')
    p.add_argument("--ux-rules", metavar="FILE",
                   help="the house rules file to register with the server (see tapwright mcp --ux-rules); a rerun "
                        "keeps the entry's earlier one unless this replaces it")
    p.add_argument("--driver-model", metavar="NAME", action="append", dest="driver_model",
                   help="the model of the driver agent (a subagent for ground the map doesn't know; Claude Code and "
                        "Codex): a name the host knows. Default: that host's fastest small model (haiku, gpt-6.1-terra)")
    p = sub.add_parser("mcp")
    p.add_argument("--screenshots", metavar="DIR",
                   help="save screenshots in DIR (the newest 50) and answer with their path, instead of sending images")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--apps", metavar="APP[,APP]",
                       help='Deprecated compatibility metadata; all apps and system screens are accessible.')
    scope.add_argument("--dedicated", action="store_true",
                       help='Deprecated compatibility flag; whole-phone control is the default.')
    scope.add_argument("--phone", action="store_true",
                       help='Enable configured automatic unlock at call start; whole-phone access already works without this flag.')
    rules = p.add_mutually_exclusive_group()
    rules.add_argument("--policy", metavar="FILE",
                       help='a policy file: its words only add to what explore and a repair skip (an agent'"'"'s own actions are never restricted by it)')
    rules.add_argument("--no-policy", action="store_true",
                       help='Deprecated compatibility flag; actions no longer use label-based policies.')
    p.add_argument("--ux-rules", metavar="FILE",
                   help="the user's house rules for ux_check and journeys, as JSON: rules written as text (the agent "
                        'judges them) or {"max_taps_to_goal": n} / {"way_back": true} (the tool checks them)')
    p.add_argument("--brief", action="store_true",
                   help="answer every action in brief unless the call says brief: false (the change block cut to 12 lines)")
    p.add_argument("--no-hints", action="store_true",
                   help="answers without the Known: and Hint: lines and instructions without the token rules: as in 1.8.5")
    p.add_argument("--maps", metavar="DIR",
                   help="where the per-app maps (<DIR>/<bundle id>/map.json and routes/) are kept, instead of "
                        "~/.tapwright/maps: keep them with a project so a team, CI and Tapline share one folder")
    p.add_argument("--no-dialog", action="store_true",
                   help="never ask the host's dialog (MCP elicitation) for a person's OK or a moment that needs them; "
                        "the live view alone answers. For a host that passes elicitation requests to the model instead "
                        "of showing them to a person")
    p.add_argument("--accounts", metavar="FILE",
                   help="the test accounts file (JSON), kept outside any git repo or ignored by it: an agent names an account "
                        "with drive's account, and the tool types its values; they are never shown")
    a = ap.parse_args()
    if a.cmd == "mcp":
        from . import mcp_server, policy as policies, uxcheck
        apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
        try:
            if a.policy:
                print('Tapwright provides whole-phone control. --apps, --dedicated, --policy and --no-policy are compatibility options, not access restrictions; permission decisions belong to the agent.', file=sys.stderr)
            # (its words still reach explore's and a repair's skip floor, which they only add to; no other call reads them:
            # CodeRabbit, PR #20)
            chosen = policies.load(a.policy) if a.policy else None
            ux_rules = uxcheck.load(a.ux_rules) if a.ux_rules else None
        except ValueError as e:
            # (review R180b L6: from 1.8.0 the file is read at start, as explore's skip floor uses it; a host config written by
            # `tapwright install --policy FILE` before then may name a file since moved or broken)
            raise SystemExit(f"tapwright mcp: {e}" + _POLICY_HINT if a.policy and str(a.policy) in str(e) else
                             f"tapwright mcp: {e}") from e
        mcp_server.run(screenshots=a.screenshots, apps=apps, dedicated=a.dedicated, rules=chosen, ux_rules=ux_rules,
                       brief=a.brief, maps=a.maps, dialog=not a.no_dialog, accounts=a.accounts, phone=a.phone,
                       hints=not a.no_hints)
        return 0
    if a.cmd == "view":
        return view()
    if a.cmd == "report":
        return report(a)
    if a.cmd == "record-video":
        from . import recordvideo
        return recordvideo.main(a)
    if a.cmd == "grants":
        from . import grantscmd
        if a.action == "unlock":
            text, code = grantscmd.run_unlock(a.policy)
        else:
            text, code = grantscmd.run_grants(a.policy, revoke_all=a.revoke_all)
        print(text)
        return code
    if a.cmd == "schedule":
        from . import schedules
        if a.schedule_cmd == "add":
            text, code = schedules.add(a.route, at=a.at, on_file=a.on_file, on_notification=a.on_notification,
                                       with_=a.with_, name=a.name, policy=a.policy, accounts=a.accounts, maps=a.maps)
        elif a.schedule_cmd == "list":
            text, code = schedules.listing_text()
        elif a.schedule_cmd == "remove":
            text, code = schedules.remove(a.name)
        elif a.phone:
            text, code = schedules.unlock_phone() if a.name is None else ("Error: give a name or --phone, not both", 1)
        elif a.name is None:
            text, code = "Error: give the schedule's name, or --phone for the phone lock", 1
        else:
            text, code = schedules.unlock(a.name)
        print(text)
        return code
    if a.cmd == "run-schedule":
        from . import schedulerun
        outcome = schedulerun.run(a.name, if_triggered=a.if_triggered)
        if outcome.status != "idle":
            print(outcome.text or outcome.result)
        return outcome.code
    if a.cmd == "audit-log":
        from . import auditlog
        if a.delete:
            # a person's own decision, asked for on a terminal, and it leaves its own line behind (review 1.7 B-L12)
            if sys.stdin is not None and sys.stdin.isatty():
                typed = input("This deletes every line of the audit log. Type delete to go on: ").strip()
                if typed.casefold() != "delete":
                    print("Nothing was deleted.")
                    return 1
            gone = auditlog.delete()
            auditlog.AuditLog().write("approval", name="audit log",
                                      detail=f"audit log deleted by the person ({gone} line{'' if gone == 1 else 's'})")
            print(f"Deleted the audit log ({gone} line{'' if gone == 1 else 's'}). One line now says it was deleted.")
            return 0
        try:
            since = auditlog.since_of(a.since) if a.since else None
        except ValueError:
            print(f"Error: --since takes a date (2026-11-14) or an ISO time (2026-11-14T09:30), not {a.since!r}")
            return 2
        lines = auditlog.read_lines(since=since)
        print("\n".join(lines) if lines else "No audit lines" + (" since then." if since else "."))
        return 0
    if a.cmd == "unlock-reset":
        from . import unlock
        done, text = asyncio.run(unlock.Ledger().reset())
        print(text if done else f"Error: {text}")
        return 0 if done else 1
    if a.cmd == "vault":
        from . import accounts, vault
        done, text = vault.protect(str(Path(a.file).expanduser()), check=accounts.check_text)
        print(text if done else f"Error: {text}")
        return 0 if done else 1
    if a.cmd == "replay":
        return replay(a)
    if a.cmd == "explore":
        return explore(a)
    if a.cmd == "bug":
        return bug_cmd(a)
    if a.cmd == "skill":
        return skill(a)
    if a.cmd == "starters":
        from . import starters
        text, code = starters.install(a.maps, a.app)
        print(text)
        return code
    if a.cmd == "install":
        return install(a)
    if a.cmd == "serve" and a.stop:
        return stop_daemon()
    from .device import run
    if a.cmd == "serve":
        from . import server
        if a.no_log:
            os.environ["TAPWRIGHT_NO_LOG"] = "1"
        port = a.port if a.port is not None else server.DEFAULT_PORT
        from . import policy as policies
        from .ceiling import Ceiling
        try:
            if a.policy:
                print('Tapwright provides whole-phone control. --apps, --dedicated, --policy and --no-policy are compatibility options, not access restrictions; permission decisions belong to the agent.', file=sys.stderr)
            # (serve --policy is the floor a call's own explore or repair policy only adds to, server.py; it restricts no other
            # call: CodeRabbit, PR #20)
            chosen = policies.load(a.policy) if a.policy else None
        except ValueError as e:
            # (review R180b L6: from 1.8.0 the file is read at start, as explore's skip floor uses it; a host config written by
            # `tapwright install --policy FILE` before then may name a file since moved or broken)
            raise SystemExit(f"tapwright serve: {e}" + _POLICY_HINT if a.policy and str(a.policy) in str(e) else
                             f"tapwright serve: {e}") from e
        whole, address = serve_phone(a.phone, a.address)
        if whole and (a.apps or a.dedicated):
            raise SystemExit("tapwright serve: --phone can't be used with --apps or --dedicated")
        ceiling = Ceiling(apps=tuple(x.strip() for x in (a.apps or "").split(",") if x.strip()), dedicated=a.dedicated,
                          phone=whole, policy=chosen, no_dialog=a.no_dialog,
                          accounts=str(Path(a.accounts).expanduser().resolve()) if a.accounts else None,
                          maps=str(Path(a.maps).expanduser().resolve()) if a.maps else None, brief=a.brief,
                          no_hints=a.no_hints)
        return run(lambda: server.serve(port=port, keep_alive=a.keep_alive, new_token=a.new_token, phone=address,
                                        video=a.video, settle_on_video=a.settle_on_video, ceiling=ceiling))
    return run({"setup": setup, "doctor": doctor, "smoke": smoke, "probe": probe}[a.cmd], a)


if __name__ == "__main__":
    sys.exit(main())
