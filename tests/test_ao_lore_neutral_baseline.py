"""Positive contract for the reusable, domain-neutral AO Lore baseline."""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import inspect
import json
import os
import socket
import subprocess
import tempfile
import textwrap
import unittest
import urllib.request
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import _parser, main
from ao_lore.home import repository_root
from ao_lore.selfcheck import run_selfcheck


ROOT = repository_root()

BOUNDARY_FILES = {
    "sources/README.md",
    "brain/README.md",
    "inbox/README.md",
    "working/candidates/README.md",
}
WORKFLOWS = {
    "workflows/answer-from-brain.md",
    "workflows/process-ingested-document.md",
    "workflows/promote-candidate.md",
    "workflows/rehearse-sanitized-lifecycle.md",
    "workflows/review-candidates.md",
}
CLI_SURFACES = {
    "benchmark",
    "candidate",
    "evaluation",
    "ingest",
    "ingest-batch",
    "ingest-docx",
    "knowledge",
    "monitoring",
    "promotion",
    "workspace",
}
CLI_ACTIONS = {
    "workspace": {"candidate", "ingest", "inspect", "list", "query", "recover", "refresh", "replay"},
}
RUN_HANDLERS = {
    "_run_candidate",
    "_run_ingest",
    "_run_ingest_batch",
    "_run_ingest_docx",
    "_run_knowledge",
    "_run_promotion",
    "_run_workspace",
    "_run_workspace_candidate",
}
WORKSPACE_OPERATIONS = {
    "inspect_workspace",
    "refresh_workspace",
    "replay_workspace",
    "recover_workspace",
    "open_workspace_context",
    "load_workspace_document_context",
    "read_workspace_inbox_source",
    "revalidate_workspace_inbox_source",
}
AUTHORITY_FIELDS = {
    "deployment",
    "github_create",
    "github_push",
    "hosted_ci",
    "release",
    "visibility_change",
}

APPROVED_INCLUDED_FILES = {
    ".gitattributes", ".github/workflows/ci.yml", ".gitignore", ".gitleaks.toml",
    "AGENTS.md", "CONTRIBUTING.md", "LICENSE", "Makefile", "README.md",
    "ROADMAP-ADDENDUM.md", "ROADMAP.md", "SECURITY.md", "ci-requirements.txt",
    "pyproject.toml",
}
APPROVED_INCLUDED_PREFIXES = {
    "brain/", "docs/architecture/", "docs/contracts/", "docs/public-release/",
    "docs/recovery/", "docs/workflows/", "inbox/", "schemas/", "scripts/",
    "sources/", "src/", "tests/", "workflows/", "working/",
}
APPROVED_EXCLUDED_PREFIXES = {
    ".ao-lore/", ".git/", ".worktrees/", "docs/evidence/", "docs/superpowers/",
}

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
OS_MUTATION_METHODS = (
    "mkdir", "makedirs", "rename", "replace", "unlink", "remove", "rmdir",
    "link", "symlink", "truncate", "chmod", "chown", "utime",
    "fchmod", "fchown", "setxattr", "removexattr",
)
PATH_MUTATION_METHODS = (
    "write_text", "write_bytes", "touch", "mkdir", "rename", "replace", "unlink",
    "link_to", "hardlink_to", "symlink_to", "chmod", "lchmod", "chown", "lchown",
    "utime",
)


def _tracked(*prefixes: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *prefixes],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _checkout_status(checkout: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "--untracked-files=all"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _checkout_tracked(checkout: Path, *prefixes: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *prefixes],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _child_environment(checkout: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(checkout / "src"),
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONOPTIMIZE": "0",
        "PYTHONHASHSEED": "0",
    }


def _filesystem_snapshot(root: Path) -> dict[str, tuple]:
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        xattrs = tuple((name, os.getxattr(path, name)) for name in sorted(os.listxattr(path)))
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        snapshot[relative] = (
            "directory" if path.is_dir() else "file" if path.is_file() else "other",
            info.st_mode, info.st_uid, info.st_gid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink, digest, xattrs,
        )
    return snapshot


def _subcommands(parser, surface: str) -> set[str]:
    surfaces = next(action for action in parser._subparsers._group_actions if action.dest == "surface")
    command_parser = surfaces.choices[surface]
    commands = next(
        action for action in command_parser._subparsers._group_actions
        if action.dest == "command"
    )
    return set(commands.choices)


def _write_attempt_guard(attempts: list[str]):
    stack = ExitStack()

    def reject(label: str):
        attempts.append(label)
        raise AssertionError("write attempt")

    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if flags & WRITE_FLAGS:
            reject("open")
        return original_open(path, flags, *args, **kwargs)

    def guarded_file_open(original, label, *args, **kwargs):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        if any(flag in mode for flag in "wax+"):
            reject(label)
        return original(*args, **kwargs)

    original_builtin_open = builtins.open
    original_io_open = io.open
    original_path_open = Path.open
    stack.enter_context(patch.object(
        builtins, "open",
        lambda *args, **kwargs: guarded_file_open(original_builtin_open, "builtins.open", *args, **kwargs),
    ))
    stack.enter_context(patch.object(
        io, "open",
        lambda *args, **kwargs: guarded_file_open(original_io_open, "io.open", *args, **kwargs),
    ))
    stack.enter_context(patch.object(
        Path, "open",
        lambda *args, **kwargs: guarded_file_open(original_path_open, "Path.open", *args, **kwargs),
    ))
    stack.enter_context(patch.object(os, "open", guarded_open))
    for name in OS_MUTATION_METHODS:
        stack.enter_context(patch.object(os, name, lambda *args, _name=name, **kwargs: reject(_name)))
    for name in PATH_MUTATION_METHODS:
        if not hasattr(Path, name):
            continue
        stack.enter_context(patch.object(Path, name, lambda *args, _name=name, **kwargs: reject(_name)))
    return stack


class NeutralBaselineTests(unittest.TestCase):
    def test_read_only_checks_do_not_use_fixed_runtime_sentinels(self):
        source = Path(__file__).read_text(encoding="utf-8")
        fixed_assignment = "check_runtime = ROOT / " + '".neutral-baseline-runtime"'
        self.assertNotIn(fixed_assignment, source)

    def test_boundary_roots_contain_only_public_readmes(self):
        self.assertEqual(BOUNDARY_FILES, _tracked("sources/", "brain/", "inbox/", "working/candidates/"))

    def test_retired_domain_source_root_is_not_ignored(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        retired_root = "public-" + "chi" + "co-" + "habi" + "tability-v1"
        self.assertNotIn(retired_root, ignored)

    def test_workflow_inventory_is_the_generic_allowlist(self):
        actual = _tracked("workflows/")
        self.assertEqual(len(WORKFLOWS), len(actual))
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.removeprefix("workflows/")):
                self.assertIn(workflow, actual)

    def test_public_cli_has_exact_generic_surface_and_actions(self):
        parser = _parser()
        surfaces = next(action for action in parser._subparsers._group_actions if action.dest == "surface")
        actual_surfaces = set(surfaces.choices)
        self.assertEqual(len(CLI_SURFACES), len(actual_surfaces))
        for surface in CLI_SURFACES:
            with self.subTest(surface=surface):
                self.assertIn(surface, actual_surfaces)
        for surface, expected in CLI_ACTIONS.items():
            with self.subTest(surface=surface):
                actual = _subcommands(parser, surface)
                self.assertEqual(len(expected), len(actual))
                for action in expected:
                    self.assertIn(action, actual)

    def test_module_run_handlers_are_the_generic_allowlist(self):
        import ao_lore.__main__ as cli

        actual = {
            name for name, value in vars(cli).items()
            if inspect.isfunction(value)
            and value.__module__ == cli.__name__
            and name.startswith("_run_")
        }
        self.assertEqual(len(RUN_HANDLERS), len(actual))
        for handler in RUN_HANDLERS:
            with self.subTest(handler=handler):
                self.assertIn(handler, actual)

    def test_workspace_runtime_operation_surface_is_generic(self):
        import ao_lore.workspace_runtime as runtime

        actual = {
            name for name, value in vars(runtime).items()
            if inspect.isfunction(value)
            and value.__module__ == runtime.__name__
            and not name.startswith("_")
        }
        self.assertEqual(len(WORKSPACE_OPERATIONS), len(actual))
        for operation in WORKSPACE_OPERATIONS:
            with self.subTest(operation=operation):
                self.assertIn(operation, actual)

    def test_no_write_guard_rejects_all_mutation_paths_but_allows_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("readable", encoding="utf-8")
            empty = root / "empty"
            empty.mkdir()
            attempts = []
            with _write_attempt_guard(attempts):
                mutations = [
                    lambda: builtins.open(target, "w"),
                    lambda: io.open(target, "a"),
                    lambda: target.open("x"),
                    lambda: os.open(target, os.O_WRONLY | os.O_CREAT, 0o600),
                    lambda: target.write_text("x"),
                    lambda: target.write_bytes(b"x"),
                    lambda: target.touch(),
                    lambda: os.mkdir(root / "os-mkdir"),
                    lambda: os.makedirs(root / "os-makedirs"),
                    lambda: target.mkdir(),
                    lambda: os.remove(target),
                    lambda: os.unlink(target),
                    lambda: target.unlink(),
                    lambda: os.rename(target, root / "renamed"),
                    lambda: os.replace(target, root / "replaced"),
                    lambda: target.rename(root / "path-renamed"),
                    lambda: target.replace(root / "path-replaced"),
                    lambda: os.rmdir(empty),
                    lambda: os.link(target, root / "hard-linked"),
                    lambda: os.symlink(target, root / "symlinked"),
                    lambda: os.truncate(target, 0),
                    lambda: os.chmod(target, 0o600),
                    lambda: os.chown(target, os.getuid(), os.getgid()),
                    lambda: os.utime(target, None),
                    lambda: target.hardlink_to(root / "path-hard-linked"),
                    lambda: target.symlink_to(root / "path-symlinked"),
                    lambda: target.chmod(0o600),
                    lambda: target.touch(exist_ok=True),
                ]
                descriptor = os.open(target, os.O_RDONLY)
                mutations.extend((
                    lambda: os.fchmod(descriptor, 0o600),
                    lambda: os.fchown(descriptor, os.getuid(), os.getgid()),
                ))
                os.close(descriptor)
                if hasattr(os, "setxattr"):
                    mutations.append(lambda: os.setxattr(target, "user.ao_lore_probe", b"x"))
                if hasattr(os, "removexattr"):
                    mutations.append(lambda: os.removexattr(target, "user.ao_lore_probe"))
                for mutation in mutations:
                    with self.assertRaises(AssertionError):
                        mutation()
                with builtins.open(target, encoding="utf-8") as stream:
                    self.assertEqual("readable", stream.read())
                with io.open(target, encoding="utf-8") as stream:
                    self.assertEqual("readable", stream.read())
                with target.open(encoding="utf-8") as stream:
                    self.assertEqual("readable", stream.read())
                descriptor = os.open(target, os.O_RDONLY)
                os.close(descriptor)
            self.assertEqual(len(mutations), len(attempts))

    def test_empty_workspace_inspection_and_selfcheck_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            runtime = parent / "runtime"
            before = set(parent.iterdir())
            output, errors, attempts = StringIO(), StringIO(), []
            with ExitStack() as stack:
                stack.enter_context(patch("ao_lore.__main__.runtime_home", return_value=runtime))
                stack.enter_context(redirect_stdout(output))
                stack.enter_context(redirect_stderr(errors))
                stack.enter_context(_write_attempt_guard(attempts))
                self.assertEqual(0, main(["workspace", "list", "--json"]))
            self.assertTrue(output.getvalue())
            self.assertEqual("", errors.getvalue())
            self.assertEqual([], attempts)
            self.assertEqual(before, set(parent.iterdir()))

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            check_runtime = Path(directory) / "runtime"
            attempts = []
            with ExitStack() as stack:
                stack.enter_context(patch("ao_lore.selfcheck.runtime_home", return_value=check_runtime))
                stack.enter_context(_write_attempt_guard(attempts))
                report = run_selfcheck()
            self.assertEqual("pass", report["status"])
            self.assertEqual([], attempts)
            self.assertFalse(check_runtime.exists())

    def test_clean_checkout_empty_lifecycle_is_bounded_and_write_free(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.addCleanup(
                lambda: subprocess.run(
                    ["git", "worktree", "remove", "--force", str(checkout)],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            )
            self.assertFalse((checkout / ".ao-lore").exists())
            self.assertEqual("", _checkout_status(checkout))
            self.assertEqual(
                BOUNDARY_FILES,
                _checkout_tracked(checkout, "sources/", "brain/", "inbox/", "working/candidates/"),
            )
            self.assertFalse(any(
                path.name == "__pycache__"
                or path.suffix == ".pyc"
                or path.name == ".ao-lore"
                for path in checkout.rglob("*")
                if ".git" not in path.parts
            ))
            before_snapshot = _filesystem_snapshot(checkout)
            script = textwrap.dedent(
                """
                import builtins
                import contextlib
                import io
                import json
                import os
                import socket
                import shutil
                import sys
                from pathlib import Path
                import subprocess
                import urllib.request
                from unittest.mock import patch

                expected_environment = {
                    "PATH", "PYTHONPATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE",
                    "PYTHONOPTIMIZE", "PYTHONHASHSEED",
                }
                if set(os.environ) != expected_environment:
                    raise RuntimeError("child environment is not minimal")
                if os.environ["PYTHONDONTWRITEBYTECODE"] != "1" or os.environ["PYTHONOPTIMIZE"] != "0" or os.environ["PYTHONHASHSEED"] != "0":
                    raise RuntimeError("child environment is not deterministic")

                from ao_lore.__main__ import main
                import ao_lore.__main__ as cli

                attempts = []
                audit_events = []
                audit_enabled = False
                def audit_guard(event, args):
                    if not audit_enabled:
                        return
                    mutating = {
                        "os.chmod", "os.chown", "os.link", "os.mkdir", "os.remove",
                        "os.rename", "os.rmdir", "os.setxattr", "os.removexattr",
                        "os.symlink", "os.truncate", "os.utime", "os.fchmod", "os.fchown",
                        "os.setxattr", "os.removexattr", "subprocess.Popen",
                        "socket.connect",
                    }
                    if event == "open":
                        mode = args[1] if len(args) > 1 else None
                        flags = args[2] if len(args) > 2 else 0
                        if (isinstance(mode, str) and any(flag in mode for flag in "wax+")) or flags & __WRITE_FLAGS__:
                            mutating.add(event)
                    if event in mutating:
                        audit_events.append(event)
                        raise RuntimeError("audited mutation")
                sys.addaudithook(audit_guard)
                def reject(label):
                    attempts.append(label)
                    raise AssertionError("write attempt")
                def guarded_open(original, label, *args, **kwargs):
                    mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
                    if any(flag in mode for flag in "wax+"):
                        reject(label)
                    return original(*args, **kwargs)
                original_open = builtins.open
                original_io_open = io.open
                original_path_open = Path.open
                original_os_open = os.open
                def os_open(path, flags, *args, **kwargs):
                    if flags & __WRITE_FLAGS__:
                        reject("os.open")
                    return original_os_open(path, flags, *args, **kwargs)
                stack = [
                    patch.object(builtins, "open", lambda *a, **k: guarded_open(original_open, "builtins.open", *a, **k)),
                    patch.object(io, "open", lambda *a, **k: guarded_open(original_io_open, "io.open", *a, **k)),
                    patch.object(Path, "open", lambda *a, **k: guarded_open(original_path_open, "Path.open", *a, **k)),
                    patch.object(os, "open", os_open),
                ]
                for name in __OS_MUTATION_METHODS__:
                    stack.append(patch.object(os, name, lambda *a, _name=name, **k: reject(_name)))
                for name in __PATH_MUTATION_METHODS__:
                    if not hasattr(Path, name):
                        continue
                    stack.append(patch.object(Path, name, lambda *a, _name=name, **k: reject(_name)))
                def tripwire(label):
                    attempts.append(label)
                    raise AssertionError(label)
                stack.extend([
                    patch.object(socket, "socket", lambda *a, **k: tripwire("socket")),
                    patch.object(socket, "create_connection", lambda *a, **k: tripwire("socket")),
                    patch.object(urllib.request, "urlopen", lambda *a, **k: tripwire("urlopen")),
                    patch.object(subprocess, "run", lambda *a, **k: tripwire("subprocess")),
                    patch.object(cli, "_workspace_refresh_transport", lambda: tripwire("provider")),
                    patch.object(cli, "_run_workspace_candidate", lambda *a, **k: tripwire("candidate")),
                    patch.object(cli, "_run_promotion", lambda *a, **k: tripwire("promotion")),
                ])
                original_environment = dict(os.environ)
                def invoke(arguments):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = main(arguments)
                    return code, stdout.getvalue(), stderr.getvalue()
                def require(condition, detail):
                    if not condition:
                        raise RuntimeError(detail)
                probe_root = Path.cwd() / ".mutation-guard-probe"
                probe_root.mkdir()
                probe_target = probe_root / "target"
                probe_target.write_text("probe", encoding="utf-8")
                audit_enabled = True
                def expect_rejected(operation):
                    try:
                        operation()
                    except (AssertionError, PermissionError, RuntimeError):
                        return
                    raise RuntimeError("mutation guard bypass")
                expect_rejected(lambda: probe_target.write_text("changed", encoding="utf-8"))
                expect_rejected(lambda: os.link(probe_target, probe_root / "hard-link"))
                expect_rejected(lambda: probe_target.chmod(0o600))
                expect_rejected(lambda: os.unlink(probe_target))
                descriptor = os.open(probe_target, os.O_RDONLY)
                expect_rejected(lambda: os.fchmod(descriptor, 0o600))
                expect_rejected(lambda: os.fchown(descriptor, os.getuid(), os.getgid()))
                os.close(descriptor)
                if hasattr(os, "setxattr"):
                    expect_rejected(lambda: os.setxattr(probe_target, "user.ao_lore_probe", b"x"))
                if hasattr(os, "removexattr"):
                    expect_rejected(lambda: os.removexattr(probe_target, "user.ao_lore_probe"))
                attempts.clear()
                for context in stack:
                    context.start()
                try:
                    cli.runtime_home = lambda: Path.cwd() / ".ao-lore"
                    listed = invoke(["workspace", "list", "--json"])
                    inspected = invoke(["workspace", "inspect", "--workspace", "missing", "--json"])
                    queried = invoke(["workspace", "query", "--workspace", "missing", "--prompt", "neutral", "--json"])
                finally:
                    for context in reversed(stack):
                        context.stop()
                audit_enabled = False
                shutil.rmtree(probe_root)
                expected_list = '{"active_count": 0, "authority_advanced": false, "candidate_decision": false, "candidate_review": false, "canonical_query": false, "credential": false, "deployment": false, "inactive_count": 0, "inspection_digest": "sha256:130683a8c462b36c494b32afa9a9b82db654abad5cf128dacd1c6219e878c680", "inspection_id": "inspection-c51e0ca985bdf80ef3b3d683", "investigate_count": 0, "legal_advice": false, "private_data": false, "promotion": false, "property_decision": false, "provider": false, "publication": false, "reason_code": "ok", "registry_digest": null, "registry_id": null, "release": false, "schema_version": "ao.lore.workspace-registry-inspection.v0.1", "sequence": 0, "status": "empty", "workspace_count": 0, "workspace_ids": []}\\n'
                require(listed == (0, expected_list, ""), listed)
                for refusal in (inspected, queried):
                    require(refusal[0] == 2, refusal)
                    require(refusal[1] == "", refusal)
                    require(refusal[2] == "ao-lore: workspace operation rejected\\n", refusal)
                    require(len(refusal[2]) <= 64, refusal)
                    require(all(ord(char) >= 32 or char == "\\n" for char in refusal[2]), refusal)
                    require("/" not in refusal[2] and "\\\\" not in refusal[2], refusal)
                require(attempts == [], attempts)
                require(audit_events, "audit hook did not observe mutations")
                require(dict(os.environ) == original_environment, "environment changed")
                """
            ).replace("__WRITE_FLAGS__", repr(WRITE_FLAGS)).replace(
                "__OS_MUTATION_METHODS__", repr(OS_MUTATION_METHODS)
            ).replace("__PATH_MUTATION_METHODS__", repr(PATH_MUTATION_METHODS))
            completed = subprocess.run(
                ["python3", "-c", script],
                cwd=checkout,
                env=_child_environment(checkout),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn(str(checkout), completed.stdout + completed.stderr)
            self.assertNotIn("/home/", completed.stdout + completed.stderr)
            self.assertNotIn("/opt/", completed.stdout + completed.stderr)
            self.assertFalse((checkout / ".ao-lore").exists())
            self.assertEqual("", _checkout_status(checkout))
            self.assertEqual(before_snapshot, _filesystem_snapshot(checkout))

    def test_clean_checkout_sanitized_lifecycle_replays_neutral_fixture_only(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.addCleanup(
                lambda: subprocess.run(
                    ["git", "worktree", "remove", "--force", str(checkout)],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            )
            environment = _child_environment(checkout)
            lifecycle_code = (
                "import os,runpy,sys; expected={'PATH','PYTHONPATH','LANG','LC_ALL',"
                "'PYTHONDONTWRITEBYTECODE','PYTHONOPTIMIZE','PYTHONHASHSEED'}; "
                "actual=set(os.environ); "
                "(actual != expected or os.environ.get('PYTHONDONTWRITEBYTECODE') != '1' "
                "or os.environ.get('PYTHONOPTIMIZE') != '0' or os.environ.get('PYTHONHASHSEED') != '0') "
                "and (_ for _ in ()).throw(RuntimeError('child environment is not minimal')); "
                "runpy.run_path('scripts/rehearse-sanitized-lifecycle.py', run_name='__main__')"
            )
            direct = subprocess.run(
                ["python3", "-c", lifecycle_code],
                cwd=checkout,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(0, direct.returncode, direct.stderr)
            checked = subprocess.run(
                [
                    "python3", "-c",
                    lifecycle_code.replace(
                        "runpy.run_path(",
                        "sys.argv=['scripts/rehearse-sanitized-lifecycle.py','--check'];runpy.run_path(",
                        1,
                    ),
                ],
                cwd=checkout,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(direct.stdout, checked.stdout)
            report = json.loads(direct.stdout)
            self.assertEqual(
                "ao.lore.sanitized-lifecycle-rehearsal.v0.1",
                report["schema_version"],
            )
            self.assertEqual(
                [
                    ("document-retrieval-before-graph", "pass"),
                    ("optional-graph-retrieval", "pass"),
                    ("governed-selection", "pass"),
                    ("accepted-review", "pass"),
                    ("authorized-promotion", "pass"),
                    ("canonical-readback", "pass"),
                    ("origin-qualified-readback", "pass"),
                    ("deterministic-replay", "pass"),
                    ("query-missing", "pass"),
                    ("query-restricted", "pass"),
                    ("query-stale", "pass"),
                    ("query-documented-conflict", "pass"),
                    ("query-capacity-saturation", "pass"),
                ],
                [(item["scenario_id"], item["status"]) for item in report["scenario_statuses"]],
            )
            self.assertEqual(
                {
                    "answers": 1,
                    "authorizations": 1,
                    "candidates": 1,
                    "canonical_entries": 1,
                    "documents": 3,
                    "evidence_identities": 4,
                    "promotions": 1,
                    "proposals": 1,
                    "reviews": 1,
                    "search_hits": 2,
                },
                report["counts"],
            )
            self.assertEqual(["candidate-b311657a8b11966edc34802b"], report["candidate_ids"])
            self.assertEqual(["review-1c2003874e184682a2e776f7"], report["review_ids"])
            self.assertEqual(["entry-8598a9f60a8b30d066ed85ee5d985c4f"], report["canonical_entry_ids"])
            self.assertEqual(
                ["policy-emergency-lighting", "policy-expense-receipts", "policy-remote-schedules"],
                report["document_ids"],
            )
            self.assertEqual(
                [
                    "evidence-2725728e52cc5eafe85c6f5e",
                    "evidence-5e18c7cf9a12f240d4d06c6a",
                    "evidence-bcdb8b7c3093853dc9de9ed9",
                    "evidence-ff9da7a15ec3c6bb9e0cc2d3",
                ],
                report["evidence_ids"],
            )
            self.assertRegex(report["generation_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(0, report["network_calls"])
            self.assertEqual(0, report["provider_calls"])
            self.assertTrue(all(not value for value in report["external_authority"].values()))
            output = direct.stdout + checked.stdout
            for forbidden in ("http://", "https://", str(checkout)):
                self.assertNotIn(forbidden, output.casefold())
            status_lines = set(_checkout_status(checkout).splitlines())
            retained = checkout / ".ao-lore" / "sanitized-lifecycle-rehearsal" / "retained"
            self.assertTrue(retained.is_dir())
            inventory = {
                path.relative_to(checkout).as_posix()
                for path in (checkout / ".ao-lore").rglob("*")
                if path.is_file()
            }
            self.assertTrue(inventory)
            self.assertTrue(all(
                path.startswith(".ao-lore/sanitized-lifecycle-rehearsal/retained/")
                for path in inventory
            ))
            self.assertIn(
                ".ao-lore/sanitized-lifecycle-rehearsal/retained/.ao-lore-sanitized-lifecycle-owner.json",
                inventory,
            )
            self.assertIn(
                ".ao-lore/sanitized-lifecycle-rehearsal/retained/sanitized-lifecycle-report.json",
                inventory,
            )
            self.assertEqual({f"!! {path}" for path in inventory}, status_lines)
            runtime = checkout / ".ao-lore"
            campaign = runtime / "sanitized-lifecycle-rehearsal"
            known_empty = {
                "sanitized-lifecycle-rehearsal/retained/promotions/staging",
                "sanitized-lifecycle-rehearsal/retained/workspace-documents",
                "sanitized-lifecycle-rehearsal/retained/workspace-registry/workspaces/registry/staging",
                "sanitized-lifecycle-rehearsal/retained/workspace-registry/workspaces/state/workspace-sanitized-lifecycle/documents/staging",
                "sanitized-lifecycle-rehearsal/retained/workspace-registry/workspaces/state/workspace-sanitized-lifecycle/graph/staging",
            }
            def validate_owned_topology():
                if {path.name for path in runtime.iterdir()} != {"sanitized-lifecycle-rehearsal"}:
                    raise AssertionError("unexpected lifecycle root child")
                for path in runtime.rglob("*"):
                    relative = path.relative_to(runtime).as_posix()
                    if path.is_dir() and not any(path.iterdir()) and relative not in known_empty:
                        raise AssertionError("unexpected empty lifecycle directory")
                    if path.is_file() and not relative.startswith("sanitized-lifecycle-rehearsal/retained/"):
                        raise AssertionError("unexpected lifecycle file")
            validate_owned_topology()
            unexpected = runtime / "unexpected-empty"
            unexpected.mkdir()
            with self.assertRaises(AssertionError):
                validate_owned_topology()

    def test_public_export_authority_is_disabled(self):
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        authority = policy["authority"]
        self.assertEqual(AUTHORITY_FIELDS, set(authority))
        self.assertTrue(all(value is False for value in authority.values()))

    def test_public_export_policy_is_the_exact_neutral_allowlist(self):
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        self.assertEqual(set(policy["included_files"]), APPROVED_INCLUDED_FILES)
        self.assertEqual(set(policy["included_prefixes"]), APPROVED_INCLUDED_PREFIXES)
        self.assertEqual(set(policy["excluded_prefixes"]), APPROVED_EXCLUDED_PREFIXES)
        self.assertNotIn("docs/superpowers/", policy["included_prefixes"])

    def test_included_schemas_do_not_embed_a_named_local_locator(self):
        named_local_locator = (
            "https" + "://" + "ao" + "." + "local"
        ).encode()
        for path in sorted((ROOT / "schemas").glob("**/*.json")):
            with self.subTest(path=path.name):
                self.assertNotIn(named_local_locator, path.read_bytes())

    def test_roadmap_is_non_premature_and_does_not_require_recursive_rewrites(self):
        roadmap = (ROOT / "ROADMAP-ADDENDUM.md").read_text(encoding="utf-8")
        month = " ".join(roadmap.split(
            "## Month 6 — Clean reusable baseline and public-release readiness",
            1,
        )[1].split())
        self.assertIn("Source review remains in progress.", month)
        self.assertIn("bounded external evidence packet", month)
        self.assertNotIn("Local sanitized readiness is complete", month)
        self.assertNotIn("post-readback roadmap commit", month)

if __name__ == "__main__":
    unittest.main()
