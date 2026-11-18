"""`tapwright explore` (AGENT-TOOLS §25.2): the flags it sends, the scope it needs and its exit codes."""
import argparse

from tapwright import cli, client


def flags(**more) -> argparse.Namespace:
    base = dict(minutes=None, steps=None, depth=None, start=None, focus=None, fill=None, keep_going=False,
                apps="com.example.app", dedicated=False, phone=False, policy=None, accounts=None, maps=None)
    base.update(more)
    return argparse.Namespace(**base)


def patched(monkeypatch, answers):
    calls, scopes = [], []

    def call(tool, args, client_id, timeout=120.0):
        calls.append((tool, args, timeout))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False, phone=False: scopes.append((apps, dedicated, phone)))
    for name in ("set_policy", "set_maps", "set_accounts"):
        monkeypatch.setattr(client, name, lambda *a, **k: None)
    return calls, scopes


def test_it_sends_what_the_flags_say_and_exits_zero_when_nothing_was_found(monkeypatch, capsys):
    calls, scopes = patched(monkeypatch, [{"text": "Explored 5 places in 1.0 min (40 steps), 0 findings. Stopped: the frontier is empty."}])
    code = cli.explore(flags(minutes=2.0, steps=50, start="home", fill="test-data", keep_going=True))
    assert code == 0 and scopes == [(["com.example.app"], False, False)]
    tool, args, timeout = calls[0]
    assert tool == "explore" and args == {"minutes": 2.0, "steps": 50, "from": "home", "fill": "test-data", "continue": True}
    assert timeout == 480.0 and "Explored 5 places" in capsys.readouterr().out


def test_findings_are_exit_one(monkeypatch):
    patched(monkeypatch, [{"text": "Explored 5 places in 1.0 min (40 steps), 2 findings. Stopped: the app crashed.\nFindings: 1 crash."}])
    assert cli.explore(flags()) == 1


def test_it_searches_one_app_named_or_the_one_in_front_and_never_two_modes(monkeypatch, capsys):
    # 1.7.5: no app list is needed to use the phone; --apps names the ONE app explore searches (none: the app in front)
    calls, scopes = patched(monkeypatch, [{"text": "Explored 1 places in 0.1 min (3 steps), 0 findings."}])
    assert cli.explore(flags(apps=None)) == 0 and scopes == [([], False, False)] and len(calls) == 1
    assert cli.explore(flags(dedicated=True)) == 2 and len(calls) == 1
    assert cli.explore(flags(apps="com.a.one,com.b.two")) == 2 and len(calls) == 1
    assert "one app to search" in capsys.readouterr().out


def test_a_stop_by_a_person_or_a_pause_is_exit_two_with_the_report_named(monkeypatch, capsys):
    patched(monkeypatch, [client.DaemonError("paused", "paused by the user in the live view; ask them to resume. Done: 41 steps, 9 places. "
                                                       "Left at: place_3. Report: /x/report.md")])
    assert cli.explore(flags()) == 2
    assert "Error paused" in capsys.readouterr().out


def test_a_phone_in_use_before_it_starts_is_waited_for_then_skipped(monkeypatch, capsys):
    busy = client.DaemonError("busy", "a person is using the phone; try again in a moment")
    calls, _ = patched(monkeypatch, [busy, {"text": "Explored 1 places in 0.1 min (3 steps), 0 findings. Stopped: the frontier is empty."}])
    slept: list[float] = []
    assert cli.explore(flags(), sleep=slept.append) == 0 and slept == [cli.EXPLORE_RETRY] and len(calls) == 2
    patched(monkeypatch, [busy] * 100)
    slept.clear()
    assert cli.explore(flags(), sleep=slept.append) == 2
    assert sum(slept) == cli.EXPLORE_WAIT and "skipped: the phone was in use" in capsys.readouterr().out


def test_a_bad_policy_file_is_exit_two(monkeypatch, tmp_path, capsys):
    patched(monkeypatch, [])
    bad = tmp_path / "p.json"
    bad.write_text("{not json", "utf-8")
    assert cli.explore(flags(policy=str(bad))) == 2
    assert "policy file can't be used" in capsys.readouterr().out
