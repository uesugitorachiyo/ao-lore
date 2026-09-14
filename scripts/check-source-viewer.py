#!/usr/bin/env python3
"""Run additive viewer tests and syntax checks; does not claim the upstream make-check gate."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
environment = dict(os.environ)
environment["PYTHONPATH"] = str(ROOT / "src")
commands = [
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_ao_lore_source_viewer*.py", "-v"],
    [sys.executable, "-m", "compileall", "-q", "src/ao_lore/source_viewer", "tests"],
]
for command in commands:
    code = subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode
    if code:
        raise SystemExit(code)
print("PASS: additive source-viewer tests and Python compilation. Upstream gate not included.")
