"""Deterministic standalone-boundary and contract self-check."""

from __future__ import annotations

import json
import os
import subprocess
import ast
import hashlib
import re
from pathlib import Path

from ._strict_io import ContractError, parse_strict_json, strict_read_json
from .home import repository_root, runtime_home
from .public_release_contracts import validate_public_release_policy


FORBIDDEN_MODULE_PREFIXES = (
    "ao_mission",
    "ao_blueprint",
    "ao_atlas",
    "ao_foundry",
    "ao_arena",
    "ao_sentinel",
)
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_SIBLING_NAMES = rb"ao-(?:mission|blueprint|atlas|foundry|arena|sentinel)|second-brain-template"
_FORBIDDEN_LITERAL = re.compile(
    rb"(?:/" + rb"opt/[A-Za-z0-9._-]+/(?:projects|workspaces)/(?:" + _SIBLING_NAMES + rb")"
    rb"|/" + rb"home/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~+-]+)+"
    rb"|AO_(?:MISSION|BLUEPRINT|ATLAS|FOUNDRY|ARENA|SENTINEL)_HOME)"
)


def _has_exact_inert_path_exception(path: str, body: bytes, policy: dict[str, object]) -> bool:
    digest = hashlib.sha256(body).hexdigest()
    return any(
        item["path"] == path
        and item["sha256"] == digest
        and item["reason_code"] == "inert_private_path"
        for item in policy["exceptions"]
    )


def _validate_repository_root(root: Path) -> None:
    if root.resolve() != root:
        raise ContractError("repository root is not absolute and canonical")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    try:
        probe = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("repository is not a verified Git checkout") from exc
    lines = probe.stdout.splitlines()
    if (
        probe.returncode != 0
        or probe.stderr
        or len(lines) != 2
        or Path(lines[0]).resolve() != root
        or not Path(lines[1]).is_absolute()
        or not Path(lines[1]).is_dir()
        or Path(lines[1]).is_symlink()
    ):
        raise ContractError("repository is not a verified Git checkout")


def run_selfcheck() -> dict[str, object]:
    root = repository_root()
    _validate_repository_root(root)
    home = runtime_home()
    try:
        home.relative_to(root)
    except ValueError as exc:
        raise ContractError("AO_LORE_HOME escapes the standalone repository") from exc

    symlinks: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in {".git", ".ao-lore", ".venv", "__pycache__"}]
        for name in [*directories, *files]:
            path = Path(current) / name
            if path.is_symlink():
                symlinks.append(str(path.relative_to(root)))
    if symlinks:
        raise ContractError(f"repository contains symlinks: {sorted(symlinks)}")

    policy_value, _ = strict_read_json(
        root / "docs/public-release/export-policy.json",
        "public release policy",
        max_bytes=65536,
        root=root,
    )
    policy = validate_public_release_policy(policy_value)
    source_violations: list[str] = []
    source_files = sorted((root / "src").rglob("*.py")) + sorted((root / "tests").glob("test_*.py"))
    for path in source_files:
        if path.name == "selfcheck.py":
            continue
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise ContractError(f"source file exceeds self-check bound: {relative}")
        body = path.read_bytes()
        if _FORBIDDEN_LITERAL.search(body) and not _has_exact_inert_path_exception(relative, body, policy):
            source_violations.append(f"{relative}:forbidden_literal")
        tree = ast.parse(body.decode("utf-8"), filename=relative)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for module in imported:
            if any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_MODULE_PREFIXES):
                source_violations.append(f"{path.relative_to(root)}:{module}")
    if source_violations:
        raise ContractError(f"sibling dependency tokens found: {source_violations}")

    schemas = sorted((root / "schemas" / "ao-lore").glob("*.schema.json"))
    if not schemas:
        raise ContractError("no AO Lore schemas found")
    for path in schemas:
        parse_strict_json(path.read_bytes(), str(path.relative_to(root)))

    return {
        "status": "pass",
        "schema_count": len(schemas),
        "source_files_checked": len(source_files),
        "sibling_dependencies": 0,
        "symlinks": 0,
    }


def main() -> int:
    try:
        report = run_selfcheck()
    except (ContractError, OSError, UnicodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
