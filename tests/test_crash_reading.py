"""Reading crash reports off the phone (AGENT-TOOLS §15.2): whose they are, how much is read, and how far the search
goes. The phone's file service is a fake; the reports are real `.ips` text, parsed by pycrashreport."""
import asyncio
import datetime
import json

import pytest

from tapwright import device as device_module
from tapwright import diagnostics

APP = "com.example.app"
OTHER = "com.private.app"


def ips(name="Example", bug_type="309", bundle=None, text_identifier=None) -> bytes:
    """A report file: the header line, then a JSON body (type 309) or the old text body (type 109)."""
    header = {"app_name": name, "name": name, "bug_type": bug_type, "timestamp": "2026-11-13 12:00:00.00 +0000"}
    if bundle:
        header["bundleID"] = bundle
    if text_identifier is not None:
        body = f"Process:             {name} [42]\nIdentifier:          {text_identifier}\nTriggered by Thread:  0\n"
    elif text_identifier is None and bug_type == "109":
        body = f"Process:             {name} [42]\nTriggered by Thread:  0\n"
    else:
        body = json.dumps({"faultingThread": 0, "usedImages": [], "threads": [{"frames": []}]})
    return (json.dumps(header) + "\n" + body).encode()


class FakeAfc:
    def __init__(self, files: dict[str, bytes], sizes: dict[str, int] | None = None) -> None:
        self.files, self.sizes = files, sizes or {}
        self.opened: list[str] = []
        self.read_bytes = 0
        self.handles: dict[int, str] = {}
        self.pos: dict[int, int] = {}

    async def dirlist(self, path, depth=1):
        yield "/"
        for name in self.files:
            yield name

    async def stat(self, name):
        n = len(self.files[name])
        # newest last in insertion order: later names are newer
        order = list(self.files).index(name)
        return {"st_ifmt": "S_IFREG", "st_size": self.sizes.get(name, n),
                "st_mtime": datetime.datetime(2026, 11, 13, 12, 0, 0) + datetime.timedelta(minutes=order)}

    async def fopen(self, name, mode="r"):
        self.opened.append(name)
        handle = len(self.handles) + 1
        self.handles[handle], self.pos[handle] = name, 0
        return handle

    async def fread(self, handle, size):
        data = self.files[self.handles[handle]]
        chunk = data[self.pos[handle]:self.pos[handle] + size]
        self.pos[handle] += len(chunk)
        self.read_bytes += len(chunk)
        return chunk

    async def fclose(self, handle):
        pass

    async def get_file_contents(self, name):  # the unbounded read the old code used: must never be reached
        raise AssertionError("an unbounded read of a crash report")


@pytest.fixture
def phone(monkeypatch):
    """`phone(files, sizes)`: a Device whose crash folder is those files."""
    from pymobiledevice3.services import crash_reports

    def make(files, sizes=None):
        afc = FakeAfc(files, sizes)

        class Manager:
            def __init__(self, rsd):
                self.afc = afc

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def parse(self, path):
                raise AssertionError("the unbounded parse of a crash report")
        monkeypatch.setattr(crash_reports, "CrashReportsManager", Manager)
        dev = device_module.Device.__new__(device_module.Device)
        dev.rsd = None
        return dev, afc
    return make


def go(coro):
    return asyncio.run(coro)


def name_of(n: int) -> str:
    return f"/Example-2026-11-13-{120000 + n:06d}.ips"


def test_a_report_without_the_bundle_id_is_not_the_apps_even_when_it_names_the_executable(phone):
    files = {name_of(1): ips(bug_type="109", text_identifier=OTHER),  # legacy text body naming another app
             name_of(2): ips(bug_type="109"),  # names no app at all: nothing proves it is ours
             name_of(3): ips(bundle=OTHER),
             name_of(4): ips(bug_type="109", text_identifier=APP),  # the legacy body names the app: kept
             name_of(5): ips(bundle=APP)}
    dev, _ = phone(files)
    found = go(dev.crash_reports("Example", 5, APP))
    assert sorted(r.filename for r in found.reports) == [name_of(4), name_of(5)]
    # the same files, asked for the other app: only its own
    found = go(dev.crash_reports("Example", 5, OTHER))
    assert sorted(r.filename for r in found.reports) == [name_of(1), name_of(3)]


def test_only_mine_needs_an_affirmative_bundle_id_and_agreeing_ids():
    from fakes import FakeReport
    mine = FakeReport(name="Example", bundle_id=APP)
    anonymous = FakeReport(name="Example")
    assert diagnostics.only_mine([mine, anonymous], "Example", APP) == [mine]
    assert diagnostics.only_mine([mine], "Example", None) == []  # no bundle id to prove: nothing is shown


def test_a_report_over_the_byte_cap_is_never_read_and_is_counted(phone):
    files = {name_of(1): ips(bundle=APP), name_of(2): ips(bundle=APP)}
    dev, afc = phone(files, sizes={name_of(2): 512 * 1024 * 1024})
    found = go(dev.crash_reports("Example", 5, APP))
    assert [r.filename for r in found.reports] == [name_of(1)] and found.too_big == 1
    assert name_of(2) not in afc.opened
    with pytest.raises(device_module.CrashTooBig):
        go(dev.crash_file(name_of(2)))
    assert name_of(2) not in afc.opened
    assert go(dev.crash_file(name_of(1))) == files[name_of(1)]


def test_a_file_that_grows_after_the_size_check_is_cut_off_at_the_cap(phone, monkeypatch):
    monkeypatch.setattr(device_module, "CRASH_MAX_BYTES", 4096)
    big = ips(bundle=APP) + b" " * 100_000
    dev, afc = phone({name_of(1): big}, sizes={name_of(1): 100})  # the stat said 100 bytes
    found = go(dev.crash_reports("Example", 5, APP))
    assert found.reports == [] and found.too_big == 1
    assert afc.read_bytes <= 4096 + device_module.CRASH_CHUNK  # it stopped reading; it did not take the 100 KB
    with pytest.raises(device_module.CrashTooBig):
        go(dev.crash_file(name_of(1)))


def test_the_count_is_of_the_apps_own_reports_not_of_the_newest_files(phone):
    """CodeRabbit, PR #4: three newer reports of another app with the same executable name must not hide the app's."""
    files = {name_of(1): ips(bundle=APP), name_of(2): ips(bundle=OTHER), name_of(3): ips(bundle=OTHER),
             name_of(4): ips(bundle=OTHER)}
    dev, _ = phone(files)
    found = go(dev.crash_reports("Example", 1, APP))
    assert [r.filename for r in found.reports] == [name_of(1)] and not found.incomplete


def test_a_search_that_ran_out_of_its_budget_says_so_instead_of_no_crash(phone, monkeypatch):
    monkeypatch.setattr(device_module, "CRASH_SCAN", 3)
    files = {name_of(1): ips(bundle=APP)}  # the oldest (the fake phone's files get newer in this order): beyond the 3 newest
    files.update({name_of(n): ips(bundle=OTHER) for n in range(2, 8)})
    dev, _ = phone(files)
    found = go(dev.crash_reports("Example", 1, APP))
    assert found.reports == [] and found.incomplete
    assert diagnostics.CRASH_INCOMPLETE.format(n=3) in diagnostics.skipped_notes(found)


def test_the_answer_says_a_report_was_too_big():
    found = diagnostics.CrashSearch([], too_big=2)
    assert diagnostics.CRASH_TOO_BIG.format(n=2, mb=diagnostics.CRASH_MAX_MB) in diagnostics.skipped_notes(found)
