#!/usr/bin/env python3
"""Prepare the fixed local sanitized AO Lore staging repository."""

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY
STAGING_PARENT = REPOSITORY.parent
sys.path.insert(0, str(REPOSITORY / "src"))

from ao_lore._strict_io import ContractError, strict_read_json
from ao_lore.public_release_export import PublicReleaseDependencies, prepare_sanitized_repository

def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--check", action="store_true")
    return value

def main() -> int:
    arguments = parser().parse_args()
    if not arguments.check:
        print("ao-lore sanitized repository preparation requires --check", file=sys.stderr); return 2
    try:
        policy, _ = strict_read_json(REPOSITORY / "docs/public-release/export-policy.json", "public release policy", max_bytes=65536, root=REPOSITORY)
        report = prepare_sanitized_repository(PublicReleaseDependencies(SOURCE, STAGING_PARENT), policy)
    except (ContractError, OSError, UnicodeError) as exc:
        print("ao-lore sanitized repository preparation rejected", file=sys.stderr); return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0

if __name__ == "__main__": raise SystemExit(main())
