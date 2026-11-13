"""The `tapwright` command's first step, before any other module of the package is loaded.

A development checkout in the middle of a merge holds conflict markers (`<<<<<<<`) in its source. Importing the CLI
would then fail with a SyntaxError in whichever module the import reaches first (signing.py, device.py, ...), before
`doctor` could say what is wrong (CodeRabbit and Codex, PR #5). So this module checks the package's own files first,
with checkout.py, which needs nothing beyond the standard library, and only then loads the CLI."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from . import checkout
    package = Path(__file__).resolve().parent
    if checkout.work_tree(package) is not None:  # an installed package has no conflict markers to look for
        verdict = checkout.check(package)
        if verdict is not None and verdict[0] == "fail":
            print(f"  FAIL  {verdict[1]}", flush=True)
            return 1
    from .cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
