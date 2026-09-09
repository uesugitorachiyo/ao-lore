import importlib.util
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import parse_strict_json
from ao_lore.sanitized_lifecycle import (
    SanitizedLifecycleError,
    _run_sanitized_lifecycle_in_rehearsal_run,
    run_sanitized_lifecycle,
)
from ao_lore.sanitized_lifecycle_contracts import (
    validate_sanitized_lifecycle_rehearsal,
)
from ao_lore.knowledge import (
    _KnowledgeDependencies,
    answer_knowledge,
    knowledge_status,
    search_knowledge,
)
from ao_lore.knowledge_contracts import origin_for_anchors, origin_identity


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
OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
OWNER_BYTES = b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
NETWORK_PROBE = "ao-lore-network-denial-guard"
PROVIDER_PROBE = "ao-lore-provider-denial-guard"


def _deny_capability(*_args, **_kwargs):
    raise RuntimeError("capability denied")


def _fixture_module():
    specification = importlib.util.spec_from_file_location(
        "sanitized_lifecycle_fixture_generator", FIXTURE_GENERATOR,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("sanitized lifecycle fixture generator is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def _digest(value: object) -> str:
    body = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _tracked_inventory() -> dict[str, bytes]:
    names = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    return {
        name.decode("utf-8"): (REPOSITORY / name.decode("utf-8")).read_bytes()
        for name in names
        if name
    }


class SanitizedLifecycleTests(unittest.TestCase):
    def _generate(self, root: Path) -> None:
        (root / OWNER_NAME).write_bytes(OWNER_BYTES)
        self.assertEqual(0, _fixture_module().main(["--out", str(root / "fixture")]))

    def test_private_fixed_campaign_entry_uses_working_candidates_only(self):
        import ao_lore.sanitized_lifecycle as lifecycle_module

        self.assertNotIn(
            "_run_sanitized_lifecycle_in_rehearsal_run",
            lifecycle_module.__all__,
        )
        campaign = REPOSITORY / ".ao-lore" / "sanitized-lifecycle-rehearsal"
        campaign.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=campaign) as temporary:
            run = Path(temporary)
            self._generate(run)
            report = _run_sanitized_lifecycle_in_rehearsal_run(
                run,
                source_head=SOURCE_HEAD,
                now=NOW,
                deny_network=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("network attempted")
                ),
                deny_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("provider attempted")
                ),
            )

            self.assertEqual(1, report["counts"]["candidates"])
            self.assertTrue((run / "working" / "candidates").is_dir())
            self.assertFalse((run / "candidate").exists())
            replay = _run_sanitized_lifecycle_in_rehearsal_run(
                run,
                source_head=SOURCE_HEAD,
                now=NOW,
                deny_network=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("network attempted")
                ),
                deny_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("provider attempted")
                ),
            )
            self.assertEqual(report, replay)

        with tempfile.TemporaryDirectory(dir=REPOSITORY / ".ao-lore") as foreign:
            foreign_run = Path(foreign)
            self._generate(foreign_run)
            with self.assertRaisesRegex(SanitizedLifecycleError, "fixed rehearsal root"):
                _run_sanitized_lifecycle_in_rehearsal_run(
                    foreign_run,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=_deny_capability,
                    deny_provider=_deny_capability,
                )

    def test_private_fixed_campaign_never_falls_back_to_path_candidate_apis(self):
        campaign = REPOSITORY / ".ao-lore" / "sanitized-lifecycle-rehearsal"
        campaign.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=campaign) as temporary:
            run = Path(temporary)
            self._generate(run)
            rejected = AssertionError("path candidate API used")
            with (
                patch("ao_lore.evidence_selection.persist_candidate", side_effect=rejected),
                patch("ao_lore.evidence_selection.load_verified_candidate", side_effect=rejected),
                patch("ao_lore.promotion._load_candidate", side_effect=rejected),
                patch(
                    "ao_lore.sanitized_lifecycle._append_review_with_dependencies",
                    side_effect=rejected,
                ),
                patch("ao_lore.sanitized_lifecycle.inspect_candidate", side_effect=rejected),
            ):
                report = _run_sanitized_lifecycle_in_rehearsal_run(
                    run,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=_deny_capability,
                    deny_provider=_deny_capability,
                )
                replay = _run_sanitized_lifecycle_in_rehearsal_run(
                    run,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=_deny_capability,
                    deny_provider=_deny_capability,
                )

            self.assertEqual(1, report["counts"]["canonical_entries"])
            self.assertEqual(report, replay)

    def test_complete_governed_lifecycle_is_exactly_replayable(self):
        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        network_calls = 0
        provider_calls = 0

        def deny_network(*_args, **_kwargs):
            nonlocal network_calls
            network_calls += 1
            raise AssertionError("network attempted")

        def deny_provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("provider attempted")

        tracked_before = _tracked_inventory()
        with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
            root = Path(temporary)
            (root / OWNER_NAME).write_bytes(OWNER_BYTES)
            self.assertEqual(
                0,
                _fixture_module().main(["--out", str(root / "fixture")]),
            )

            first = run_sanitized_lifecycle(
                root,
                source_head=SOURCE_HEAD,
                now=NOW,
                deny_network=deny_network,
                deny_provider=deny_provider,
            )
            validated = validate_sanitized_lifecycle_rehearsal(first)
            after_first = _inventory(root)
            second = run_sanitized_lifecycle(
                root,
                source_head=SOURCE_HEAD,
                now=NOW,
                deny_network=deny_network,
                deny_provider=deny_provider,
            )

            self.assertEqual(first, validated)
            self.assertEqual(first, second)
            self.assertEqual(after_first, _inventory(root))
            self.assertEqual(3, first["counts"]["documents"])
            self.assertGreaterEqual(first["counts"]["evidence_identities"], 3)
            self.assertEqual(1, first["counts"]["candidates"])
            self.assertEqual(1, first["counts"]["reviews"])
            self.assertEqual(1, first["counts"]["proposals"])
            self.assertEqual(1, first["counts"]["authorizations"])
            self.assertEqual(1, first["counts"]["promotions"])
            self.assertEqual(1, first["counts"]["canonical_entries"])
            self.assertGreaterEqual(first["counts"]["search_hits"], 1)
            self.assertEqual(1, first["counts"]["answers"])
            self.assertEqual((0, 0), (network_calls, provider_calls))
            self.assertNotIn(str(root), json.dumps(first, sort_keys=True))
            self.assertTrue(
                all(value is False for value in first["external_authority"].values())
            )
            scenario_ids = [item["scenario_id"] for item in first["scenario_statuses"]]
            self.assertIn("origin-qualified-readback", scenario_ids)
            self.assertEqual(
                {
                    "query-missing", "query-restricted", "query-stale",
                    "query-documented-conflict", "query-capacity-saturation",
                },
                set(scenario_ids) & {
                    "query-missing", "query-restricted", "query-stale",
                    "query-documented-conflict", "query-capacity-saturation",
                },
            )

            dependencies = _KnowledgeDependencies(
                root / "brain", root / "promotions" / "promotion.lock",
            )
            status = knowledge_status(_dependencies=dependencies)
            search = search_knowledge(
                "expense receipts remote schedules", _dependencies=dependencies,
            )
            answer = answer_knowledge(
                "When are expense receipts submitted and remote schedules approved?",
                _dependencies=dependencies,
            )
            self.assertEqual("ao.lore.knowledge-status-readback.v0.2", status["schema_version"])
            self.assertEqual("ao.lore.knowledge-search-readback.v0.2", search["schema_version"])
            self.assertEqual("ao.lore.knowledge-answer-readback.v0.2", answer["schema_version"])
            self.assertEqual(first["search_digest"], _digest(search))
            self.assertEqual(first["answer_digest"], _digest(answer))

            entry_path = next((root / "brain" / "generations").glob("*/entries/*.json"))
            entry = parse_strict_json(entry_path.read_bytes(), "canonical entry")
            citations = {item["citation_id"]: item for item in entry["citations"]}
            expected_by_claim = {}
            for claim in entry["claims"]:
                citation = citations[claim["citation_id"]]
                expected_by_claim[claim["claim_id"]] = {
                    "citation_id": citation["citation_id"],
                    "origin_identity": origin_identity(
                        origin_for_anchors(entry["evidence_origins"], claim["source_block_ids"])
                    ),
                    "source_ref": (
                        f"canonical:{entry['canonical_entry_id']}#{citation['citation_id']}"
                    ),
                }
            self.assertEqual(set(expected_by_claim), {item["claim_id"] for item in search["hits"]})
            for hit in search["hits"]:
                expected = expected_by_claim[hit["claim_id"]]
                self.assertEqual(expected["citation_id"], hit["citation_id"])
                self.assertEqual(expected["origin_identity"], hit["origin_identity"])
                self.assertEqual(expected["source_ref"], hit["source_ref"])
            search_by_claim = {item["claim_id"]: item for item in search["hits"]}
            self.assertTrue(answer["claim_ids"])
            self.assertEqual(len(answer["claim_ids"]), len(answer["evidence_ids"]))
            for claim_id, evidence_id in zip(
                answer["claim_ids"], answer["evidence_ids"], strict=True,
            ):
                self.assertIn(claim_id, search_by_claim)
                self.assertEqual(search_by_claim[claim_id]["evidence_id"], evidence_id)
            expected_answer_citations = [
                {
                    "source_ref": search_by_claim[claim_id]["source_ref"],
                    "origin_identity": search_by_claim[claim_id]["origin_identity"],
                }
                for claim_id in answer["claim_ids"]
            ]
            self.assertEqual(
                {
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in expected_answer_citations
                },
                {
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in answer["citations"]
                },
            )
            fixture = parse_strict_json(
                (root / "fixture" / "fixture-bundle.json").read_bytes(),
                "fixture bundle",
            )
            unrelated = next(
                item["source_id"] for item in fixture["claims"]
                if item["document_id"] == fixture["unrelated_document_id"]
            )
            self.assertNotIn(unrelated, json.dumps((search, answer), sort_keys=True))

        with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
            fresh_root = Path(temporary)
            (fresh_root / OWNER_NAME).write_bytes(OWNER_BYTES)
            self.assertEqual(
                0,
                _fixture_module().main(["--out", str(fresh_root / "fixture")]),
            )
            fresh = run_sanitized_lifecycle(
                fresh_root,
                source_head=SOURCE_HEAD,
                now=NOW,
                deny_network=deny_network,
                deny_provider=deny_provider,
            )
            self.assertEqual(first, fresh)

        self.assertEqual(tracked_before, _tracked_inventory())

    def test_denial_guards_are_armed_without_probe_on_fresh_and_replay(self):
        import ao_lore.sanitized_lifecycle as lifecycle_module

        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
            root = Path(temporary)
            self._generate(root)
            events: list[tuple[str, str | None]] = []
            actual_validate = lifecycle_module._validate_root

            def deny_network(token):
                events.append(("network", token))
                raise RuntimeError("network denied")

            def deny_provider(token):
                events.append(("provider", token))
                raise RuntimeError("provider denied")

            def observed_validate(selected):
                events.append(("filesystem", None))
                return actual_validate(selected)

            with patch.object(lifecycle_module, "_validate_root", observed_validate):
                first = run_sanitized_lifecycle(
                    root,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=deny_network,
                    deny_provider=deny_provider,
                )
            self.assertEqual(("filesystem", None), events[0])
            self.assertNotIn(("network", NETWORK_PROBE), events)
            self.assertNotIn(("provider", PROVIDER_PROBE), events)

            events.clear()
            with patch.object(lifecycle_module, "_validate_root", observed_validate):
                replay = run_sanitized_lifecycle(
                    root,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=deny_network,
                    deny_provider=deny_provider,
                )
            self.assertEqual(first, replay)
            self.assertEqual(("filesystem", None), events[0])
            self.assertNotIn(("network", NETWORK_PROBE), events)
            self.assertNotIn(("provider", PROVIDER_PROBE), events)

    def test_context_capability_entries_invoke_hooks_before_mutation(self):
        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        import ao_lore.sanitized_lifecycle as lifecycle_module

        class GuardDenied(Exception):
            pass

        attempts = (
            (
                "network",
                lifecycle_module._network_entry,
                "network",
                NETWORK_PROBE,
            ),
            (
                "provider",
                lifecycle_module._provider_gateway,
                "provider",
                PROVIDER_PROBE,
            ),
        )
        for name, attempt, expected_kind, expected_token in attempts:
            with self.subTest(attempt=name), tempfile.TemporaryDirectory(
                dir=allowed_parent
            ) as temporary:
                root = Path(temporary)
                self._generate(root)
                before = _inventory(root)
                probes: list[tuple[str, str]] = []

                def deny_network(token):
                    probes.append(("network", token))
                    raise GuardDenied("network denied")

                def deny_provider(token):
                    probes.append(("provider", token))
                    raise GuardDenied("provider denied")

                with patch.object(
                    lifecycle_module, "_validate_root", side_effect=lambda _root: attempt()
                ), self.assertRaises(GuardDenied):
                    run_sanitized_lifecycle(
                        root, source_head=SOURCE_HEAD, now=NOW,
                        deny_network=deny_network, deny_provider=deny_provider,
                    )
                self.assertEqual(before, _inventory(root))
                self.assertEqual([(expected_kind, expected_token)], probes)

    def test_returning_guard_rejects_forced_attempt_without_mutation(self):
        import ao_lore.sanitized_lifecycle as lifecycle_module

        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
            root = Path(temporary)
            self._generate(root)
            before = _inventory(root)
            attempt_started = False

            def attempt_network(_root):
                nonlocal attempt_started
                attempt_started = True
                lifecycle_module._network_entry()

            with patch.object(
                lifecycle_module, "_validate_root", side_effect=attempt_network
            ), self.assertRaisesRegex(
                SanitizedLifecycleError, "^lifecycle denial guard is invalid$"
            ) as rejected:
                run_sanitized_lifecycle(
                    root, source_head=SOURCE_HEAD, now=NOW,
                    deny_network=lambda _token: None,
                    deny_provider=_deny_capability,
                )
            self.assertTrue(attempt_started)
            self.assertNotIn(str(root), str(rejected.exception))
            self.assertEqual(before, _inventory(root))

        with tempfile.TemporaryDirectory(dir=allowed_parent) as temporary:
            root = Path(temporary)
            self._generate(root)
            before = _inventory(root)
            attempt_started = False

            def attempt_provider(_root):
                nonlocal attempt_started
                attempt_started = True
                lifecycle_module._provider_gateway()

            with patch.object(
                lifecycle_module, "_validate_root", side_effect=attempt_provider
            ), self.assertRaisesRegex(
                SanitizedLifecycleError, "^lifecycle denial guard is invalid$"
            ) as rejected:
                run_sanitized_lifecycle(
                    root, source_head=SOURCE_HEAD, now=NOW,
                    deny_network=_deny_capability,
                    deny_provider=lambda _token: None,
                )
            self.assertTrue(attempt_started)
            self.assertNotIn(str(root), str(rejected.exception))
            self.assertEqual(before, _inventory(root))

        for missing in ("network", "provider"):
            with self.subTest(noncallable=missing), tempfile.TemporaryDirectory(
                dir=allowed_parent
            ) as temporary:
                root = Path(temporary)
                self._generate(root)
                before = _inventory(root)
                with self.assertRaisesRegex(
                    SanitizedLifecycleError, "^lifecycle denial guard is invalid$"
                ):
                    run_sanitized_lifecycle(
                        root, source_head=SOURCE_HEAD, now=NOW,
                        deny_network=None if missing == "network" else _deny_capability,
                        deny_provider=None if missing == "provider" else _deny_capability,
                    )
                self.assertEqual(before, _inventory(root))

    def test_nested_lifecycle_restores_outer_then_absent_context(self):
        import ao_lore.sanitized_lifecycle as lifecycle_module

        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        events: list[tuple[str, str]] = []

        class InnerDenied(Exception):
            pass

        class OuterDenied(Exception):
            pass

        with tempfile.TemporaryDirectory(dir=allowed_parent) as outer_temporary, \
                tempfile.TemporaryDirectory(dir=allowed_parent) as inner_temporary:
            outer_root = Path(outer_temporary)
            inner_root = Path(inner_temporary)
            self._generate(outer_root)
            self._generate(inner_root)
            outer_before = _inventory(outer_root)
            inner_before = _inventory(inner_root)
            actual_validate = lifecycle_module._validate_root

            def outer_network(token):
                events.append(("outer-network", token))
                raise OuterDenied("outer denied")

            def inner_provider(token):
                events.append(("inner-provider", token))
                raise InnerDenied("inner denied")

            def observed_validate(root):
                if root == inner_root:
                    lifecycle_module._provider_gateway()
                with self.assertRaises(InnerDenied):
                    run_sanitized_lifecycle(
                        inner_root, source_head=SOURCE_HEAD, now=NOW,
                        deny_network=_deny_capability,
                        deny_provider=inner_provider,
                    )
                lifecycle_module._network_entry()
                return actual_validate(root)

            with patch.object(lifecycle_module, "_validate_root", observed_validate), \
                    self.assertRaises(OuterDenied):
                run_sanitized_lifecycle(
                    outer_root, source_head=SOURCE_HEAD, now=NOW,
                    deny_network=outer_network, deny_provider=_deny_capability,
                )

            self.assertEqual(outer_before, _inventory(outer_root))
            self.assertEqual(inner_before, _inventory(inner_root))
            self.assertEqual(
                [
                    ("inner-provider", PROVIDER_PROBE),
                    ("outer-network", NETWORK_PROBE),
                ],
                events,
            )
        for entry in (
            lifecycle_module._network_entry,
            lifecycle_module._provider_gateway,
        ):
            with self.assertRaisesRegex(
                SanitizedLifecycleError,
                "^lifecycle offline capability is unavailable$",
            ):
                entry()

    def test_context_capabilities_are_isolated_across_concurrent_threads(self):
        import ao_lore.sanitized_lifecycle as lifecycle_module

        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        failures: list[BaseException] = []
        actual_validate = lifecycle_module._validate_root
        first_hook_calls: list[str] = []
        second_hook_calls: list[str] = []

        class SecondDenied(Exception):
            pass

        with tempfile.TemporaryDirectory(dir=allowed_parent) as first_temporary, \
                tempfile.TemporaryDirectory(dir=allowed_parent) as second_temporary:
            first_root = Path(first_temporary)
            second_root = Path(second_temporary)
            self._generate(first_root)
            self._generate(second_root)

            def observed_validate(root):
                if root == first_root:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("serialization release timed out")
                else:
                    second_entered.set()
                    lifecycle_module._provider_gateway()
                return actual_validate(root)

            def first_network(token):
                first_hook_calls.append(token)
                raise AssertionError("first network denied")

            def second_provider(token):
                second_hook_calls.append(token)
                raise SecondDenied("second provider denied")

            def execute_first():
                try:
                    run_sanitized_lifecycle(
                        first_root, source_head=SOURCE_HEAD, now=NOW,
                        deny_network=first_network,
                        deny_provider=_deny_capability,
                    )
                except BaseException as exc:
                    failures.append(exc)

            def execute_second():
                try:
                    run_sanitized_lifecycle(
                        second_root, source_head=SOURCE_HEAD, now=NOW,
                        deny_network=_deny_capability,
                        deny_provider=second_provider,
                    )
                except SecondDenied:
                    pass
                except BaseException as exc:
                    failures.append(exc)

            with patch.object(lifecycle_module, "_validate_root", observed_validate):
                first = threading.Thread(target=execute_first)
                second = threading.Thread(target=execute_second)
                first.start()
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(
                    SanitizedLifecycleError,
                    "^lifecycle offline capability is unavailable$",
                ):
                    lifecycle_module._network_entry()
                second.start()
                isolated = second_entered.wait(5)
                release.set()
                first.join(10)
                second.join(10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual([], failures)
            self.assertTrue(isolated)
            self.assertEqual([], first_hook_calls)
            self.assertEqual([PROVIDER_PROBE], second_hook_calls)

    def test_brain_readme_existing_and_dangling_symlinks_never_escape(self):
        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        for target_exists in (True, False):
            with self.subTest(target_exists=target_exists), tempfile.TemporaryDirectory(
                dir=allowed_parent
            ) as temporary, tempfile.TemporaryDirectory(dir=allowed_parent) as external:
                root = Path(temporary)
                outside = Path(external) / "outside.txt"
                if target_exists:
                    outside.write_bytes(b"# Disposable synthetic proof brain\n")
                self._generate(root)
                (root / "brain").mkdir()
                (root / "brain" / "README.md").symlink_to(outside)
                before = outside.read_bytes() if target_exists else None
                with self.assertRaises(SanitizedLifecycleError):
                    run_sanitized_lifecycle(
                        root,
                        source_head=SOURCE_HEAD,
                        now=NOW,
                        deny_network=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("network attempted")
                        ),
                        deny_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("provider attempted")
                        ),
                    )
                self.assertEqual(target_exists, outside.exists())
                if target_exists:
                    self.assertEqual(before, outside.read_bytes())

    def test_replay_rejects_exact_artifact_drift_and_duplicate_keys(self):
        allowed_parent = REPOSITORY / "working" / "candidates"
        allowed_parent.mkdir(parents=True, exist_ok=True)
        for case in (
            "candidate-drift", "provenance-drift", "review-drift",
            "proposal-drift", "duplicate-entry", "duplicate-report",
            "transaction-drift", "audit-absent", "consumed-duplicate",
            "retained-drift", "recovery-drift",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                dir=allowed_parent
            ) as temporary:
                root = Path(temporary)
                self._generate(root)
                report = run_sanitized_lifecycle(
                    root,
                    source_head=SOURCE_HEAD,
                    now=NOW,
                    deny_network=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("network attempted")
                    ),
                    deny_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("provider attempted")
                    ),
                )
                if case == "candidate-drift":
                    candidate = root / "candidate" / report["candidate_ids"][0] / "candidate.json"
                    candidate.write_bytes(b"{}\n")
                elif case == "provenance-drift":
                    provenance = (
                        root / "candidate" / report["candidate_ids"][0]
                        / "provenance.json"
                    )
                    provenance.write_bytes(b"{}\n")
                elif case == "review-drift":
                    review = next(
                        (root / "candidate" / report["candidate_ids"][0] / "reviews")
                        .iterdir()
                    )
                    review.write_bytes(b"{}\n")
                elif case == "proposal-drift":
                    proposal = next((root / "promotions" / "proposals").iterdir())
                    proposal.write_bytes(b"{}\n")
                elif case == "transaction-drift":
                    transaction = next((root / "promotions" / "transactions").iterdir())
                    transaction.write_bytes(b"{}\n")
                elif case == "audit-absent":
                    audit = next((root / "promotions" / "audit").iterdir())
                    audit.unlink()
                elif case == "consumed-duplicate":
                    consumed = next(
                        item for item in (root / "promotions" / "consumed").iterdir()
                        if item.name.endswith("-committed.json")
                    )
                    body = consumed.read_text(encoding="utf-8")
                    consumed.write_text(
                        body.replace("{", '{"state":"committed",', 1),
                        encoding="utf-8",
                    )
                elif case == "retained-drift":
                    retained = next((root / "promotions" / "retained").iterdir())
                    retained.write_bytes(b"{}\n")
                elif case == "recovery-drift":
                    recovery = next((root / "promotions" / "recovery").iterdir())
                    recovery.write_bytes(b"{}\n")
                elif case == "duplicate-entry":
                    entry = next((root / "brain" / "generations").glob("*/entries/*.json"))
                    body = entry.read_text(encoding="utf-8")
                    entry.write_text(
                        body.replace(
                            "{\n",
                            '{\n  "canonical_entry_id": "'
                            + report["canonical_entry_ids"][0]
                            + '",\n',
                            1,
                        ),
                        encoding="utf-8",
                    )
                else:
                    retained = root / "sanitized-lifecycle-report.json"
                    body = retained.read_text(encoding="utf-8")
                    retained.write_text(
                        body.replace(
                            "{",
                            '{"schema_version":"ao.lore.sanitized-lifecycle-rehearsal.v0.1",',
                            1,
                        ),
                        encoding="utf-8",
                    )
                with self.assertRaises(SanitizedLifecycleError):
                    run_sanitized_lifecycle(
                        root,
                        source_head=SOURCE_HEAD,
                        now=NOW,
                        deny_network=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("network attempted")
                        ),
                        deny_provider=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("provider attempted")
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
