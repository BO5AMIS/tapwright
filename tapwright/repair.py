"""Keeping a set-up phone working: when the signing runs out, and what `tapwright doctor --fix` repairs.

A development profile and certificate last a year. When one runs out, WebDriverAgent stops starting
on the phone. `expiry_warning` says so early. `doctor --fix` repairs what can be repaired with the
signing material already in `~/.tapwright`: it never creates a certificate or a profile (that is
`tapwright setup`, with the API key). The decisions are plain functions of facts, so they are
tested without a phone; `cli.doctor` gathers the facts and does the work."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from . import signing, store

WARN_DAYS = 30  # doctor and the agent's first answer warn this long before the signing runs out
REINSTALL_DAYS = 14  # `doctor --fix` re-signs WebDriverAgent when its signature has less than this left

Verdict = Literal["ok", "reinstall", "setup"]


def _aware(moment: datetime) -> datetime:
    """Profiles and certificates give UTC times without a zone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Expiry:
    """When one piece of signing material runs out: `what` is 'profile "name"' or 'certificate'."""
    what: str
    when: datetime

    def days_left(self, now: datetime) -> int:
        """Whole days left; 0 or less once it has run out."""
        return (self.when - now) // timedelta(days=1)

    def date(self) -> str:
        return f"{self.when:%Y-%m-%d}"


def profile_expiry() -> Expiry | None:
    """The stored profile's expiry, or None when there is no readable profile."""
    try:
        p = signing.read_profile()
        return Expiry(f"profile '{p['Name']}'", _aware(p["ExpirationDate"]))
    except (OSError, RuntimeError, KeyError, ValueError):
        return None


def certificate_expiry() -> Expiry | None:
    """The stored development certificate's expiry, or None when there is none."""
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(signing.CERT_FILE.read_bytes())
        return Expiry("certificate", _aware(cert.not_valid_after_utc))
    except (OSError, ValueError, ImportError, AttributeError):
        return None


def expiry_lines(expiries: list[Expiry], now: datetime, within: int = WARN_DAYS) -> list[str]:
    """One sentence for each of `expiries` that is gone or goes within `within` days, soonest first."""
    lines = []
    for e in sorted(expiries, key=lambda x: x.when):
        left = e.days_left(now)
        if e.when <= now:
            lines.append(f"the {e.what} expired on {e.date()}")
        elif left < within:
            lines.append(f"the {e.what} expires on {e.date()} (in {left} day{'s' if left != 1 else ''})")
    return lines


def expiry_warning(now: datetime | None = None) -> str | None:
    """A short warning for the profile or certificate that runs out within WARN_DAYS, or None. Used by
    `tapwright doctor` and by the MCP server's first answer."""
    now = now or now_utc()
    found = [e for e in (profile_expiry(), certificate_expiry()) if e]
    lines = expiry_lines(found, now)
    if not lines:
        return None
    return "Tapwright signing: " + "; ".join(lines) + ". Renew it with `tapwright setup` (App Store Connect key, phone on USB)."


# ---------- what to do about WebDriverAgent ----------

@dataclass(frozen=True)
class WdaFacts:
    """What is known about WebDriverAgent on the phone and the signing material on this computer."""
    installed: bool  # the runner app is on the phone under the id the profile gives it
    installed_expiry: datetime | None  # the profile it was signed with, when known (setup records it)
    profile: Expiry | None  # the profile in ~/.tapwright/signing
    profile_has_phone: bool
    certificate: Expiry | None
    installed_build: str | None = None  # the runner build setup installed (its cache folder name), when recorded
    wanted_build: str | None = None  # the build this Tapwright uses (store.wda_source)


@dataclass(frozen=True)
class WdaDecision:
    verdict: Verdict
    reason: str


SETUP_AGAIN = "run `tapwright setup` again with your App Store Connect key (it makes the new profile)"


def decide_wda(f: WdaFacts, now: datetime) -> WdaDecision:
    """ok, reinstall (re-sign with what is stored and install again), or setup (new signing material is
    needed, which only `tapwright setup` creates)."""
    if f.profile is None:
        return WdaDecision("setup", "there is no signing profile; " + SETUP_AGAIN)
    if not f.profile_has_phone:
        return WdaDecision("setup", "the profile doesn't include this iPhone; " + SETUP_AGAIN)
    if f.profile.when <= now:
        return WdaDecision("setup", f"the profile expired on {f.profile.date()}; " + SETUP_AGAIN)
    if f.certificate is not None and f.certificate.when <= now:
        return WdaDecision("setup", f"the certificate expired on {f.certificate.date()}; " + SETUP_AGAIN)
    if not f.installed:
        return WdaDecision("reinstall", "WebDriverAgent isn't on the phone")
    if f.wanted_build and f.installed_build != f.wanted_build:
        was = f"build {f.installed_build}" if f.installed_build else "the build an older Tapwright installed"
        return WdaDecision("reinstall", f"the phone has {was}; this Tapwright uses {f.wanted_build} (a faster tap)")
    signed = _aware(f.installed_expiry) if f.installed_expiry else f.profile.when  # setup signs with the profile it holds
    if signed - now > timedelta(days=REINSTALL_DAYS):
        return WdaDecision("ok", f"signed until {signed:%Y-%m-%d}")
    if f.profile.when - now > timedelta(days=REINSTALL_DAYS):  # the stored profile is newer than the one on the phone
        return WdaDecision("reinstall", f"its signature ends {signed:%Y-%m-%d}; the stored profile lasts until "
                                        f"{f.profile.date()}")
    return WdaDecision("setup", f"its signature ends {signed:%Y-%m-%d} and the stored profile ends "
                                f"{f.profile.date()}; " + SETUP_AGAIN)


def record_install(profile: Expiry | None) -> None:
    """Remember which profile the WebDriverAgent on the phone was signed with, and which build it is."""
    if profile:
        store.save_state(wda_profile_expires=profile.when.isoformat(), wda_build=store.wda_source()[3])


def builds() -> dict[str, str | None]:
    """installed_build and wanted_build for WdaFacts."""
    return {"installed_build": store.load_state().get("wda_build"), "wanted_build": store.wda_source()[3]}


def recorded_install_expiry() -> datetime | None:
    raw = store.load_state().get("wda_profile_expires")
    try:
        return _aware(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None


# ---------- the repairs, in order ----------

class Phone:
    """What the repairs need from the phone. `cli` has the real one over USB; tests have a fake."""

    async def image_mounted(self) -> bool:
        raise NotImplementedError

    async def mount_image(self) -> None:
        raise NotImplementedError

    async def wda_installed(self) -> bool:
        raise NotImplementedError

    async def install_wda(self) -> None:
        raise NotImplementedError

    async def refresh_pairing_port(self) -> tuple[int | None, int | None]:
        """(the port the phone offers now, or None if not found; the port saved before)."""
        raise NotImplementedError


async def run_fixes(phone: Phone, report, now: datetime | None = None) -> int:
    """Repair what can be repaired, saying what is done. `report(kind, message)` takes 'ok', 'note' or
    'fail'. Returns the number of problems left."""
    now = now or now_utc()
    left = 0
    try:
        if await phone.image_mounted():
            report("ok", "developer disk image: mounted")
        else:
            report("note", "developer disk image: not mounted; mounting it (downloads once, needs internet)")
            await phone.mount_image()
            if await phone.image_mounted():
                report("ok", "developer disk image: mounted now")
            else:
                left += 1
                report("fail", "developer disk image: still not mounted. Is the phone unlocked, in Developer Mode, "
                               "and is this computer online?")
    except Exception as e:
        left += 1
        report("fail", f"developer disk image: {e}")
    try:
        profile = profile_expiry()
        facts = WdaFacts(installed=await phone.wda_installed(), installed_expiry=recorded_install_expiry(),
                         profile=profile, profile_has_phone=profile_includes_phone(), certificate=certificate_expiry(),
                         **builds())
        decision = decide_wda(facts, now)
        if decision.verdict == "ok":
            report("ok", f"WebDriverAgent: {decision.reason}")
        elif decision.verdict == "setup":
            left += 1
            report("fail", f"WebDriverAgent: {decision.reason}")
        else:
            report("note", f"WebDriverAgent: {decision.reason}; re-signing it with your stored profile and "
                           "installing it (about 20 s)")
            await phone.install_wda()
            report("ok", "WebDriverAgent: installed again")
    except Exception as e:
        left += 1
        report("fail", f"WebDriverAgent: {e}")
    try:
        found, saved = await phone.refresh_pairing_port()
        if found is None:
            report("note", "network pairing: the phone isn't offering it on this Wi-Fi (put the phone on the same "
                           "Wi-Fi, unlocked, and run `tapwright doctor --fix` again)"
                   + (f"; the saved port is {saved}" if saved else ""))
        elif found == saved:
            report("ok", f"network pairing: port {found} is current")
        else:
            report("ok", f"network pairing: port refreshed to {found}" + (f" (was {saved})" if saved else ""))
    except Exception as e:
        report("note", f"network pairing: couldn't refresh it ({e})")
    return left


def profile_includes_phone() -> bool:
    """Does the stored profile list the phone this computer was set up for? True when it can't tell."""
    udid = store.load_state().get("udid")
    try:
        devices = signing.read_profile().get("ProvisionedDevices", [])
    except (OSError, RuntimeError, ValueError):
        return True
    return not udid or udid in devices
