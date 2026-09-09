#!/usr/bin/env python3
"""Fail closed unless a sealed private-calibration manifest is present."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

def reject() -> int:
    print("private calibration assets are not installed", file=sys.stderr)
    return 2

def main() -> int:
    if sys.argv != [sys.argv[0], "--check"]:
        return reject()
    root = Path(os.environ.get("AO_LORE_HOME", Path(__file__).resolve().parents[1] / ".ao-lore"))
    manifest = root / "private-calibration-assets.json"
    try:
        info = os.lstat(manifest)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            return reject()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if set(value) != {"schema_version", "assets"} or value["schema_version"] != "ao.lore.private-calibration-assets.v0.1":
            return reject()
        assets = value["assets"]
        if not isinstance(assets, list) or not assets:
            return reject()
        paths = []
        for item in assets:
            if set(item) != {"path", "sha256"} or not isinstance(item["path"], str) or item["path"].startswith("/") or ".." in Path(item["path"]).parts:
                return reject()
            target = root / item["path"]
            target_info = os.lstat(target)
            if not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode) or target_info.st_nlink != 1:
                return reject()
            if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                return reject()
            paths.append(item["path"])
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            return reject()
    except (OSError, UnicodeError, ValueError, TypeError):
        return reject()
    print(json.dumps({"status": "pass", "asset_count": len(assets)}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
