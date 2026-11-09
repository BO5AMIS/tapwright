"""The command line: serve/view exist; smoke and probe leave a running daemon alone."""
import argparse
import asyncio
import subprocess
import sys

from tapwright import cli, client


def test_smoke_and_probe_leave_a_running_daemon_alone(monkeypatch, capsys):
    monkeypatch.setattr(client, "healthy", lambda info, timeout=2.0: {"state": "live"})
    assert asyncio.run(cli.smoke(argparse.Namespace(app="com.example.app", host=None, wait=0, keep_open=False))) == 1
    assert asyncio.run(cli.probe(argparse.Namespace(host=None, watch=False))) == 1
    assert "tapwright serve --stop" in capsys.readouterr().out


def test_serve_view_and_mcp_commands():
    serve = subprocess.run([sys.executable, "-m", "tapwright", "serve", "--help"], capture_output=True, text=True)
    assert serve.returncode == 0 and all(f in serve.stdout for f in ("--phone", "--keep-alive", "--new-token", "--stop"))
    view = subprocess.run([sys.executable, "-m", "tapwright", "view", "--help"], capture_output=True, text=True)
    assert view.returncode == 0
    mcp = subprocess.run([sys.executable, "-m", "tapwright", "mcp", "--help"], capture_output=True, text=True)
    assert mcp.returncode == 0 and "--host" not in mcp.stdout
