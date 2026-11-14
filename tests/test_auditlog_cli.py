"""AGENT-TOOLS §23: `tapwright audit-log` is the person's command (not an MCP tool): `--since <date or ISO time>` lists the
lines since then as written, `--delete` deletes the log."""
import datetime

import pytest

from tapwright import auditlog, cli, mcp_server


def write(folder, when: str, kind: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{when[:7]}.jsonl", "a", encoding="utf-8") as f:
        f.write(f'{{"t": "{when}", "tool": "tapwright", "kind": "{kind}"}}\n')


def test_lists_since_and_deletes(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "audit"
    monkeypatch.setattr(auditlog, "default_folder", lambda: folder)
    write(folder, "2026-10-30T08:00:00.000Z", "grant")
    write(folder, "2026-11-14T09:00:00.000Z", "otp")
    monkeypatch.setattr("sys.argv", ["tapwright", "audit-log"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert '"kind": "grant"' in out and '"kind": "otp"' in out
    monkeypatch.setattr("sys.argv", ["tapwright", "audit-log", "--since", "2026-11-01T00:00Z"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert '"kind": "otp"' in out and '"kind": "grant"' not in out
    monkeypatch.setattr("sys.argv", ["tapwright", "audit-log", "--since", "yesterday"])
    assert cli.main() == 2 and "--since takes a date" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["tapwright", "audit-log", "--delete"])
    assert cli.main() == 0 and "2 lines" in capsys.readouterr().out
    left = auditlog.read_lines(folder)  # (review 1.7 B-L12: the deletion leaves its own line)
    assert len(left) == 1 and '"kind": "approval"' in left[0] and "audit log deleted by the person" in left[0]
    with pytest.raises(SystemExit):  # one or the other
        monkeypatch.setattr("sys.argv", ["tapwright", "audit-log", "--delete", "--since", "2026-11-01"])
        cli.main()


def test_a_date_is_that_days_start_and_a_time_without_a_zone_is_this_computers():
    day = auditlog.since_of("2026-11-14")
    assert day.tzinfo is not None and (day.year, day.month, day.day, day.hour) == (2026, 11, 14, 0)
    assert auditlog.since_of("2026-11-14T09:30Z") == datetime.datetime(2026, 11, 14, 9, 30, tzinfo=datetime.timezone.utc)
    assert auditlog.since_of("2026-11-14T09:30").tzinfo is not None
    with pytest.raises(ValueError):
        auditlog.since_of("last week")


def test_it_is_no_mcp_tool():
    import asyncio
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert names and not any("audit" in name and name != "audit" for name in names)  # (`audit` is the a11y audit)
