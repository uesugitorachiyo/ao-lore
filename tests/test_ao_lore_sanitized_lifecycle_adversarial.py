import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ao_lore.sanitized_lifecycle import (
    SanitizedLifecycleError,
    run_sanitized_lifecycle,
)


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_GENERATOR = (
    REPOSITORY
    / "tests"
    / "fixtures"
    / "ao_lore"
    / "sanitized_lifecycle"
    / "generate.py"
)
SOURCE_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=REPOSITORY,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
NOW = "2026-08-14T12:00:00Z"
OWNER_BYTES = (
    b'{"owner":"ao-lore-sanitized-lifecycle-fixture",'
    b'"schema_version":"v0.1"}\n'
)


def _fixture_module():
    specification = importlib.util.spec_from_file_location(
        "sanitized_lifecycle_adversarial_fixture_generator",
        FIXTURE_GENERATOR,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("sanitized lifecycle fixture generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _deny_capability(*_args, **_kwargs):
    raise AssertionError("external capability attempted")


@contextmanager
def _completed_lifecycle():
    allowed_parent = REPOSITORY / "working" / "candidates"
    allowed_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
        root = Path(temporary)
        (root / ".ao-lore-sanitized-lifecycle-owner.json").write_bytes(
            OWNER_BYTES
        )
        if _fixture_module().main(["--out", str(root / "fixture")]) != 0:
            raise RuntimeError("sanitized lifecycle fixture generation failed")
        report = run_sanitized_lifecycle(
            root,
            source_head=SOURCE_HEAD,
            now=NOW,
            deny_network=_deny_capability,
            deny_provider=_deny_capability,
        )
        yield root, report


def _retained_artifacts(root, report):
    candidate_root = root / "candidate" / report["candidate_ids"][0]
    generation = next((root / "brain" / "generations").iterdir())
    return {
        "candidate": candidate_root / "candidate.json",
        "provenance": candidate_root / "provenance.json",
        "review": next((candidate_root / "reviews").iterdir()),
        "proposal": next((root / "promotions" / "proposals").iterdir()),
        "authorization": (
            root / "promotions" / "authorization-sanitized-lifecycle.json"
        ),
        "canonical": next((generation / "entries").iterdir()),
        "transaction": next((root / "promotions" / "transactions").iterdir()),
        "audit": next((root / "promotions" / "audit").iterdir()),
        "consumed": next((root / "promotions" / "consumed").iterdir()),
        "retained": next((root / "promotions" / "retained").iterdir()),
        "recovery": next((root / "promotions" / "recovery").iterdir()),
        "report": root / "sanitized-lifecycle-report.json",
    }


def _replay(root):
    return run_sanitized_lifecycle(
        root,
        source_head=SOURCE_HEAD,
        now=NOW,
        deny_network=_deny_capability,
        deny_provider=_deny_capability,
    )


def _file_inventory(root):
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


class SanitizedLifecycleAdversarialTests(unittest.TestCase):
    def test_replay_rejects_hardlinked_retained_candidate(self):
        with _completed_lifecycle() as (root, report):
            candidate = (
                root
                / "candidate"
                / report["candidate_ids"][0]
                / "candidate.json"
            )
            os.link(candidate, root / "candidate-hardlink-copy.json")

            with self.assertRaises(SanitizedLifecycleError):
                _replay(root)

    def test_replay_rejects_missing_artifact_in_every_retained_class(self):
        artifact_names = (
            "candidate",
            "provenance",
            "review",
            "proposal",
            "authorization",
            "canonical",
            "transaction",
            "audit",
            "consumed",
            "retained",
            "recovery",
            "report",
        )
        for artifact_name in artifact_names:
            with self.subTest(artifact=artifact_name), _completed_lifecycle() as (
                root,
                report,
            ):
                _retained_artifacts(root, report)[artifact_name].unlink()
                before = _file_inventory(root)
                with self.assertRaises(SanitizedLifecycleError) as caught:
                    _replay(root)
                self.assertEqual(before, _file_inventory(root))
                if artifact_name == "report":
                    self.assertEqual(
                        "retained lifecycle report is missing", str(caught.exception)
                    )

    def test_replay_rejects_duplicate_key_in_every_retained_class(self):
        artifact_names = (
            "candidate",
            "provenance",
            "review",
            "proposal",
            "authorization",
            "canonical",
            "transaction",
            "audit",
            "consumed",
            "retained",
            "recovery",
            "report",
        )
        for artifact_name in artifact_names:
            with self.subTest(artifact=artifact_name), _completed_lifecycle() as (
                root,
                report,
            ):
                artifact = _retained_artifacts(root, report)[artifact_name]
                body = artifact.read_bytes()
                artifact.write_bytes(b'{"schema_version":null,' + body[1:])
                with self.assertRaises(SanitizedLifecycleError):
                    _replay(root)

    def test_replay_rejects_foreign_candidate_brain_and_promotions_state(self):
        for state_root in ("candidate", "brain", "promotions"):
            with self.subTest(state_root=state_root), _completed_lifecycle() as (
                root,
                _report,
            ):
                foreign = root / state_root / "foreign-state.json"
                foreign.write_text('{"foreign":true}\n', encoding="utf-8")
                before = _file_inventory(root)
                with self.assertRaises(SanitizedLifecycleError) as caught:
                    _replay(root)
                self.assertEqual(
                    f"retained {state_root} inventory differs",
                    str(caught.exception),
                )
                self.assertEqual(before, _file_inventory(root))

    def test_replay_rejects_foreign_nested_candidate_entries(self):
        for entry_kind in ("regular", "symlink", "directory"):
            with self.subTest(entry_kind=entry_kind), _completed_lifecycle() as (
                root,
                report,
            ):
                candidate_directory = (
                    root / "candidate" / report["candidate_ids"][0]
                )
                foreign = candidate_directory / "extra.json"
                if entry_kind == "regular":
                    foreign.write_text('{"foreign":true}\n', encoding="utf-8")
                elif entry_kind == "symlink":
                    foreign.symlink_to("candidate.json")
                else:
                    foreign.mkdir()
                with self.assertRaisesRegex(
                    SanitizedLifecycleError,
                    "^retained candidate entry inventory differs$",
                ):
                    _replay(root)

    def test_replay_rejects_foreign_or_hardlinked_review_entries(self):
        for entry_kind in ("regular", "symlink", "directory", "hardlink"):
            with self.subTest(entry_kind=entry_kind), _completed_lifecycle() as (
                root,
                report,
            ):
                reviews = (
                    root
                    / "candidate"
                    / report["candidate_ids"][0]
                    / "reviews"
                )
                review = next(reviews.iterdir())
                foreign = reviews / "extra.json"
                if entry_kind == "regular":
                    foreign.write_text('{"foreign":true}\n', encoding="utf-8")
                elif entry_kind == "symlink":
                    foreign.symlink_to(review.name)
                elif entry_kind == "directory":
                    foreign.mkdir()
                else:
                    os.link(review, foreign)
                with self.assertRaisesRegex(
                    SanitizedLifecycleError,
                    "^retained review inventory differs$",
                ):
                    _replay(root)

    def test_replay_rejects_review_directory_rebound_during_inventory(self):
        with _completed_lifecycle() as (root, report):
            reviews = (
                root
                / "candidate"
                / report["candidate_ids"][0]
                / "reviews"
            )
            review_identity = (os.lstat(reviews).st_dev, os.lstat(reviews).st_ino)
            moved = reviews.with_name("reviews-displaced")
            original_scandir = os.scandir
            swapped = False

            def swap_during_scan(directory):
                nonlocal swapped
                if (
                    not swapped
                    and isinstance(directory, int)
                    and (os.fstat(directory).st_dev, os.fstat(directory).st_ino)
                    == review_identity
                ):
                    swapped = True
                    reviews.rename(moved)
                    shutil.copytree(moved, reviews)
                return original_scandir(directory)

            with patch(
                "ao_lore.sanitized_lifecycle.os.scandir",
                side_effect=swap_during_scan,
            ):
                with self.assertRaisesRegex(
                    SanitizedLifecycleError,
                    "^retained review inventory changed$",
                ):
                    _replay(root)
            self.assertTrue(swapped)

    def test_replay_rejects_candidate_root_rebound_during_inventory(self):
        with _completed_lifecycle() as (root, _report):
            candidate_root = root / "candidate"
            candidate_identity = (
                os.lstat(candidate_root).st_dev,
                os.lstat(candidate_root).st_ino,
            )
            moved = root / "candidate-displaced"
            original_scandir = os.scandir
            swapped = False

            def swap_during_scan(directory):
                nonlocal swapped
                if (
                    not swapped
                    and isinstance(directory, int)
                    and (os.fstat(directory).st_dev, os.fstat(directory).st_ino)
                    == candidate_identity
                ):
                    swapped = True
                    candidate_root.rename(moved)
                    shutil.copytree(moved, candidate_root)
                return original_scandir(directory)

            with patch(
                "ao_lore.sanitized_lifecycle.os.scandir",
                side_effect=swap_during_scan,
            ):
                with self.assertRaisesRegex(
                    SanitizedLifecycleError,
                    "retained candidate inventory changed",
                ):
                    _replay(root)
            self.assertTrue(swapped)

    def test_replay_rejects_caller_source_head_drift(self):
        with _completed_lifecycle() as (root, _report):
            drifted_head = "0" * 40 if SOURCE_HEAD != "0" * 40 else "1" * 40
            with self.assertRaises(SanitizedLifecycleError):
                run_sanitized_lifecycle(
                    root,
                    source_head=drifted_head,
                    now=NOW,
                    deny_network=_deny_capability,
                    deny_provider=_deny_capability,
                )

    def test_replay_rejects_fixture_workspace_graph_and_selection_drift(self):
        for drift in (
            "fixture-source",
            "fixture-evidence",
            "workspace-registry",
            "workspace-document",
            "published-graph",
            "selection-result",
        ):
            with self.subTest(drift=drift), _completed_lifecycle() as (
                root,
                _report,
            ):
                if drift in {"fixture-source", "fixture-evidence"}:
                    bundle_path = root / "fixture" / "fixture-bundle.json"
                    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                    if drift == "fixture-source":
                        bundle["claims"][0]["source_digest"] = (
                            "sha256:" + "0" * 64
                        )
                    else:
                        bundle["claims"][0]["excerpt"] = (
                            "Drifted synthetic evidence."
                        )
                    bundle_path.write_text(
                        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                elif drift == "workspace-registry":
                    registry_manifest = sorted(
                        (
                            root
                            / "workspace-registry"
                            / "workspaces"
                            / "registry"
                            / "generations"
                        ).glob("*/manifest.json")
                    )[-1]
                    registry_manifest.write_bytes(b"{}\n")
                elif drift == "workspace-document":
                    document_manifest = next(
                        (
                            root
                            / "workspace-registry"
                            / "workspaces"
                            / "state"
                            / "workspace-sanitized-lifecycle"
                            / "documents"
                            / "generations"
                        ).glob("*/manifest.json")
                    )
                    document_manifest.write_bytes(b"{}\n")
                elif drift == "published-graph":
                    graph_generation = next(
                        (
                            root
                            / "workspace-registry"
                            / "workspaces"
                            / "state"
                            / "workspace-sanitized-lifecycle"
                            / "graph"
                            / "generations"
                        ).iterdir()
                    )
                    graph_generation.write_bytes(b"{}\n")
                else:
                    selection_result = next(
                        (
                            root
                            / "workspace-registry"
                            / "workspaces"
                            / "state"
                            / "workspace-sanitized-lifecycle"
                            / "selection"
                            / "transactions"
                        ).iterdir()
                    )
                    selection_result.write_bytes(b"{}\n")
                before = _file_inventory(root)
                with self.assertRaises(SanitizedLifecycleError):
                    _replay(root)
                self.assertEqual(before, _file_inventory(root))


if __name__ == "__main__":
    unittest.main()
