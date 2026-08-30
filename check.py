#!/usr/bin/env python
"""
Pre-commit checks: lint for undefined names, then run the suites.

Streamlit modules are executed top to bottom at request time, so a name that
is referenced before it is bound only fails in the browser -- which is how a
missing sidebar variable reached production twice. pyflakes catches exactly
that, statically, in under a second.

    python check.py
"""

import subprocess
import sys

FILES = [
    "scanner.py", "config.py", "run.py", "sweep.py", "portfolio.py",
    "build_cache.py", "record_snapshot.py",
    "orbfvg/pine.py", "orbfvg/strategy.py", "orbfvg/angel.py",
    "orbfvg/broker.py", "orbfvg/backtest.py", "orbfvg/live.py",
    "orbfvg/instruments.py", "orbfvg/screener.py",
    "tests/test_strategy.py", "tests/test_broker.py",
]


def run(label, cmd):
    print("\n== %s ==" % label)
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = (result.stdout + result.stderr).strip()
    if out:
        print(out[-3000:])
    ok = result.returncode == 0
    print("   %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    ok = True
    try:
        ok &= run("pyflakes", [sys.executable, "-m", "pyflakes"] + FILES)
    except FileNotFoundError:
        print("pyflakes not installed: pip install pyflakes")
        ok = False
    ok &= run("engine tests", [sys.executable, "tests/test_strategy.py"])
    ok &= run("broker tests", [sys.executable, "tests/test_broker.py"])
    print("\n%s" % ("All checks passed." if ok else "CHECKS FAILED."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
