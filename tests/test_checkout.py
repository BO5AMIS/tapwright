"""doctor's look at where Tapwright runs from (1.3.1)."""
import subprocess
from types import SimpleNamespace

import pytest

from tapwright import checkout, cli


def package(tmp_path, source="x = 1\n", git=True):
    pkg = tmp_path / "repo" / "tapwright"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(source, encoding="utf-8")
    if git:
        (tmp_path / "repo" / ".git").mkdir()
        (tmp_path / "repo" / "pyproject.toml").write_text("[project]\nname = 'tapwright'\n", encoding="utf-8")
    return pkg


def git_says(out="", code=0):
    return lambda *a, **k: SimpleNamespace(returncode=code, stdout=out)


def test_an_installed_package_is_fine(tmp_path):
    assert checkout.check(package(tmp_path, git=False))[0] == "ok"


def test_a_pinned_release_in_a_venv_inside_a_git_repo_is_installed(tmp_path):
    """Review 1.3.1: the docs' own advice (a venv with a pinned release) gave a 'development checkout' note when the venv
    sat inside a git repository."""
    pkg = tmp_path / "repo" / "venv" / "Lib" / "site-packages" / "tapwright"
    pkg.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    assert checkout.work_tree(pkg) is None
    assert checkout.check(pkg, git_says(" M x\n"))[0] == "ok"


def test_conflict_markers_fail_and_say_where(tmp_path):
    pkg = package(tmp_path, "a = 1\n<<<<<<< HEAD\nb = 2\n=======\nb = 3\n>>>>>>> feature\n")
    kind, message = checkout.check(pkg, git_says(""))
    assert kind == "fail" and "mod.py" in message and "pip install tapwright==" in message
    assert cli.report_checkout(pkg) == 1


def test_uncommitted_changes_warn_but_do_not_fail(tmp_path):
    pkg = package(tmp_path)
    kind, message = checkout.check(pkg, git_says(" M tapwright/mod.py\n?? tapwright/new.py\n"))
    assert kind == "note" and "2 uncommitted changes" in message
    assert checkout.check(pkg, git_says(""))[0] == "ok"


def test_no_git_is_skipped_quietly(tmp_path):
    pkg = package(tmp_path)

    def missing(*a, **k):
        raise FileNotFoundError("git")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)
    assert checkout.check(pkg, missing) is None and checkout.check(pkg, slow) is None
    assert checkout.check(pkg, git_says("", 128)) is None


def _conflicted_copy(tmp_path, module: str):
    import shutil
    from pathlib import Path
    import tapwright
    copy = tmp_path / "src"
    shutil.copytree(Path(tapwright.__file__).parent, copy / "tapwright", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "pyproject.toml").write_text('[project]\nname = "tapwright"\n', encoding="utf-8")
    (copy / ".git").mkdir()  # a source checkout in a git work tree
    (copy / "tapwright" / module).write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> other\n", encoding="utf-8")
    return copy


def _run_tapwright(copy, tmp_path, *args):
    import os
    import subprocess
    import sys
    env = {"PYTHONPATH": str(copy), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "TAPWRIGHT_HOME": str(tmp_path / "home")}
    r = subprocess.run([sys.executable, "-m", "tapwright", *args], cwd=copy, capture_output=True, text=True,
                       timeout=120, env=env)
    return r.returncode, r.stdout + r.stderr


@pytest.mark.parametrize("module", ["signing.py", "device.py", "cli.py", "daemon.py"])
def test_a_fresh_tapwright_names_merge_markers_in_any_module_before_loading_it(tmp_path, module):
    """`python -m tapwright` (and the `tapwright` command, tapwright.entry:main) checks the package's files for merge
    markers before the CLI is imported, so whichever module holds them, the answer names it instead of a SyntaxError,
    and nothing reaches for the phone (CodeRabbit and Codex, PR #5 cycles 2 and 3)."""
    copy = _conflicted_copy(tmp_path, module)
    code, out = _run_tapwright(copy, tmp_path, "doctor")
    assert code == 1 and "SyntaxError" not in out, out
    assert "merge markers" in out and module in out, out
    assert "device service" not in out, out
