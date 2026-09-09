#!/usr/bin/env python3
"""Run public tests with a unique disposable runtime root."""

import subprocess
import sys
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    runtime_parent = ROOT / ".ao-lore"
    created_parent = not runtime_parent.exists()
    runtime_parent.mkdir(mode=0o700, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="clean-tests-", dir=runtime_parent) as runtime:
            environment = {
                **os.environ,
                "AO_LORE_HOME": runtime,
                "PYTHONWARNINGS": "ignore::ResourceWarning",
            }
            return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], cwd=ROOT, env=environment, check=False).returncode
    finally:
        if created_parent:
            try:
                runtime_parent.rmdir()
            except OSError:
                pass

if __name__ == "__main__":
    raise SystemExit(main())
