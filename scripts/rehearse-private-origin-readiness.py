#!/usr/bin/env python3
"""Rehearse the sanitized origin through an offline bare remote and fresh clone."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

from ao_lore._strict_io import ContractError, strict_read_json
from ao_lore.public_release_contracts import validate_public_release_readiness
from ao_lore.public_release_contracts import validate_public_release_policy
from ao_lore.public_release_export import PublicReleaseDependencies, prepare_sanitized_repository
from ao_lore.public_release_safety import scan_public_tree


class ReadinessError(RuntimeError):
    pass


_PRIVATE_ORIGIN_FILES = {
    ".github/workflows/ci.yml": ("5d51db43186722b7f32d8d75344b1345918b2fac1750865e493b93dc49ddc608", 623),
    "ci-requirements.txt": ("19f9eb7827f34df22b5634a621e7d4d279fc0a19ec7d383bd9cbc7608841baeb", 345),
}
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
_GATE_DEPENDENCIES = {"attrs": "23.2.0", "jsonschema": "4.10.3", "pyrsistent": "0.20.0"}
_REQUIREMENTS_SHA256 = "19f9eb7827f34df22b5634a621e7d4d279fc0a19ec7d383bd9cbc7608841baeb"


def _repository_head(repository_root: Path) -> str:
    try:
        info = os.lstat(repository_root)
    except OSError as exc:
        raise ReadinessError("repository root is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReadinessError("repository root must be a real directory")
    if repository_root.resolve() != repository_root:
        raise ReadinessError("repository root must be canonical")
    head = _run(["/usr/bin/git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=repository_root).stdout.strip().decode("ascii")
    if not _GIT_HEAD.fullmatch(head):
        raise ReadinessError("repository HEAD is invalid")
    return head


def _committed_private_origin_blob(repository_root: Path, head: str, relative: str) -> tuple[str, int]:
    """Return the exact committed blob digest and size for one fixed path."""
    records = _run(["/usr/bin/git", "ls-tree", "-rz", "-l", "--full-tree", head, "--", relative], cwd=repository_root).stdout.split(b"\0")
    records = [record for record in records if record]
    if len(records) != 1:
        raise ReadinessError("required private-origin file is missing or ambiguous")
    header, separator, raw_path = records[0].partition(b"\t")
    fields = header.split()
    if not separator or len(fields) != 4 or raw_path != relative.encode("utf-8"):
        raise ReadinessError("required private-origin tree entry is malformed")
    mode, kind, object_id, size_raw = fields
    if mode not in {b"100644", b"100755"} or kind != b"blob" or not re.fullmatch(rb"[0-9a-f]{40}", object_id):
        raise ReadinessError("required private-origin file is not a regular blob")
    try:
        size = int(size_raw)
    except ValueError as exc:
        raise ReadinessError("required private-origin blob size is invalid") from exc
    expected_size = _PRIVATE_ORIGIN_FILES[relative][1]
    if size != expected_size:
        raise ReadinessError("required private-origin blob size is outside its canonical bound")
    body = _run(["/usr/bin/git", "cat-file", "blob", object_id.decode("ascii")], cwd=repository_root).stdout
    if len(body) != size:
        raise ReadinessError("required private-origin blob size drifted")
    return hashlib.sha256(body).hexdigest(), size


def validate_private_origin_export_contract(policy: object, repository_root: Path) -> dict[str, object]:
    """Validate the hosted-gate files against immutable committed Git blobs."""
    try:
        validated = validate_public_release_policy(policy)
    except ContractError as exc:
        raise ReadinessError("public release policy rejected") from exc
    required = sorted(_PRIVATE_ORIGIN_FILES)
    if validated["included_files"] != sorted(validated["included_files"]):
        raise ReadinessError("private-origin included files are not sorted")
    if not all(path in validated["included_files"] for path in required):
        raise ReadinessError("private-origin policy must include exact hosted-gate files")
    if any(path.startswith(".github/workflows/") and path != ".github/workflows/ci.yml" for path in validated["included_files"]):
        raise ReadinessError("private-origin workflow binding must be exact")
    if any(value for value in validated["authority"].values()):
        raise ReadinessError("private-origin policy cannot grant authority")
    head = _repository_head(repository_root)
    files = []
    for relative in required:
        digest, size = _committed_private_origin_blob(repository_root, head, relative)
        if digest != _PRIVATE_ORIGIN_FILES[relative][0]:
            raise ReadinessError("committed private-origin blob digest drifted")
        files.append({"path": relative, "sha256": digest, "size": size})
    if _repository_head(repository_root) != head:
        raise ReadinessError("repository HEAD changed during validation")
    return {
        "schema_version": "ao.lore.private-origin-export-contract.v0.1",
        "source_head": head,
        "files": files,
        "authority": {key: False for key in ("github_create", "github_push", "visibility_change", "hosted_ci", "release", "deployment")},
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--check", action="store_true")
    return value


def _environment() -> dict[str, str]:
    value = {key: item for key, item in os.environ.items() if not key.startswith("GIT_")}
    value.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "LC_ALL": "C", "TZ": "UTC"})
    return value


def _run(arguments: list[str], *, cwd: Path, timeout: int = 300, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(arguments, cwd=cwd, env=environment or _environment(), capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReadinessError("offline readiness subprocess failed") from exc
    if result.returncode != 0:
        raise ReadinessError("offline readiness subprocess rejected")
    return result


def _tree_digest(repository: Path) -> str:
    body = _run(["/usr/bin/git", "ls-tree", "-rz", "--full-tree", "HEAD"], cwd=repository).stdout
    return hashlib.sha256(body).hexdigest()


def _gitleaks_arguments(binary: Path) -> list[str]:
    return [str(binary), "git", "--no-banner", "--redact"]


def _modern_gitleaks() -> Path:
    candidates = [Path(item) / "gitleaks" for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    candidates.append(Path(os.environ.get("GOPATH", str(Path.home() / "go"))) / "bin/gitleaks")
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            info = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
            continue
        probe = subprocess.run(
            [str(candidate), "git", "--help"],
            env=_environment(),
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise ReadinessError("gitleaks with Git object-history support is unavailable")


def _gate_consistency_arguments() -> list[str]:
    return ["-m", "pip", "check"]


def _validate_gate_requirements(clone: Path) -> None:
    requirements = clone / "ci-requirements.txt"
    try:
        info = os.lstat(requirements)
        body = requirements.read_bytes()
    except OSError as exc:
        raise ReadinessError("clean gate requirements file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or len(body) > 65536:
        raise ReadinessError("clean gate requirements file is unsafe")
    if hashlib.sha256(body).hexdigest() != _REQUIREMENTS_SHA256:
        raise ReadinessError("clean gate requirements bytes drifted")


def _validate_gate_dependency_versions(output: bytes) -> None:
    versions: dict[str, str] = {}
    for line in output.decode("ascii", "strict").splitlines():
        name, separator, version = line.partition("==")
        if not separator or name in versions:
            raise ReadinessError("clean gate dependency metadata is malformed")
        versions[name] = version
    if versions != _GATE_DEPENDENCIES:
        raise ReadinessError("clean gate dependency versions are not exact")


def _validate_gate_interpreter_metadata(output: bytes) -> None:
    lines = output.decode("ascii", "strict").splitlines()
    if len(lines) != 4:
        raise ReadinessError("clean gate interpreter metadata is incomplete")
    try:
        major, minor = (int(item) for item in lines[0].split(".", 1))
    except (ValueError, TypeError) as exc:
        raise ReadinessError("clean gate interpreter version is invalid") from exc
    if (major, minor) < (3, 11):
        raise ReadinessError("clean gate requires Python 3.11 or newer")
    _validate_gate_dependency_versions(("\n".join(lines[1:]) + "\n").encode("ascii"))


def _select_gate_interpreter() -> str:
    candidate = Path(sys.executable)
    try:
        info = os.lstat(candidate)
    except OSError as exc:
        raise ReadinessError("invoked Python interpreter is unavailable") from exc
    if not candidate.is_absolute() or (not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)):
        raise ReadinessError("invoked Python interpreter path is unsafe")
    probe = _run(
        [str(candidate), "-c", "import importlib.metadata as m,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print('\\n'.join(f'{n}=={m.version(n)}' for n in ('attrs','jsonschema','pyrsistent')))"],
        cwd=REPOSITORY,
    )
    _validate_gate_interpreter_metadata(probe.stdout)
    return str(candidate)


def _prepare_gate_environment(clone: Path, temporary: Path) -> tuple[Path, dict[str, str]]:
    supported_python = _select_gate_interpreter()
    gate = temporary / "supported-python-gate"
    _run([supported_python, "-m", "venv", "--system-site-packages", str(gate)], cwd=temporary)
    gate_python = gate / "bin/python"
    _validate_gate_requirements(clone)
    metadata = _run(
        [str(gate_python), "-c", "import importlib.metadata as m; print('\\n'.join(f'{n}=={m.version(n)}' for n in ('attrs','jsonschema','pyrsistent')))"],
        cwd=temporary,
    )
    _validate_gate_dependency_versions(metadata.stdout)
    _run([str(gate_python), *_gate_consistency_arguments()], cwd=clone, timeout=600)
    # Keep caller PATH semantics for make check; AO Lore packaging commands
    # below receive the validated gate interpreter explicitly.
    environment = _environment()
    return gate_python, environment


def local_round_trip(staging: Path, bare_remote: Path, clone: Path) -> dict[str, object]:
    for path, label in ((staging, "staging"), (bare_remote.parent, "remote parent"), (clone.parent, "clone parent")):
        try: info = os.lstat(path)
        except OSError as exc: raise ReadinessError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode): raise ReadinessError(f"{label} must be a real directory")
    if bare_remote.exists() or bare_remote.is_symlink() or clone.exists() or clone.is_symlink():
        raise ReadinessError("round-trip destinations must be absent")
    if _run(["/usr/bin/git", "status", "--porcelain"], cwd=staging).stdout:
        raise ReadinessError("staging repository is dirty")
    if _run(["/usr/bin/git", "rev-list", "--all", "--count"], cwd=staging).stdout.strip() != b"1":
        raise ReadinessError("staging repository must contain one commit")
    if _run(["/usr/bin/git", "tag"], cwd=staging).stdout or _run(["/usr/bin/git", "remote"], cwd=staging).stdout:
        raise ReadinessError("staging repository has unexpected refs")
    staging_commit = _run(["/usr/bin/git", "rev-parse", "HEAD"], cwd=staging).stdout.decode().strip()
    staging_manifest = _tree_digest(staging)
    _run(["/usr/bin/git", "init", "-q", "--bare", str(bare_remote)], cwd=bare_remote.parent)
    _run(["/usr/bin/git", "push", str(bare_remote), f"{staging_commit}:refs/heads/main"], cwd=staging)
    _run(["/usr/bin/git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare_remote)
    refs = _run(["/usr/bin/git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=bare_remote).stdout.decode().splitlines()
    if refs != [f"refs/heads/main {staging_commit}"]:
        raise ReadinessError("bare remote refs drifted")
    _run(["/usr/bin/git", "clone", "-q", "--no-local", str(bare_remote), str(clone)], cwd=clone.parent)
    _run(["/usr/bin/git", "remote", "remove", "origin"], cwd=clone)
    clone_commit = _run(["/usr/bin/git", "rev-parse", "HEAD"], cwd=clone).stdout.decode().strip()
    clone_manifest = _tree_digest(clone)
    count = int(_run(["/usr/bin/git", "rev-list", "--all", "--count"], cwd=clone).stdout)
    if staging_commit != clone_commit or staging_manifest != clone_manifest or count != 1:
        raise ReadinessError("fresh clone identity drifted")
    if _run(["/usr/bin/git", "remote"], cwd=clone).stdout or _run(["/usr/bin/git", "status", "--porcelain"], cwd=clone).stdout:
        raise ReadinessError("fresh clone is not clean and disconnected")
    return {"staging_commit": staging_commit, "clone_commit": clone_commit, "staging_manifest_digest": staging_manifest, "clone_manifest_digest": clone_manifest, "reachable_commit_count": count, "network_used": False}


def _product_gates(clone: Path, policy: dict[str, object], temporary: Path) -> dict[str, str]:
    gate_python, environment = _prepare_gate_environment(clone, temporary)
    environment.update({"AO_LORE_CLEAN_GATE_CHILD": "1", "PYTHONPATH": "src", "PYTHONWARNINGS": "ignore::ResourceWarning", "PYTHON": str(gate_python)})
    _run(["make", "check"], cwd=clone, timeout=600, environment=environment)
    scan_root = temporary / "tracked-tree-scan"
    scan_root.mkdir()
    tracked = _run(["/usr/bin/git", "ls-files", "-z"], cwd=clone, environment=environment).stdout.split(b"\0")
    for raw in filter(None, tracked):
        try: relative = Path(os.fsdecode(raw))
        except UnicodeError as exc: raise ReadinessError("tracked scan path is not UTF-8") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise ReadinessError("tracked scan path is unsafe")
        source = clone / relative; target = scan_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    safety = scan_public_tree(scan_root, policy)
    if safety["status"] != "accepted": raise ReadinessError("fresh clone safety scan rejected")
    _run(_gitleaks_arguments(_modern_gitleaks()), cwd=clone, environment=environment)
    _run([str(Path(shutil.which("bandit") or "")), "-q", "--severity-level", "high", "-r", "src"], cwd=clone, environment=environment)
    wheels = temporary / "wheels"; wheels.mkdir()
    _run([str(gate_python), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), "."], cwd=clone, environment=environment)
    wheel_files = sorted(wheels.glob("*.whl"))
    if len(wheel_files) != 1: raise ReadinessError("wheel build did not produce exactly one artifact")
    install = temporary / "install"; install.mkdir()
    _run([str(gate_python), "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(install), str(wheel_files[0])], cwd=clone, environment=environment)
    _run([str(gate_python), "-c", "import ao_lore; print(ao_lore.__name__)"], cwd=temporary, environment={**environment, "PYTHONPATH": str(install)})
    _run(["/usr/bin/git", "fsck", "--full", "--no-dangling"], cwd=clone, environment=environment)
    return {"make_check": "pass", "safety": "pass", "gitleaks": "pass", "bandit": "pass", "wheel": "pass", "git_fsck": "pass"}


def _validate_export_binding(export: dict[str, object], staging: Path, contract: dict[str, object]) -> None:
    head = contract["source_head"]
    if export.get("source_head") != head:
        raise ReadinessError("sanitized export source head drifted")
    if _repository_head(REPOSITORY) != head:
        raise ReadinessError("source head changed before export handoff")
    staging_head = _repository_head(staging)
    for item in contract["files"]:
        digest, size = _committed_private_origin_blob(staging, staging_head, item["path"])
        if digest != item["sha256"] or size != item["size"]:
            raise ReadinessError("sanitized export hosted-gate blob drifted")


def rehearse() -> dict[str, object]:
    policy, _ = strict_read_json(REPOSITORY / "docs/public-release/export-policy.json", "public release policy", max_bytes=65536, root=REPOSITORY)
    contract = validate_private_origin_export_contract(policy, REPOSITORY)
    with tempfile.TemporaryDirectory(prefix="ao-lore-origin-readiness-") as raw:
        temporary = Path(raw); export_parent = temporary / "export"; export_parent.mkdir()
        export = prepare_sanitized_repository(PublicReleaseDependencies(REPOSITORY, export_parent), policy)
        staging = export_parent / "ao-lore-sanitized-staging"
        _validate_export_binding(export, staging, contract)
        round_trip = local_round_trip(staging, temporary / "remote.git", temporary / "clone")
        _product_gates(temporary / "clone", policy, temporary)
        if round_trip["clone_commit"] != export["commit"]:
            raise ReadinessError("round-trip clean commit drifted")
    return _readiness_report(export, policy)


def _readiness_report(export: dict[str, object], policy: dict[str, object]) -> dict[str, object]:
    report = {
        "schema_version": "ao.lore.public-release-readiness.v0.1",
        "status": "ready",
        "source_head": export["source_head"],
        "clean_commit": export["commit"],
        "policy_digest": policy["policy_digest"],
        "manifest_digest": export["manifest_digest"],
        "file_count": export["file_count"],
        "total_bytes": export["total_bytes"],
        "gates": {
            "contracts": "pass",
            "safety": "pass",
            "clean_checkout": "pass",
            "git_fsck": "pass",
        },
        "authority": {
            "github_created": False,
            "github_push": False,
            "network_used": False,
            "release": False,
            "deployment": False,
        },
    }
    manifest = {
        key: report[key]
        for key in ("source_head", "policy_digest", "manifest_digest", "file_count", "total_bytes")
    }
    return validate_public_release_readiness(report, manifest=manifest)


def main() -> int:
    parser().parse_args()
    try: report = rehearse()
    except (ContractError, ReadinessError, OSError, UnicodeError):
        print("ao-lore private origin readiness rejected", file=sys.stderr); return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__": raise SystemExit(main())
