import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from ao_lore.evidence_acquisition import (
    AcquisitionDependencies,
    AcquisitionError,
    AcquisitionLimits,
    AcquisitionSpec,
    HTTPResponse,
    acquire_official_evidence,
)
from ao_lore.evidence_freshness import (
    FreshnessDependencies,
    FreshnessStorageError,
    recover_freshness_transactions,
    refresh_official_evidence,
)
from tests.neutral_fixture_utils import neutral_https_locator, neutral_public_https_locator
from tests.test_ao_lore_evidence_freshness import SOURCE_LOCATOR, bind, graph, policy


REDIRECT_LOCATOR = neutral_public_https_locator(("source", "redirect"), ("guide",))
REDIRECT_HOST = urlsplit(REDIRECT_LOCATOR).hostname
SOURCE_TWO_LOCATOR = neutral_public_https_locator(("source",), ("guide", "two"))
SOURCE_A_LOCATOR = neutral_public_https_locator(("source",), ("a", "guide"))
SOURCE_Z_LOCATOR = neutral_public_https_locator(("source",), ("z", "guide"))


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def fetch(self, locator, **limits):
        self.requests.append((locator, limits))
        if not self.responses:
            raise AssertionError("unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class EvidenceFreshnessRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sources"
        self.root.mkdir()
        self.now = "2026-08-13T18:00:00Z"
        self.limits = AcquisitionLimits(
            max_specs=3, max_redirects=2, max_response_bytes=1024,
            max_header_bytes=128, connect_timeout_seconds=1,
            per_spec_timeout_seconds=3, total_timeout_seconds=10,
            max_retained_files=8, max_directory_levels=4,
            max_total_retained_bytes=4096,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def deps(self, client, monotonic=lambda: 1.0, failpoint=lambda _: None):
        return AcquisitionDependencies(
            self.root, client, lambda: self.now, monotonic, failpoint,
        )

    def prepare(self, *, sources=(("source-one", SOURCE_LOCATOR, b"baseline"),),
                successors=None, redirect_hosts=None):
        specs = []
        successors = successors or {}
        redirect_hosts = redirect_hosts or {}
        for source_id, locator, _ in sources:
            spec = AcquisitionSpec(
                source_id, locator, ("text/html",),
                redirect_hosts.get(source_id, ()),
            )
            object.__setattr__(spec, "successor_locator", successors.get(source_id))
            object.__setattr__(spec, "successor_version", None)
            object.__setattr__(spec, "successor_effective_date", None)
            specs.append(spec)
        specs = tuple(specs)
        acquired = acquire_official_evidence(
            specs,
            self.deps(FakeHTTP([
                HTTPResponse(200, (("Content-Type", "text/html"),), body)
                for _, _, body in sources
            ])),
            limits=self.limits,
        )
        graph_template = graph()
        graph_sources = []
        for index, record in enumerate(acquired):
            template = deepcopy(graph_template["sources"][0])
            template.update({
                "source_id": record["source_id"],
                "source_digest": record["content_digest"],
                "canonical_locator": record["requested_locator"],
                "media_type": record["media_type"],
                "retained_artifact_digests": [record["content_digest"]],
                "operational_question_ids": ["question-one"],
            })
            graph_sources.append(template)
        graph_value = graph(graph_sources)
        return specs, acquired, graph_value, policy(acquired, graph_value, successors)

    def refresh(self, policy_value, graph_value, records, specs, client, *,
                monotonic=lambda: 1.0, failpoint=lambda _: None):
        return refresh_official_evidence(
            policy_value, graph_value, specs,
            self.deps(client, monotonic, failpoint),
            limits=self.limits,
        )

    def crashing_refresh(self, boundary, policy_value, graph_value, records,
                         specs, client):
        def failpoint(name):
            if name == boundary:
                raise RuntimeError(boundary)
        with self.assertRaisesRegex(RuntimeError, boundary):
            self.refresh(
                policy_value, graph_value, records, specs, client,
                failpoint=failpoint,
            )

    def test_classifies_successful_unchanged_updated_and_retains_new_digest(self):
        for expected, observed_body in (("unchanged", b"baseline"), ("updated", b"changed")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                original_record = (self.root / "records" / "source-one.json").read_bytes()
                summary = self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), observed_body)]),
                )
                self.assertEqual(expected, summary["results"][0]["classification"])
                self.assertEqual(original_record, (self.root / "records" / "source-one.json").read_bytes())
                digest_name = hashlib.sha256(observed_body).hexdigest()
                self.assertEqual(observed_body, (self.root / "artifacts" / "sha256" / digest_name).read_bytes())

    def test_declared_successor_without_observed_semantic_metadata_investigates(self):
        successor = REDIRECT_LOCATOR
        specs, records, graph_value, policy_value = self.prepare(
            successors={"source-one": successor},
            redirect_hosts={"source-one": (REDIRECT_HOST,)},
        )
        result = self.refresh(
            policy_value, graph_value, records, specs,
            FakeHTTP([
                HTTPResponse(302, (("Location", successor),), b""),
                HTTPResponse(200, (("Content-Type", "text/html"),), b"successor"),
            ]),
        )
        self.assertEqual("investigate", result["results"][0]["classification"])
        self.assertEqual(
            ["redirect_drift", "locator_drift", "version_ambiguous",
             "effective_date_ambiguous"],
            result["results"][0]["reason_codes"],
        )
        observed = json.loads(next(
            (self.root / "freshness" / "observations").iterdir()
        ).read_text())
        self.assertIsNone(observed["version"])
        self.assertIsNone(observed["effective_date"])

    def test_classifies_terminal_unavailability(self):
        for status in (404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                summary = self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(status, (("Content-Type", "text/html"),), b"")]),
                )
                self.assertEqual("unavailable", summary["results"][0]["classification"])

    def test_models_nonterminal_http_failures_as_investigate(self):
        for status in (401, 429, 500, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                summary = self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(status, (), b"")]),
                )
                self.assertEqual("investigate", summary["results"][0]["classification"])

    def test_rejects_unsafe_redirect_media_and_response_budgets(self):
        cases = (
            ([HTTPResponse(302, (("Location", neutral_https_locator(("source", "escape"), ("te", "st"), ("guide",))),), b"")], "official locator"),
            ([HTTPResponse(200, (("Content-Type", "application/pdf"),), b"pdf")], "media type"),
            ([HTTPResponse(200, (("Content-Type", "text/html"),), b"x" * 1025)], "byte budget"),
            ([HTTPResponse(200, (("X-Large", "x" * 200), ("Content-Type", "text/html")), b"x")], "header byte budget"),
            ([HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("a",))),), b""),
              HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("b",))),), b""),
              HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("c",))),), b"")], "redirect budget"),
        )
        for responses, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                with self.assertRaisesRegex(AcquisitionError, message):
                    self.refresh(policy_value, graph_value, records, specs, FakeHTTP(responses))

    def test_enforces_total_time_and_passes_same_limits_on_every_hop(self):
        specs, records, graph_value, policy_value = self.prepare(
            redirect_hosts={"source-one": (REDIRECT_HOST,)},
        )
        client = FakeHTTP([
            HTTPResponse(302, (("Location", REDIRECT_LOCATOR),), b""),
            HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline"),
        ])
        self.refresh(policy_value, graph_value, records, specs, client)
        self.assertEqual(2, len(client.requests))
        self.assertTrue(all(request[1] == client.requests[0][1] for request in client.requests))

        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory) / "sources"; self.root.mkdir()
            specs, records, graph_value, policy_value = self.prepare()
            ticks = iter((0.0, 11.0))
            with self.assertRaisesRegex(AcquisitionError, "total acquisition time budget"):
                self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
                    monotonic=lambda: next(ticks),
                )

    def test_rejects_spec_order_source_and_baseline_drift_before_request(self):
        sources = (
            ("source-one", SOURCE_LOCATOR, b"one"),
            ("source-two", SOURCE_TWO_LOCATOR, b"two"),
        )
        specs, records, graph_value, policy_value = self.prepare(sources=sources)
        cases = []
        cases.append((tuple(reversed(specs)), records, policy_value))
        cases.append(((AcquisitionSpec(
            "source-other", specs[0].locator, specs[0].media_types, (),
        ), specs[1]), records, policy_value))
        drifted_policy = deepcopy(policy_value)
        drifted_policy["sources"][0]["prior_record_digest"] = "sha256:" + "0" * 64
        drifted_policy = bind(drifted_policy, "policy_digest")
        cases.append((specs, records, drifted_policy))
        for bad_specs, bad_records, bad_policy in cases:
            with self.subTest(specs=bad_specs):
                client = FakeHTTP([])
                with self.assertRaises(Exception):
                    self.refresh(bad_policy, graph_value, bad_records, bad_specs, client)
                self.assertEqual([], client.requests)

    def test_disk_record_drift_extra_inventory_and_record_injection_reject(self):
        for mutation in ("drift", "extra"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                records_root = self.root / "records"
                if mutation == "drift":
                    stored = json.loads((records_root / "source-one.json").read_text())
                    stored["record_digest"] = "sha256:" + "0" * 64
                    (records_root / "source-one.json").write_text(json.dumps(stored))
                else:
                    (records_root / "source-extra.json").write_text("{}\n")
                client = FakeHTTP([])
                with self.assertRaises(Exception):
                    self.refresh(policy_value, graph_value, records, specs, client)
                self.assertEqual([], client.requests)

        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory) / "sources"; self.root.mkdir()
            specs, records, graph_value, policy_value = self.prepare()
            client = FakeHTTP([])
            with self.assertRaises(TypeError):
                refresh_official_evidence(
                    policy_value, graph_value, records, specs, self.deps(client),
                    limits=self.limits,
                )
            self.assertEqual([], client.requests)

    def test_each_retained_refresh_directory_replacement_fails_closed_after_intent(self):
        cases = (
            (Path("records"), True),
            (Path("staging"), True),
            (Path("artifacts"), False),
            (Path("artifacts/sha256"), True),
        )
        for relative, recreate_leaf in cases:
            with self.subTest(relative=str(relative)), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"
                self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                moved = self.root / ("moved-" + relative.name)

                def replace_retained(name):
                    if name == "after_refresh_intent":
                        target = self.root / relative
                        target.rename(moved)
                        target.mkdir()
                        if relative == Path("artifacts") and recreate_leaf:
                            (target / "sha256").mkdir()
                        if relative == Path("artifacts"):
                            (target / "sha256").mkdir()

                client = FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")])
                with self.assertRaises((AcquisitionError, FreshnessStorageError)):
                    self.refresh(
                        policy_value, graph_value, records, specs, client,
                        failpoint=replace_retained,
                    )
                self.assertTrue(moved.exists())
                if relative == Path("artifacts"):
                    self.assertEqual({"sha256"}, {item.name for item in (self.root / relative).iterdir()})
                else:
                    self.assertEqual([], list((self.root / relative).iterdir()))

    def test_summary_is_absent_until_every_source_has_a_terminal_comparison(self):
        sources = (
            ("source-one", SOURCE_LOCATOR, b"one"),
            ("source-two", SOURCE_TWO_LOCATOR, b"two"),
        )
        specs, records, graph_value, policy_value = self.prepare(sources=sources)
        client = FakeHTTP([
            HTTPResponse(200, (("Content-Type", "text/html"),), b"one"),
            RuntimeError("second fetch failed"),
        ])
        with self.assertRaisesRegex(RuntimeError, "second fetch failed"):
            self.refresh(policy_value, graph_value, records, specs, client)
        self.assertEqual([spec.locator for spec in specs], [item[0] for item in client.requests])
        freshness = self.root / "freshness"
        self.assertEqual(1, len(list((freshness / "observations").iterdir())))
        self.assertEqual(1, len(list((freshness / "comparisons").iterdir())))
        self.assertFalse(any((freshness / "summaries").iterdir()))
        recovery_names = {item.name for item in (freshness / "recovery").iterdir()}
        self.assertTrue(any(name.endswith(".run.json") for name in recovery_names))
        self.assertTrue(any(name.endswith(".step-0000.json") for name in recovery_names))

        retry = FakeHTTP([
            HTTPResponse(200, (("Content-Type", "text/html"),), b"two"),
        ])
        summary = self.refresh(policy_value, graph_value, records, specs, retry)
        self.assertEqual([specs[1].locator], [item[0] for item in retry.requests])
        self.assertEqual(
            ["source-one", "source-two"],
            [item["source_id"] for item in summary["results"]],
        )

    def test_intent_is_durable_before_first_response_and_exact_retry_resumes(self):
        specs, records, graph_value, policy_value = self.prepare()
        with self.assertRaisesRegex(RuntimeError, "before response"):
            self.refresh(
                policy_value, graph_value, records, specs,
                FakeHTTP([RuntimeError("before response")]),
            )
        freshness = self.root / "freshness"
        recovery_names = {item.name for item in (freshness / "recovery").iterdir()}
        self.assertTrue(any(name.endswith(".run.json") for name in recovery_names))
        self.assertFalse(any(name.endswith(".step-0000.json") for name in recovery_names))
        recovered = recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("resume", recovered[0]["classification"])

        retry = FakeHTTP([
            HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline"),
        ])
        summary = self.refresh(policy_value, graph_value, records, specs, retry)
        self.assertEqual("unchanged", summary["results"][0]["classification"])
        self.assertEqual([specs[0].locator], [item[0] for item in retry.requests])

    def test_completed_run_starts_a_new_invocation_and_fetches_again(self):
        specs, records, graph_value, policy_value = self.prepare()
        first = self.refresh(
            policy_value,
            graph_value,
            records,
            specs,
            FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
            monotonic=lambda: 1.0,
        )
        second_client = FakeHTTP([
            HTTPResponse(200, (("Content-Type", "text/html"),), b"changed-body"),
        ])
        second = self.refresh(
            policy_value,
            graph_value,
            records,
            specs,
            second_client,
            monotonic=lambda: 1.0,
        )
        self.assertEqual([specs[0].locator], [item[0] for item in second_client.requests])
        self.assertEqual("unchanged", first["results"][0]["classification"])
        self.assertEqual("updated", second["results"][0]["classification"])
        self.assertNotEqual(first["summary_id"], second["summary_id"])

    def test_refresh_history_duplicate_gap_and_overflow_fail_closed_before_network(self):
        specs, records, graph_value, policy_value = self.prepare()
        self.refresh(
            policy_value, graph_value, records, specs,
            FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
            monotonic=lambda: 1.0,
        )
        self.refresh(
            policy_value, graph_value, records, specs,
            FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"changed")]),
            monotonic=lambda: 1.0,
        )
        recovery = self.root / "freshness" / "recovery"
        run_paths = sorted(recovery.glob("*.run.json"))
        self.assertEqual(2, len(run_paths))
        cases = (
            ("duplicate", 1),
            ("gap", 3),
            ("overflow", 145),
        )
        for label, sequence in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"
                self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
                    monotonic=lambda: 1.0,
                )
                self.refresh(
                    policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"changed")]),
                    monotonic=lambda: 1.0,
                )
                cloned_runs = sorted((self.root / "freshness" / "recovery").glob("*.run.json"))
                self.assertEqual(2, len(cloned_runs))
                target_path = next(
                    path for path in cloned_runs
                    if json.loads(path.read_text())["invocation_sequence"] == 2
                )
                run_value = json.loads(target_path.read_text())
                run_value["invocation_sequence"] = sequence
                run_value["run_digest"] = bind(run_value, "run_digest")["run_digest"]
                target_path.write_text(json.dumps(run_value))
                client = FakeHTTP([])
                with self.assertRaises(FreshnessStorageError):
                    self.refresh(policy_value, graph_value, records, specs, client)
                self.assertEqual([], client.requests)

    def test_body_stage_crash_resumes_exact_owned_stage(self):
        specs, records, graph_value, policy_value = self.prepare()
        response = HTTPResponse(
            200, (("Content-Type", "text/html"),), b"changed-body",
        )
        self.crashing_refresh(
            "after_refresh_body_stage", policy_value, graph_value, records,
            specs, FakeHTTP([response]),
        )
        staged = list((self.root / "staging").glob("freshness-*.part"))
        self.assertEqual(1, len(staged))
        retry = FakeHTTP([])
        summary = self.refresh(
            policy_value, graph_value, records, specs, retry,
        )
        self.assertEqual([], retry.requests)
        self.assertEqual("updated", summary["results"][0]["classification"])
        self.assertEqual([], list((self.root / "staging").glob("freshness-*.part")))

    def test_crashed_invocation_reuses_persisted_identity_even_if_retry_clock_changes(self):
        specs, records, graph_value, policy_value = self.prepare()
        response = HTTPResponse(
            200, (("Content-Type", "text/html"),), b"changed-body",
        )
        self.crashing_refresh(
            "after_refresh_body_stage", policy_value, graph_value, records,
            specs, FakeHTTP([response]),
        )
        persisted_run = json.loads(next(
            (self.root / "freshness" / "recovery").glob("*.run.json")
        ).read_text())
        retry = FakeHTTP([])
        summary = self.refresh(
            policy_value,
            graph_value,
            records,
            specs,
            retry,
            monotonic=lambda: 99.0,
        )
        reloaded_run = json.loads(next(
            (self.root / "freshness" / "recovery").glob("*.run.json")
        ).read_text())
        self.assertEqual([], retry.requests)
        self.assertEqual(persisted_run["attempt_id"], reloaded_run["attempt_id"])
        self.assertEqual("updated", summary["results"][0]["classification"])

    def test_bodyless_terminal_fetch_receipt_resumes_without_network(self):
        for status in (410, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                self.crashing_refresh(
                    "after_refresh_fetch_receipt", policy_value, graph_value,
                    records, specs, FakeHTTP([HTTPResponse(status, (), b"")]),
                )
                retry = FakeHTTP([])
                summary = self.refresh(
                    policy_value, graph_value, records, specs, retry,
                )
                self.assertEqual([], retry.requests)
                self.assertEqual(
                    "unavailable" if status == 410 else "investigate",
                    summary["results"][0]["classification"],
                )

    def test_stage_without_fetch_receipt_fails_closed_before_network(self):
        specs, records, graph_value, policy_value = self.prepare()
        self.crashing_refresh(
            "after_refresh_body_stage_before_receipt", policy_value, graph_value,
            records, specs, FakeHTTP([HTTPResponse(
                200, (("Content-Type", "text/html"),), b"changed-body",
            )]),
        )
        staged = next((self.root / "staging").glob("freshness-*.part"))
        retry = FakeHTTP([])
        with self.assertRaisesRegex(AcquisitionError, "stage.*receipt"):
            self.refresh(policy_value, graph_value, records, specs, retry)
        self.assertEqual([], retry.requests)
        self.assertEqual(b"changed-body", staged.read_bytes())

    def test_conflicting_fetch_receipt_fails_closed_before_network(self):
        specs, records, graph_value, policy_value = self.prepare()
        self.crashing_refresh(
            "after_refresh_body_stage", policy_value, graph_value, records,
            specs, FakeHTTP([HTTPResponse(
                200, (("Content-Type", "text/html"),), b"changed-body",
            )]),
        )
        receipt = next(
            (self.root / "freshness" / "recovery").glob("*.fetch.json")
        )
        receipt.write_text('{"source_id":"source-one","source_id":"other"}\n')
        client = FakeHTTP([])
        with self.assertRaises(FreshnessStorageError):
            self.refresh(policy_value, graph_value, records, specs, client)
        self.assertEqual([], client.requests)

    def test_conflicting_staged_body_fails_closed_and_is_preserved(self):
        specs, records, graph_value, policy_value = self.prepare()
        response = HTTPResponse(
            200, (("Content-Type", "text/html"),), b"changed-body",
        )
        self.crashing_refresh(
            "after_refresh_body_stage", policy_value, graph_value, records,
            specs, FakeHTTP([response]),
        )
        staged = next((self.root / "staging").glob("freshness-*.part"))
        staged.write_bytes(b"conflict")
        client = FakeHTTP([response])
        with self.assertRaisesRegex(AcquisitionError, "refresh.*(artifact|receipt)|receipt body"):
            self.refresh(policy_value, graph_value, records, specs, client)
        self.assertEqual(b"conflict", staged.read_bytes())

    def test_foreign_refresh_stage_fails_closed_and_is_preserved(self):
        specs, records, graph_value, policy_value = self.prepare()
        foreign = self.root / "staging" / ("freshness-" + "0" * 64 + ".part")
        foreign.write_bytes(b"foreign")
        with self.assertRaisesRegex(AcquisitionError, "staged refresh.*fetch receipt"):
            self.refresh(
                policy_value, graph_value, records, specs,
                FakeHTTP([HTTPResponse(
                    200, (("Content-Type", "text/html"),), b"changed-body",
                )]),
            )
        self.assertEqual(b"foreign", foreign.read_bytes())

    def test_prepared_source_resumes_every_publication_boundary_without_refetch(self):
        boundaries = (
            "after_refresh_prepare", "after_refresh_observation",
            "after_refresh_comparison", "before_refresh_commit",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"; self.root.mkdir()
                specs, records, graph_value, policy_value = self.prepare()
                self.crashing_refresh(
                    boundary, policy_value, graph_value, records, specs,
                    FakeHTTP([HTTPResponse(
                        200, (("Content-Type", "text/html"),), b"baseline",
                    )]),
                )
                recovery = self.root / "freshness" / "recovery"
                self.assertEqual(1, len(list(recovery.glob("*.prepare.json"))))
                retry = FakeHTTP([])
                summary = self.refresh(
                    policy_value, graph_value, records, specs, retry,
                )
                self.assertEqual([], retry.requests)
                self.assertEqual("unchanged", summary["results"][0]["classification"])
                self.assertEqual(1, len(list(
                    (self.root / "freshness" / "observations").iterdir()
                )))
                self.assertEqual(1, len(list(
                    (self.root / "freshness" / "comparisons").iterdir()
                )))
                self.assertEqual(1, len(list(recovery.glob("*.step-0000.json"))))

    def test_conflicting_prepared_publication_fails_closed_without_refetch(self):
        specs, records, graph_value, policy_value = self.prepare()
        self.crashing_refresh(
            "after_refresh_prepare", policy_value, graph_value, records, specs,
            FakeHTTP([HTTPResponse(
                200, (("Content-Type", "text/html"),), b"baseline",
            )]),
        )
        observations = self.root / "freshness" / "observations"
        prepare = json.loads(next(
            (self.root / "freshness" / "recovery").glob("*.prepare.json")
        ).read_text())
        conflict = observations / (prepare["observation"]["observation_id"] + ".json")
        conflict.write_text("{}\n")
        client = FakeHTTP([])
        with self.assertRaises(FreshnessStorageError):
            self.refresh(policy_value, graph_value, records, specs, client)
        self.assertEqual([], client.requests)
        self.assertEqual("{}\n", conflict.read_text())

    def test_ambiguous_refresh_journal_is_preserved_and_recovery_investigates(self):
        specs, records, graph_value, policy_value = self.prepare()
        with self.assertRaises(RuntimeError):
            self.refresh(
                policy_value, graph_value, records, specs,
                FakeHTTP([RuntimeError("offline")]),
            )
        run_path = next(
            item for item in (self.root / "freshness" / "recovery").iterdir()
            if item.name.endswith(".run.json")
        )
        original = run_path.read_bytes()
        run_path.write_text('{"operation":"refresh_official_evidence","operation":"other"}\n')
        ambiguous = run_path.read_bytes()
        recovered = recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", recovered[0]["classification"])
        self.assertEqual(ambiguous, run_path.read_bytes())
        self.assertNotEqual(original, ambiguous)

    def test_nonlexical_policy_order_is_preserved_in_results_and_transaction_writes(self):
        sources = (
            ("z-source", SOURCE_Z_LOCATOR, b"z"),
            ("a-source", SOURCE_A_LOCATOR, b"a"),
        )
        specs, records, graph_value, policy_value = self.prepare(sources=sources)
        summary = self.refresh(
            policy_value, graph_value, records, specs,
            FakeHTTP([
                HTTPResponse(200, (("Content-Type", "text/html"),), b"z"),
                HTTPResponse(200, (("Content-Type", "text/html"),), b"a"),
            ]),
        )
        self.assertEqual(
            ["z-source", "a-source"],
            [item["source_id"] for item in summary["results"]],
        )
        recovery = self.root / "freshness" / "recovery"
        transaction = json.loads(next(
            item for item in recovery.iterdir() if item.name.endswith(".bundle.json")
        ).read_text())
        observed_names = [
            item["name"] for item in transaction["writes"]
            if item["directory"] == "observations"
        ]
        compared_names = [
            item["name"] for item in transaction["writes"]
            if item["directory"] == "comparisons"
        ]
        observation_sources = [
            json.loads((self.root / "freshness" / "observations" / name).read_text())["source_id"]
            for name in observed_names
        ]
        comparison_sources = [
            json.loads((self.root / "freshness" / "comparisons" / name).read_text())["source_id"]
            for name in compared_names
        ]
        self.assertEqual(["z-source", "a-source"], observation_sources)
        self.assertEqual(["z-source", "a-source"], comparison_sources)

    def test_completed_refresh_artifacts_replay_and_recover_without_network(self):
        specs, records, graph_value, policy_value = self.prepare()
        summary = self.refresh(
            policy_value, graph_value, records, specs,
            FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
        )
        stored = json.loads(next((self.root / "freshness" / "summaries").iterdir()).read_text())
        self.assertEqual(summary, stored)
        offline = FakeHTTP([])
        with self.assertRaises(AssertionError):
            self.refresh(
                policy_value, graph_value, records, specs, offline,
            )
        self.assertEqual([specs[0].locator], [item[0] for item in offline.requests])
        recovered = recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("complete", recovered[0]["classification"])
        initial = acquire_official_evidence(specs, self.deps(FakeHTTP([])), limits=self.limits)
        self.assertEqual(records, initial)

    def test_explicit_workspace_freshness_namespace_is_shared_with_recovery(self):
        specs, records, graph_value, policy_value = self.prepare()
        freshness_root = self.root.parent / "workspace-freshness"
        freshness_root.mkdir()
        freshness_dependencies = FreshnessDependencies(
            freshness_root, _workspace_namespace=True,
        )
        summary = refresh_official_evidence(
            policy_value, graph_value, specs,
            self.deps(FakeHTTP([HTTPResponse(
                200, (("Content-Type", "text/html"),), b"baseline",
            )])),
            limits=self.limits,
            freshness_dependencies=freshness_dependencies,
        )
        self.assertFalse((self.root / "freshness").exists())
        self.assertEqual(
            summary,
            json.loads(next((freshness_root / "summaries").iterdir()).read_text()),
        )
        recovered = recover_freshness_transactions(freshness_dependencies)
        self.assertEqual("complete", recovered[0]["classification"])

    def test_explicit_freshness_namespace_must_differ_from_retained_sources(self):
        specs, _records, graph_value, policy_value = self.prepare()
        with self.assertRaisesRegex(FreshnessStorageError, "must differ"):
            refresh_official_evidence(
                policy_value, graph_value, specs,
                self.deps(FakeHTTP([])), limits=self.limits,
                freshness_dependencies=FreshnessDependencies(
                    self.root, _workspace_namespace=True,
                ),
            )

    def test_workspace_recovery_observes_each_refresh_journal_failpoint(self):
        boundaries = (
            "after_refresh_intent", "after_refresh_fetch_receipt",
            "after_refresh_body_stage", "after_refresh_prepare",
            "after_refresh_observation", "after_refresh_comparison",
            "before_refresh_commit",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "sources"
                self.root.mkdir()
                specs, _records, graph_value, policy_value = self.prepare()
                freshness_root = Path(directory) / "workspace-freshness"
                freshness_root.mkdir()
                freshness_dependencies = FreshnessDependencies(
                    freshness_root, _workspace_namespace=True,
                )

                def failpoint(name):
                    if name == boundary:
                        raise RuntimeError(boundary)

                with self.assertRaisesRegex(RuntimeError, boundary):
                    refresh_official_evidence(
                        policy_value, graph_value, specs,
                        self.deps(FakeHTTP([HTTPResponse(
                            200, (("Content-Type", "text/html"),), b"baseline",
                        )]), failpoint=failpoint),
                        limits=self.limits,
                        freshness_dependencies=freshness_dependencies,
                    )
                self.assertFalse((self.root / "freshness").exists())
                recovered = recover_freshness_transactions(freshness_dependencies)
                self.assertEqual(1, len(recovered))
                self.assertEqual("resume", recovered[0]["classification"])


if __name__ == "__main__":
    unittest.main()
