import json
import os
import tempfile
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.home import repository_root
from ao_lore.private_pdf_uat import (
    PrivatePdfUatError,
    cleanup_private_pdf_uat,
    load_private_pdf_manifest,
    prepare_representative_pdf_corpus,
)
from ao_lore.private_pdf_domain import (
    DOMAIN_CORPUS_ID,
    DOMAIN_ITEM_IDS,
    DOMAIN_PAGE_COUNTS,
    DOMAIN_PROVENANCE_DIGEST,
    DOMAIN_TRANSFORMATION_ID,
    MAX_DOMAIN_PDF_BYTES,
    PdfQualification,
    PreparedDomainDocument,
    PrivatePdfDomainError,
    ReviewedDomainCorpus,
    load_reviewed_domain_sources,
    parse_private_pdf_domain_review,
    restore_pdf_nomagic_header,
    validate_private_pdf_domain_review,
)


def review_value():
    return {
        "schema_version": "ao.lore.private-pdf-domain-review.v0.1",
        "corpus_id": DOMAIN_CORPUS_ID,
        "provenance": {
            "dataset_id": "napierone-pdf-nomagic",
            "document_digest": DOMAIN_PROVENANCE_DIGEST,
            "transformation_id": DOMAIN_TRANSFORMATION_ID,
        },
        "representative_domain": True,
        "documents": [
            {
                "item_id": item_id,
                "source_file": f"source-{index}.pdf",
                "source_digest": "sha256:" + str(index) * 64,
                "derived_digest": "sha256:" + str(index + 4) * 64,
                "page_count": pages,
                "expected_text": [f"TEST-ANCHOR-{index}"],
                "required_block_types": list(
                    (
                        ("heading", "paragraph"),
                        ("paragraph",),
                        ("heading", "list"),
                        ("heading", "table"),
                    )[index - 1]
                ),
            }
            for index, (item_id, pages) in enumerate(
                zip(DOMAIN_ITEM_IDS, DOMAIN_PAGE_COUNTS), start=1
            )
        ],
        "parser_id": "docling",
        "parser_version": "2.118.1",
        "ocr_enabled": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


class PrivatePdfDomainReviewTests(unittest.TestCase):
    def test_valid_review_returns_frozen_detached_contract(self):
        supplied = review_value()
        reviewed = validate_private_pdf_domain_review(supplied)
        self.assertIsInstance(reviewed, ReviewedDomainCorpus)
        self.assertEqual(DOMAIN_CORPUS_ID, reviewed.corpus_id)
        self.assertEqual(DOMAIN_PROVENANCE_DIGEST, reviewed.provenance_digest)
        self.assertEqual(DOMAIN_TRANSFORMATION_ID, reviewed.transformation_id)
        self.assertEqual(DOMAIN_ITEM_IDS, tuple(item.item_id for item in reviewed.documents))
        supplied["documents"][0]["expected_text"][0] = "MUTATED"
        self.assertEqual(("TEST-ANCHOR-1",), reviewed.documents[0].expected_text)

    def test_duplicate_and_semantically_conflicting_bindings_reject(self):
        mutations = (
            lambda value: value["documents"][1].update(source_file=value["documents"][0]["source_file"]),
            lambda value: value["documents"][1].update(source_digest=value["documents"][0]["source_digest"]),
            lambda value: value["documents"][1].update(derived_digest=value["documents"][0]["derived_digest"]),
            lambda value: value["documents"][1].update(expected_text=value["documents"][0]["expected_text"]),
            lambda value: value["documents"][0].update(derived_digest=value["documents"][0]["source_digest"]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = review_value()
                mutate(value)
                with self.assertRaisesRegex(ContractError, "private PDF domain review is invalid"):
                    validate_private_pdf_domain_review(value)

    def test_non_builtin_or_inexact_values_reject(self):
        class DictSubclass(dict):
            pass

        class StringSubclass(str):
            pass

        values = []
        top = DictSubclass(review_value())
        values.append(top)
        nested = review_value()
        nested["documents"] = tuple(nested["documents"])
        values.append(nested)
        scalar = review_value()
        scalar["documents"][0]["source_file"] = StringSubclass("source-1.pdf")
        values.append(scalar)
        extra = review_value()
        extra["documents"][0]["unknown"] = False
        values.append(extra)
        for value in values:
            with self.subTest(value_type=type(value)):
                with self.assertRaisesRegex(ContractError, "private PDF domain review is invalid"):
                    validate_private_pdf_domain_review(value)

    def test_wrong_order_pages_authority_and_unsafe_source_reject(self):
        mutations = (
            lambda value: value["documents"].reverse(),
            lambda value: value["documents"][0].update(page_count=2),
            lambda value: value.update(ocr_enabled=True),
            lambda value: value.update(provider_calls=True),
            lambda value: value.update(promotion_authority=True),
            lambda value: value.update(claims_authority_advance=True),
            lambda value: value["documents"][0].update(source_file="../source.pdf"),
        )
        for mutate in mutations:
            value = review_value()
            mutate(value)
            with self.assertRaisesRegex(ContractError, "private PDF domain review is invalid"):
                validate_private_pdf_domain_review(value)

    def test_duplicate_json_keys_reject_before_validation(self):
        body = json.dumps(review_value(), sort_keys=True).encode()
        body = body.replace(
            b'"corpus_id": "pdf-nomagic-uk-public-sector-v1"',
            b'"corpus_id": "pdf-nomagic-uk-public-sector-v1", "corpus_id": "other"',
        )
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            parse_private_pdf_domain_review(body)

    def test_validator_never_retains_caller_containers(self):
        supplied = review_value()
        reviewed = validate_private_pdf_domain_review(supplied)
        cloned = deepcopy(supplied)
        supplied.clear()
        self.assertEqual(cloned["corpus_id"], reviewed.corpus_id)
        self.assertEqual(tuple(cloned["documents"][2]["expected_text"]), reviewed.documents[2].expected_text)


def _digest(body):
    return "sha256:" + sha256(body).hexdigest()


def _source(index):
    return b"\x00" * 5 + b"1.7\n" + f"TEST PDF BODY {index}\n".encode()


def _review_for_bodies(bodies):
    value = review_value()
    for index, body in enumerate(bodies):
        value["documents"][index]["source_digest"] = _digest(body)
        value["documents"][index]["derived_digest"] = _digest(b"%PDF-" + body[5:])
    return validate_private_pdf_domain_review(value)


class PrivatePdfDomainSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bodies = tuple(_source(index) for index in range(1, 5))
        self.review = _review_for_bodies(self.bodies)
        for document, body in zip(self.review.documents, self.bodies):
            (self.root / document.source_file).write_bytes(body)

    def tearDown(self):
        self.temporary.cleanup()

    def qualify(self, body):
        index = next(
            index
            for index, source in enumerate(self.bodies)
            if body == b"%PDF-" + source[5:]
        )
        return PdfQualification(
            page_count=DOMAIN_PAGE_COUNTS[index], encrypted=False, readable_text=True
        )

    def test_exact_five_byte_transform_and_held_source_load(self):
        prepared = load_reviewed_domain_sources(
            self.root, self.review, qualify=self.qualify
        )
        self.assertEqual(DOMAIN_ITEM_IDS, tuple(item.item_id for item in prepared))
        self.assertTrue(all(isinstance(item, PreparedDomainDocument) for item in prepared))
        for supplied, reviewed, item in zip(self.bodies, self.review.documents, prepared):
            self.assertEqual(supplied, (self.root / reviewed.source_file).read_bytes())
            self.assertEqual(b"%PDF-" + supplied[5:], item.body)
            self.assertEqual(reviewed.source_digest, item.source_digest)
            self.assertEqual(reviewed.derived_digest, item.derived_digest)
            self.assertEqual(reviewed.page_count, item.page_count)

    def test_transform_rejects_nonexact_magicless_header(self):
        invalid = (
            b"\x00" + b"1.7\nbody",
            b"\x00" * 4 + b"X1.7\nbody",
            b"\x00" * 6 + b".7\nbody",
            b"%PDF-1.7\nbody",
            b"\x00" * 5 + b"2.0\nbody",
            b"\x00" * 5 + b"1.X\nbody",
            b"",
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaisesRegex(PrivatePdfDomainError, "representative PDF source is invalid"):
                    restore_pdf_nomagic_header(body)

    def test_digest_page_encryption_and_text_drift_fail_closed(self):
        bad_review_value = review_value()
        for index, body in enumerate(self.bodies):
            bad_review_value["documents"][index]["source_digest"] = _digest(body)
            bad_review_value["documents"][index]["derived_digest"] = _digest(b"%PDF-" + body[5:])
        bad_review_value["documents"][0]["source_digest"] = "sha256:" + "f" * 64
        bad_review = validate_private_pdf_domain_review(bad_review_value)
        cases = (
            (bad_review, self.qualify),
            (self.review, lambda body: PdfQualification(99, False, True)),
            (self.review, lambda body: PdfQualification(1, True, True)),
            (self.review, lambda body: PdfQualification(1, False, False)),
        )
        for review, qualify in cases:
            with self.subTest(review=review, qualify=qualify):
                with self.assertRaisesRegex(PrivatePdfDomainError, "representative PDF source qualification failed"):
                    load_reviewed_domain_sources(self.root, review, qualify=qualify)

    def test_symlink_hardlink_fifo_and_oversize_sources_reject(self):
        first = self.root / self.review.documents[0].source_file
        cases = []
        outside = self.root / "outside.pdf"
        outside.write_bytes(self.bodies[0])
        first.unlink()
        first.symlink_to(outside.name)
        cases.append("symlink")
        for label in cases:
            with self.subTest(label=label):
                with self.assertRaises(PrivatePdfDomainError):
                    load_reviewed_domain_sources(self.root, self.review, qualify=self.qualify)
        first.unlink()
        first.write_bytes(self.bodies[0])
        hardlink = self.root / "hardlink.pdf"
        os.link(first, hardlink)
        with self.assertRaises(PrivatePdfDomainError):
            load_reviewed_domain_sources(self.root, self.review, qualify=self.qualify)
        hardlink.unlink()
        first.unlink()
        os.mkfifo(first)
        with self.assertRaises(PrivatePdfDomainError):
            load_reviewed_domain_sources(self.root, self.review, qualify=self.qualify)
        first.unlink()
        first.write_bytes(self.bodies[0])
        with patch("ao_lore.private_pdf_domain.MAX_DOMAIN_PDF_BYTES", len(self.bodies[0]) - 1):
            with self.assertRaises(PrivatePdfDomainError):
                load_reviewed_domain_sources(self.root, self.review, qualify=self.qualify)

    def test_root_and_source_symlink_ancestors_reject(self):
        linked_root = self.root.parent / f"{self.root.name}-link"
        linked_root.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaises(PrivatePdfDomainError):
                load_reviewed_domain_sources(linked_root, self.review, qualify=self.qualify)
        finally:
            linked_root.unlink()

    def test_mutation_after_held_read_and_path_replacement_reject(self):
        original_fstat = os.fstat
        calls = 0

        def changed_fstat(descriptor):
            nonlocal calls
            result = original_fstat(descriptor)
            calls += 1
            if calls == 2:
                replacement = self.root / self.review.documents[0].source_file
                replacement.write_bytes(self.bodies[0] + b"changed")
            return result

        with patch("ao_lore.private_pdf_domain.os.fstat", side_effect=changed_fstat):
            with self.assertRaises(PrivatePdfDomainError):
                load_reviewed_domain_sources(self.root, self.review, qualify=self.qualify)

        first = self.root / self.review.documents[0].source_file
        first.write_bytes(self.bodies[0])

        def replace_after_read(body):
            replacement = self.root / "replacement.pdf"
            replacement.write_bytes(self.bodies[0])
            os.replace(replacement, first)
            return self.qualify(body)

        with self.assertRaises(PrivatePdfDomainError):
            load_reviewed_domain_sources(
                self.root, self.review, qualify=replace_after_read
            )

    def test_control_exception_closes_held_descriptors(self):
        before = len(os.listdir("/proc/self/fd"))

        def interrupt(_body):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            load_reviewed_domain_sources(self.root, self.review, qualify=interrupt)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_private_qualifier_errors_are_redacted(self):
        private = str(self.root / "secret.pdf")

        def fail(_body):
            raise OSError(private)

        with self.assertRaises(PrivatePdfDomainError) as captured:
            load_reviewed_domain_sources(self.root, self.review, qualify=fail)
        self.assertEqual("representative PDF source qualification failed", str(captured.exception))
        self.assertNotIn(private, str(captured.exception))


class PrivatePdfDomainPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.root = Path(self.temporary.name)
        self.sources = self.root / "reviewed"
        self.sources.mkdir()
        self.runtime = self.root / "runtime"
        self.bodies = tuple(_source(index) for index in range(1, 5))
        self.review = _review_for_bodies(self.bodies)
        for document, body in zip(self.review.documents, self.bodies):
            (self.sources / document.source_file).write_bytes(body)

    def tearDown(self):
        self.temporary.cleanup()

    def qualify(self, body):
        index = next(
            index
            for index, source in enumerate(self.bodies)
            if body == b"%PDF-" + source[5:]
        )
        return PdfQualification(DOMAIN_PAGE_COUNTS[index], False, True)

    def prepare(self):
        with patch(
            "ao_lore.private_pdf_uat._qualify_domain_pdf", side_effect=self.qualify
        ), patch(
            "ao_lore.private_pdf_uat._run_pdf_tool",
            side_effect=lambda arguments, body: (
                b"Pages: 1\nEncrypted: no\n"
                if arguments[0] == "pdfinfo"
                else b"text\n"
            ),
        ):
            return prepare_representative_pdf_corpus(
                self.review, self.sources, runtime_root=self.runtime
            )

    def test_prepare_load_is_idempotent_and_persists_only_derived_bytes(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(DOMAIN_CORPUS_ID, first["corpus_id"])
        self.assertEqual(list(DOMAIN_ITEM_IDS), [item["item_id"] for item in first["documents"]])
        with patch("ao_lore.private_pdf_uat._run_pdf_tool") as tool:
            tool.side_effect = lambda arguments, body: (
                b"Pages: 1\nEncrypted: no\n" if arguments[0] == "pdfinfo" else b"text\n"
            )
            loaded = load_private_pdf_manifest(
                DOMAIN_CORPUS_ID, runtime_root=self.runtime
            )
        self.assertEqual(first, loaded)
        corpus = self.runtime / "calibration/private-pdf" / DOMAIN_CORPUS_ID
        for reviewed, source in zip(first["documents"], self.bodies):
            persisted = (corpus / reviewed["file"]).read_bytes()
            self.assertEqual(b"%PDF-" + source[5:], persisted)
            self.assertNotEqual(source, persisted)

    def test_representative_dependencies_bind_review_origin_into_prepare(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        manifest = self.prepare()
        sentinel = object()
        with patch.object(
            private_pdf_uat,
            "prepare_representative_pdf_corpus",
            return_value=manifest,
        ) as intake, patch.object(
            private_pdf_uat, "_default_prepare_uat_run", return_value=sentinel
        ) as prepare:
            dependencies = private_pdf_uat.representative_private_pdf_uat_dependencies(
                self.review, self.sources, runtime_root=self.runtime
            )
            loaded = dependencies.load_manifest(self.runtime)
            result = dependencies.prepare_run(loaded, self.runtime)
        self.assertIs(sentinel, result)
        intake.assert_called_once_with(
            self.review, self.sources, runtime_root=self.runtime
        )
        origin = prepare.call_args.kwargs["origin"]
        self.assertTrue(origin.representative_domain)
        self.assertEqual(DOMAIN_PROVENANCE_DIGEST, origin.provenance_digest)
        self.assertEqual(DOMAIN_PAGE_COUNTS, origin.page_counts)
        self.assertEqual(canonical_digest(manifest), origin.corpus_digest)

    def test_representative_origin_survives_prepared_worker_and_calibration_contracts(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        manifest = self.prepare()
        origin = private_pdf_uat._representative_campaign_origin(
            self.review, manifest
        )
        qualification = build_manifest(
            fixture_corpus_digest=canonical_digest(
                private_pdf_uat._private_benchmark_corpus(manifest)
            ),
            parser_id="docling",
            parser_version="2.118.1",
            parser_configuration_digest=(
                private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST
            ),
            runtime="python-test",
            platform="linux-test",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["test"]],
            raw_metrics={},
            normalized_scores={},
            failures=[],
            exclusions=[],
        )
        qualification_path = self.runtime / "benchmarks/docling-2.118.1.json"
        qualification_path.parent.mkdir(parents=True)
        qualification_path.write_bytes(private_pdf_uat._manifest_body(qualification))
        cache = self.runtime / "cache/huggingface"
        for model in private_pdf_uat._PRIVATE_PDF_REQUIRED_CACHE_MODELS:
            snapshot = cache / "hub" / model / "snapshots/revision"
            snapshot.mkdir(parents=True)
            (snapshot / "model.bin").write_bytes(b"contained fixture")
        run = None
        before_candidates = {
            item["candidate_id"]: item
            for item in private_pdf_uat._default_queue_items()
        }
        verify = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verify.start()
        try:
            run = private_pdf_uat._default_prepare_uat_run(
                manifest, self.runtime, origin=origin
            )
            digest = private_pdf_uat._campaign_origin_digest(origin)
            evidence = private_pdf_uat._prepared_run_evidence(run)
            worker = private_pdf_uat._worker_run_contract(run, "resume")
            calibration = private_pdf_uat._private_pdf_calibration_contract(
                run, {}
            )
            for supplied in (evidence, worker, calibration):
                self.assertEqual(
                    private_pdf_uat._campaign_origin_value(origin),
                    supplied["campaign_origin"],
                )
                self.assertEqual(digest, supplied["campaign_origin_digest"])
            self.assertEqual(
                qualification["fixture_corpus_digest"],
                worker["qualification_corpus_digest"],
            )
            self.assertEqual(
                qualification["fixture_corpus_digest"],
                calibration["qualification_corpus_digest"],
            )
        finally:
            if run is not None:
                private_pdf_uat._cleanup_partial_uat_state(
                    run, before_candidates
                )
            verify.stop()

    def test_exact_domain_cleanup_is_idempotent_and_seed_cleanup_is_rejected(self):
        manifest = self.prepare()
        with patch("ao_lore.private_pdf_uat._run_pdf_tool") as tool:
            tool.side_effect = lambda arguments, body: (
                b"Pages: 1\nEncrypted: no\n" if arguments[0] == "pdfinfo" else b"text\n"
            )
            removed = cleanup_private_pdf_uat(
                DOMAIN_CORPUS_ID, runtime_root=self.runtime
            )
            absent = cleanup_private_pdf_uat(
                DOMAIN_CORPUS_ID, runtime_root=self.runtime
            )
        self.assertEqual(len(manifest["documents"]), removed["verified_documents"])
        self.assertTrue(removed["removed"])
        self.assertFalse(absent["removed"])
        with self.assertRaises(PrivatePdfUatError):
            cleanup_private_pdf_uat("unknown-corpus", runtime_root=self.runtime)

    def test_interrupted_prepare_reclaims_exact_stage_and_retry_succeeds(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        original = private_pdf_uat._persist_bytes_at
        interrupted = False

        def persist_then_interrupt(descriptor, name, body):
            nonlocal interrupted
            original(descriptor, name, body)
            if name == "domain-01.pdf" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        with patch(
            "ao_lore.private_pdf_uat._qualify_domain_pdf", side_effect=self.qualify
        ), patch(
            "ao_lore.private_pdf_uat._persist_bytes_at",
            side_effect=persist_then_interrupt,
        ), self.assertRaises(KeyboardInterrupt):
            prepare_representative_pdf_corpus(
                self.review, self.sources, runtime_root=self.runtime
            )
        private = self.runtime / "calibration/private-pdf"
        self.assertFalse((private / private_pdf_uat._DOMAIN_STAGE).exists())
        self.assertEqual(DOMAIN_CORPUS_ID, self.prepare()["corpus_id"])

    def test_prepare_bounds_interruption_inside_document_publication(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        real_link = os.link
        interrupted = False

        def interrupt_after_link(source, destination, *args, **kwargs):
            nonlocal interrupted
            real_link(source, destination, *args, **kwargs)
            if destination == "domain-01.pdf" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        with patch(
            "ao_lore.private_pdf_uat._qualify_domain_pdf", side_effect=self.qualify
        ), patch.object(
            private_pdf_uat.os, "link", side_effect=interrupt_after_link
        ), self.assertRaises(KeyboardInterrupt):
            prepare_representative_pdf_corpus(
                self.review, self.sources, runtime_root=self.runtime
            )
        private = self.runtime / "calibration/private-pdf"
        self.assertEqual(
            [private_pdf_uat._DOMAIN_PREPARATION_RECOVERY, private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private.iterdir()),
        )
        with self.assertRaises(PrivatePdfUatError):
            self.prepare()

    def test_interrupted_cleanup_resumes_from_exact_recovery(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        self.prepare()
        original = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def quarantine_then_interrupt(*args, **kwargs):
            nonlocal interrupted
            original(*args, **kwargs)
            if args[1] == "domain-01.pdf" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        with patch.object(
            private_pdf_uat,
            "_quarantine_verified_file_at",
            side_effect=quarantine_then_interrupt,
        ), patch.object(
            private_pdf_uat,
            "_run_pdf_tool",
            side_effect=lambda arguments, body: (
                b"Pages: 1\nEncrypted: no\n"
                if arguments[0] == "pdfinfo"
                else b"text\n"
            ),
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat(DOMAIN_CORPUS_ID, runtime_root=self.runtime)
        private = self.runtime / "calibration/private-pdf"
        names = {path.name for path in private.iterdir()}
        self.assertTrue(
            any(name.startswith(private_pdf_uat._DOMAIN_RECOVERY_PREFIX) for name in names)
        )
        self.assertFalse(
            any(name.startswith(private_pdf_uat._RECOVERY_PREFIX) for name in names)
        )
        result = cleanup_private_pdf_uat(
            DOMAIN_CORPUS_ID, runtime_root=self.runtime
        )
        self.assertTrue(result["removed"])
        self.assertEqual([private_pdf_uat._LOCK_NAME], sorted(path.name for path in private.iterdir()))

    def test_domain_and_ubuntu_corpora_coexist_and_cleanup_independently(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        self.prepare()
        seed_documents = []
        block_types = (
            ("heading", "paragraph"),
            ("paragraph",),
            ("heading", "list"),
            ("heading", "table"),
        )
        for index in range(1, 5):
            path = self.root / f"ubuntu-{index}.pdf"
            path.write_bytes(f"%PDF-1.7\nubuntu {index}\n".encode())
            seed_documents.append(
                private_pdf_uat.SeedDocument(
                    f"seed-{index:02d}",
                    path,
                    (f"UBUNTU-{index}",),
                    block_types[index - 1],
                )
            )
        tool = lambda arguments, body: (
            b"Pages: 1\nEncrypted: no\n"
            if arguments[0] == "pdfinfo"
            else b"text\n"
        )
        with patch.object(
            private_pdf_uat, "UBUNTU_PDF_SEED", tuple(seed_documents)
        ), patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=tool):
            ubuntu = private_pdf_uat.prepare_ubuntu_pdf_seed(
                runtime_root=self.runtime
            )
            cleanup_private_pdf_uat(DOMAIN_CORPUS_ID, runtime_root=self.runtime)
            retained = load_private_pdf_manifest(
                private_pdf_uat.PRIVATE_PDF_UAT_CORPUS_ID,
                runtime_root=self.runtime,
            )
        self.assertEqual(ubuntu, retained)
        private = self.runtime / "calibration/private-pdf"
        self.assertTrue((private / private_pdf_uat.PRIVATE_PDF_UAT_CORPUS_ID).is_dir())
        self.assertFalse((private / DOMAIN_CORPUS_ID).exists())

    def test_foreign_preparation_collision_is_preserved(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        private = self.runtime / "calibration/private-pdf"
        stage = private / private_pdf_uat._DOMAIN_STAGE
        stage.mkdir(parents=True)
        sentinel = stage / "foreign"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(PrivatePdfUatError):
            self.prepare()
        self.assertEqual(b"preserve", sentinel.read_bytes())


class PrivatePdfCampaignOriginTests(unittest.TestCase):
    def test_representative_origin_binds_review_and_derived_manifest(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        bodies = tuple(_source(index) for index in range(1, 5))
        review = _review_for_bodies(bodies)
        prepared = tuple(
            PreparedDomainDocument(
                item_id=document.item_id,
                source_digest=document.source_digest,
                derived_digest=document.derived_digest,
                page_count=document.page_count,
                body=b"%PDF-" + body[5:],
                expected_text=document.expected_text,
                required_block_types=document.required_block_types,
            )
            for document, body in zip(review.documents, bodies, strict=True)
        )
        manifest = private_pdf_uat._domain_manifest(prepared)
        origin = private_pdf_uat._representative_campaign_origin(review, manifest)
        self.assertIsInstance(origin, private_pdf_uat.PrivatePdfCampaignOrigin)
        self.assertEqual(DOMAIN_CORPUS_ID, origin.corpus_id)
        self.assertTrue(origin.representative_domain)
        self.assertEqual(DOMAIN_PROVENANCE_DIGEST, origin.provenance_digest)
        self.assertEqual(DOMAIN_TRANSFORMATION_ID, origin.transformation_id)
        self.assertEqual(
            tuple(item.source_digest for item in review.documents),
            origin.source_digests,
        )
        self.assertEqual(
            tuple(item["source_digest"] for item in manifest["documents"]),
            origin.derived_digests,
        )
        self.assertEqual(DOMAIN_PAGE_COUNTS, origin.page_counts)
        value = private_pdf_uat._campaign_origin_value(origin)
        self.assertEqual(origin, private_pdf_uat._validate_campaign_origin(value, manifest))
        self.assertRegex(private_pdf_uat._campaign_origin_digest(origin), r"^sha256:[0-9a-f]{64}$")

    def test_origin_swaps_and_authority_fields_fail_closed(self):
        import ao_lore.private_pdf_uat as private_pdf_uat

        bodies = tuple(_source(index) for index in range(1, 5))
        review = _review_for_bodies(bodies)
        prepared = tuple(
            PreparedDomainDocument(
                item_id=document.item_id,
                source_digest=document.source_digest,
                derived_digest=document.derived_digest,
                page_count=document.page_count,
                body=b"%PDF-" + body[5:],
                expected_text=document.expected_text,
                required_block_types=document.required_block_types,
            )
            for document, body in zip(review.documents, bodies, strict=True)
        )
        manifest = private_pdf_uat._domain_manifest(prepared)
        origin = private_pdf_uat._representative_campaign_origin(review, manifest)
        value = private_pdf_uat._campaign_origin_value(origin)
        mutations = (
            lambda item: item.update(corpus_id="ubuntu-pdf-seed-v1"),
            lambda item: item["source_digests"].reverse(),
            lambda item: item["derived_digests"].reverse(),
            lambda item: item["page_counts"].__setitem__(0, 2),
            lambda item: item.update(representative_domain=False),
            lambda item: item.update(authority=True),
        )
        for mutate in mutations:
            changed = deepcopy(value)
            mutate(changed)
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._validate_campaign_origin(
                    changed, manifest, expected=origin
                )


if __name__ == "__main__":
    unittest.main()
