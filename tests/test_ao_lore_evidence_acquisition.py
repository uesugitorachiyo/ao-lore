import hashlib
import json
import os
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
    recover_official_evidence,
)
from ao_lore.benchmark import canonical_digest
from tests.neutral_fixture_utils import (
    neutral_https_locator,
    neutral_public_https_locator,
)


SOURCE_LOCATOR = neutral_public_https_locator(
    ("source",), ("record", "brief-guide"),
)
SOURCE_HOST = urlsplit(SOURCE_LOCATOR).hostname
REDIRECT_LOCATOR = neutral_public_https_locator(
    ("source", "redirect"), ("record", "brief-guide"),
)
REDIRECT_HOST = urlsplit(REDIRECT_LOCATOR).hostname


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def fetch(self, locator, **limits):
        self.requests.append((locator, limits))
        if not self.responses:
            raise AssertionError("unexpected network request")
        return self.responses.pop(0)


class EvidenceAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sources"
        self.root.mkdir()
        self.clock = lambda: "2026-08-13T01:02:03Z"
        self.limits = AcquisitionLimits(
            max_specs=3, max_redirects=2, max_response_bytes=1024,
            max_header_bytes=1024, connect_timeout_seconds=1,
            per_spec_timeout_seconds=3, total_timeout_seconds=10,
            max_retained_files=4, max_directory_levels=4,
            max_total_retained_bytes=2048,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def deps(self, client, failpoint=lambda _: None):
        return AcquisitionDependencies(self.root, client, self.clock, lambda: 1.0, failpoint)

    def spec(self, locator=SOURCE_LOCATOR, media=("text/html",),
             redirect_hosts=()):
        return AcquisitionSpec("source-guide", locator, media, redirect_hosts)

    def assert_recovered_complete(self, recovered, body):
        self.assertEqual("complete", recovered[0]["classification"])
        self.assertEqual("completed", recovered[0]["status"])
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(body, (self.root / "artifacts" / "sha256" / digest).read_bytes())

    def assert_offline_replay(self, body):
        replay = acquire_official_evidence((self.spec(),), self.deps(FakeHTTP([])), limits=self.limits)
        self.assertEqual("sha256:" + hashlib.sha256(body).hexdigest(), replay[0]["content_digest"])
        self.assertEqual("acquired", replay[0]["status"])

    def test_acquires_redirected_exact_official_source_and_replays_offline(self):
        body = b"<!doctype html><title>Official record guide</title>"
        client = FakeHTTP([
            HTTPResponse(302, (("Location", REDIRECT_LOCATOR),), b""),
            HTTPResponse(200, (("Content-Type", "text/html; charset=UTF-8"),), body),
        ])
        first = acquire_official_evidence(
            (self.spec(redirect_hosts=(REDIRECT_HOST,)),),
            self.deps(client), limits=self.limits,
        )
        self.assertEqual("acquired", first[0]["status"])
        self.assertEqual("sha256:" + hashlib.sha256(body).hexdigest(), first[0]["content_digest"])
        self.assertEqual([REDIRECT_LOCATOR], first[0]["redirect_chain"])
        retained = self.root / "artifacts" / "sha256" / hashlib.sha256(body).hexdigest()
        self.assertEqual(body, retained.read_bytes())
        self.assertEqual(1, retained.stat().st_nlink)
        replay = acquire_official_evidence(
            (self.spec(redirect_hosts=(REDIRECT_HOST,)),),
            self.deps(FakeHTTP([])), limits=self.limits,
        )
        self.assertEqual(first, replay)

    def test_cached_redirect_replay_requires_the_current_cross_host_grant(self):
        body = b"redirected record"
        acquire_official_evidence(
            (self.spec(redirect_hosts=(REDIRECT_HOST,)),),
            self.deps(FakeHTTP([
                HTTPResponse(302, (("Location", REDIRECT_LOCATOR),), b""),
                HTTPResponse(200, (("Content-Type", "text/html"),), body),
            ])),
            limits=self.limits,
        )
        with self.assertRaisesRegex(AcquisitionError, "^cached acquisition is invalid$"):
            acquire_official_evidence(
                (self.spec(),), self.deps(FakeHTTP([])), limits=self.limits,
            )

    def test_cached_record_is_bound_to_active_spec_and_retained_bytes(self):
        body = b"cached record"
        original = acquire_official_evidence(
            (self.spec(),),
            self.deps(FakeHTTP([
                HTTPResponse(200, (("Content-Type", "text/html"),), body),
            ])),
            limits=self.limits,
        )[0]
        record_path = self.root / "records" / "source-guide.json"
        alternate = SOURCE_LOCATOR.rsplit("/", 1)[0] + "/alternate"

        def unsuccessful(value):
            value.update(
                status="unavailable", http_status=503, final_locator=None,
                media_type=None, byte_count=0, content_digest=None,
                redirect_chain=[],
            )

        mutations = (
            lambda value: value.update(source_id="source-other"),
            lambda value: value.update(requested_locator=alternate),
            lambda value: value.update(acquisition_id="acquisition-copied"),
            lambda value: value.update(status="verified"),
            lambda value: value.update(http_status=201),
            lambda value: value.update(media_type="application/pdf"),
            lambda value: value.update(byte_count=len(body) + 1),
            lambda value: value.update(final_locator=alternate),
            lambda value: value.update(
                redirect_chain=[alternate, alternate + "-2", alternate + "-3"],
                final_locator=alternate + "-3",
            ),
            unsuccessful,
        )
        for mutate in mutations:
            changed = deepcopy(original)
            mutate(changed)
            changed["record_digest"] = canonical_digest({
                key: item for key, item in changed.items()
                if key != "record_digest"
            })
            record_path.write_text(
                json.dumps(changed, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                AcquisitionError, "^cached acquisition is invalid$"
            ):
                acquire_official_evidence(
                    (self.spec(),), self.deps(FakeHTTP([])), limits=self.limits,
                )

        record_path.write_text(
            json.dumps(original, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(AcquisitionError, "^cached acquisition is invalid$"):
            acquire_official_evidence(
                (self.spec(locator=alternate),),
                self.deps(FakeHTTP([])), limits=self.limits,
            )

    def test_initial_and_same_host_redirect_need_no_explicit_host(self):
        body = b"same host"
        client = FakeHTTP([
            HTTPResponse(302, (("Location", "/record/current"),), b""),
            HTTPResponse(200, (("Content-Type", "text/html"),), body),
        ])
        result = acquire_official_evidence(
            (self.spec(SOURCE_LOCATOR),),
            self.deps(client), limits=self.limits,
        )
        self.assertEqual(
            SOURCE_LOCATOR.rsplit("/", 1)[0] + "/current",
            result[0]["final_locator"],
        )

    def test_cross_host_redirect_requires_exact_explicit_host(self):
        target = neutral_public_https_locator(("source", "alternate"), ("record",))
        target_host = urlsplit(target).hostname
        denied = FakeHTTP([HTTPResponse(302, (("Location", target),), b"")])
        with self.assertRaisesRegex(AcquisitionError, "host is not allowed"):
            acquire_official_evidence(
                (self.spec(SOURCE_LOCATOR),),
                self.deps(denied), limits=self.limits,
            )
        self.assertEqual(1, len(denied.requests))

        allowed = FakeHTTP([
            HTTPResponse(302, (("Location", target),), b""),
            HTTPResponse(200, (("Content-Type", "text/html"),), b"allowed"),
        ])
        result = acquire_official_evidence(
            (self.spec(SOURCE_LOCATOR, redirect_hosts=(target_host,)),),
            self.deps(allowed), limits=self.limits,
        )
        self.assertEqual(target, result[0]["final_locator"])

    def test_every_redirect_hop_is_revalidated_against_exact_hosts(self):
        client = FakeHTTP([
            HTTPResponse(302, (("Location", neutral_public_https_locator(
                ("source", "alternate"), ("one",),
            )),), b""),
            HTTPResponse(302, (("Location", neutral_public_https_locator(
                ("source", "escape"), ("two",),
            )),), b""),
        ])
        with self.assertRaisesRegex(AcquisitionError, "host is not allowed"):
            acquire_official_evidence(
                (self.spec(SOURCE_LOCATOR,
                           redirect_hosts=("source-alternate.example.com",)),),
                self.deps(client), limits=self.limits,
            )
        self.assertEqual(2, len(client.requests))

    def test_redirect_host_tuple_is_immutable_bounded_and_normalized(self):
        self.assertEqual((), self.spec().redirect_hosts)
        invalid_host_sets = (
            ["source-alternate.example.com"],
            ([],),
            ("source-alternate.example.com", "source-alternate.example.com"),
            tuple(f"source-{index}.example.com" for index in range(9)),
            ("SOURCE.example.com",), ("*.example.com",), ("example.com.",),
            ("éxample.example",),
            (SOURCE_HOST,),
            ("user" + "@" + "example.com",), ("127.0.0.1",), ("[::1]",),
            ("example.com:443",), ("example.com#fragment",),
            ("bad\nhost.example",), ("-bad.example",), ("bad-.example",),
        )
        for redirect_hosts in invalid_host_sets:
            with self.subTest(redirect_hosts=redirect_hosts), self.assertRaisesRegex(
                    AcquisitionError, "redirect host"):
                acquire_official_evidence(
                    (self.spec(SOURCE_LOCATOR,
                               redirect_hosts=redirect_hosts),),
                    self.deps(FakeHTTP([])), limits=self.limits,
                )

    def test_reserved_synthetic_redirect_host_is_rejected_before_http_even_when_declared(self):
        for locator in (
            neutral_https_locator(("source",), ("in", "valid"), ("record",)),
        ):
            client = FakeHTTP([])
            with self.subTest(locator=locator), self.assertRaisesRegex(
                    AcquisitionError, "reserved synthetic"):
                acquire_official_evidence(
                    (self.spec(locator, redirect_hosts=(urlsplit(locator).hostname,)),),
                    self.deps(client), limits=self.limits,
                )
            self.assertEqual([], client.requests)

    def test_rejects_every_legacy_numeric_ipv4_spelling_before_http(self):
        hosts = (
            "0x7f000001", "0x7f.0.0.1", "127.0.0x0.1",
            "2130706433", "017700000001", "127.1", "0177.0.0.1",
        )
        for host in hosts:
            client = FakeHTTP([])
            with self.subTest(host=host), self.assertRaisesRegex(
                    AcquisitionError, "official locator"):
                acquire_official_evidence(
                    (self.spec("https" + "://" + host + "/source"),),
                    self.deps(client), limits=self.limits,
                )
            self.assertEqual([], client.requests)

    def test_rejects_raw_redirect_location_controls_without_follow_up_request(self):
        next_locator = neutral_public_https_locator(("source",), ("next",))
        locations = (
            "\n" + next_locator,
            next_locator.replace("next", "ne\x00xt"),
            next_locator + "\t",
            next_locator.replace("next", "ne\x7fxt"),
            next_locator.replace("next", "ne\x85xt"),
            " " + next_locator,
            next_locator + " ",
            next_locator.replace("https", "HTTPS", 1),
            next_locator.replace("https", "hTtPs", 1),
        )
        for location in locations:
            client = FakeHTTP([HTTPResponse(302, (("Location", location),), b"")])
            with self.subTest(location=repr(location)), self.assertRaisesRegex(
                    AcquisitionError, "^official locator is invalid$"):
                acquire_official_evidence(
                    (self.spec(),), self.deps(client), limits=self.limits,
                )
            self.assertEqual(1, len(client.requests))
            self.assertEqual(SOURCE_LOCATOR, client.requests[0][0])

    def test_rejects_unsafe_locator_and_every_unsafe_redirect_form(self):
        unsafe = (
            SOURCE_LOCATOR.replace("https", "http", 1),
            SOURCE_LOCATOR.replace(SOURCE_HOST, SOURCE_HOST.upper()),
            SOURCE_LOCATOR.replace("://", "://user@", 1),
            SOURCE_LOCATOR.replace(SOURCE_HOST, SOURCE_HOST + ":444"),
            "https" + "://[broken/record",
        )
        for locator in unsafe:
            with self.subTest(locator=locator), self.assertRaisesRegex(AcquisitionError, "official locator"):
                acquire_official_evidence((self.spec(locator),), self.deps(FakeHTTP([])), limits=self.limits)
        for target in (
            *unsafe,
            neutral_https_locator(("source", "escape"), ("te", "st"), ("record",)),
        ):
            with self.subTest(target=target), self.assertRaisesRegex(AcquisitionError, "official locator"):
                client = FakeHTTP([HTTPResponse(302, (("Location", target),), b"")])
                acquire_official_evidence((self.spec(),), self.deps(client), limits=self.limits)

    def test_reserved_synthetic_locator_is_rejected_before_http(self):
        client = FakeHTTP([])
        with self.assertRaisesRegex(AcquisitionError, "reserved synthetic"):
            acquire_official_evidence((self.spec(neutral_https_locator(
                ("source",), ("in", "valid"), ("record",),
            )),), self.deps(client), limits=self.limits)
        self.assertEqual([], client.requests)

    def test_rejects_media_headers_body_and_redirect_budget(self):
        cases = (
            (FakeHTTP([HTTPResponse(200, (("Content-Type", "application/pdf"),), b"pdf")]), "media type"),
            (FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"x" * 1025)]), "byte budget"),
            (FakeHTTP([
                HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("a",))),), b""),
                HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("b",))),), b""),
                HTTPResponse(302, (("Location", neutral_public_https_locator(("source",), ("c",))),), b""),
            ]), "redirect budget"),
        )
        for client, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(AcquisitionError, message):
                acquire_official_evidence((self.spec(),), self.deps(client), limits=self.limits)

    def test_non_success_retains_the_pinned_initial_acquisition_error(self):
        client = FakeHTTP([
            HTTPResponse(503, (("X-Large", "x" * 2048),), b"unavailable"),
        ])
        with self.assertRaisesRegex(
                AcquisitionError, "^required official source is unavailable$"):
            acquire_official_evidence(
                (self.spec(),), self.deps(client), limits=self.limits,
            )

    def test_crash_after_intent_recovers_exact_staged_bytes(self):
        body = b"stable official bytes"
        def failpoint(phase):
            if phase == "after_staging":
                raise RuntimeError("crash")
        client = FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), body)])
        with self.assertRaisesRegex(RuntimeError, "crash"):
            acquire_official_evidence((self.spec(),), self.deps(client, failpoint), limits=self.limits)
        recovered = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)
        self.assert_recovered_complete(recovered, body)
        self.assert_offline_replay(body)

    def test_recovery_handles_each_crash_boundary_exactly(self):
        bodies = {
            "after_intent": b"intent only bytes",
            "after_staging": b"staged bytes",
            "after_artifact_publish": b"published bytes",
            "after_record": b"recorded bytes",
            "after_terminal": b"terminal bytes",
        }
        for phase, body in bodies.items():
            with self.subTest(phase=phase):
                self.tearDown()
                self.setUp()

                def failpoint(name):
                    if name == phase:
                        raise RuntimeError("crash")

                client = FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), body)])
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    acquire_official_evidence((self.spec(),), self.deps(client, failpoint), limits=self.limits)
                first = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)
                second = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)

                if phase == "after_intent":
                    self.assertEqual(("conflict", "investigate"), (first[0]["status"], first[0]["classification"]))
                    self.assertEqual(first, second)
                    self.assertFalse((self.root / "records" / "source-guide.json").exists())
                elif phase == "after_terminal":
                    self.assertEqual([], first)
                    self.assertEqual([], second)
                    self.assert_offline_replay(body)
                else:
                    self.assert_recovered_complete(first, body)
                    self.assertEqual([], second)
                    self.assert_offline_replay(body)

    def test_recovery_investigates_conflicting_record_after_artifact_publish(self):
        body = b"artifact without terminal"

        def failpoint(phase):
            if phase == "after_artifact_publish":
                raise RuntimeError("crash")

        client = FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), body)])
        with self.assertRaisesRegex(RuntimeError, "crash"):
            acquire_official_evidence((self.spec(),), self.deps(client, failpoint), limits=self.limits)

        records = self.root / "records"
        records.mkdir(exist_ok=True)
        records.joinpath("source-guide.json").write_text(
            '{"schema_version":"ao.lore.evidence-acquisition-record.v0.1",'
            '"acquisition_id":"acquisition-bad","source_id":"source-guide",'
            '"requested_locator":"' + SOURCE_LOCATOR + '",'
            '"final_locator":"' + SOURCE_LOCATOR + '",'
            '"retrieved_at":"2026-08-13T01:02:03Z","status":"acquired",'
            '"http_status":200,"media_type":"text/html","byte_count":1,'
            '"content_digest":"sha256:' + ("0" * 64) + '","redirect_chain":[],'
            '"record_digest":"sha256:' + ("1" * 64) + '",'
            '"legal_advice":false,"property_decision":false,"candidate_review":false,'
            '"candidate_decision":false,"promotion":false,"canonical_query":false,'
            '"provider":false,"credential":false,"private_data":false,'
            '"publication":false,"release":false,"deployment":false,'
            '"authority_advanced":false}',
            encoding="utf-8",
        )
        recovered = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)
        self.assertEqual(("conflict", "investigate"), (recovered[0]["status"], recovered[0]["classification"]))
        self.assertTrue((records / "source-guide.json").exists())

    def test_recovery_marks_foreign_staging_and_digest_drift_investigate(self):
        (self.root / "staging").mkdir()
        (self.root / "staging" / "foreign.part").write_bytes(b"foreign")
        result = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)
        self.assertEqual(("conflict", "investigate"), (result[0]["status"], result[0]["classification"]))
        self.assertTrue((self.root / "staging" / "foreign.part").exists())

    def test_rejects_root_swap_symlink_hardlink_fifo_and_destination_conflict(self):
        outside = Path(self.temporary.name) / "outside"; outside.mkdir()
        original = self.root
        original.rename(Path(self.temporary.name) / "old-root")
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(AcquisitionError, "source root"):
            acquire_official_evidence((self.spec(),), self.deps(FakeHTTP([])), limits=self.limits)
        self.root.unlink(); self.root.mkdir()

        artifacts = self.root / "artifacts" / "sha256"; artifacts.mkdir(parents=True)
        body = b"same bytes"; name = hashlib.sha256(body).hexdigest(); target = artifacts / name
        other = self.root / "other"; other.write_bytes(body); os.link(other, target)
        client = FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), body)])
        with self.assertRaisesRegex(AcquisitionError, "destination"):
            acquire_official_evidence((self.spec(),), self.deps(client), limits=self.limits)
        target.unlink(); other.unlink()

        if hasattr(os, "mkfifo"):
            staging = self.root / "staging"; staging.mkdir(exist_ok=True)
            os.mkfifo(staging / "foreign.part")
            result = recover_official_evidence(self.deps(FakeHTTP([])), limits=self.limits)
            self.assertEqual("investigate", result[0]["classification"])


if __name__ == "__main__":
    unittest.main()
