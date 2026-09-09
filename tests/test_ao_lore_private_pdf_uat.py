import copy
import importlib.util
import hashlib
import io
import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

import ao_lore.private_pdf_uat as private_pdf_uat
from ao_lore._strict_io import ContractError, parse_strict_json, write_exclusive_json
from ao_lore.home import repository_root
from ao_lore.docling_pdf import ConvertedPdf, DoclingPdfAdapter
from ao_lore.parsing import ParseOutput, ParsingError, base_capability
from ao_lore.pdf_benchmark import PdfBenchmarkRejection
from ao_lore.private_pdf_uat import (
    PrivatePdfUatDependencies,
    PrivatePdfUatPreparedRun,
    PrivatePdfCalibrationEvidence,
    PrivatePdfUatError,
    UBUNTU_PDF_SEED,
    cleanup_private_pdf_uat,
    derive_tuning_decision,
    evaluate_private_pdf_corpus,
    load_private_pdf_manifest,
    prepare_ubuntu_pdf_seed,
    run_private_pdf_uat,
    validate_private_pdf_manifest,
    validate_private_pdf_readback,
)
from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.batch_ingestion import _reconstruct_checkpoint, load_or_initialize_checkpoint
from ao_lore.private_document_uat import private_document_policy_digest
from tests.private_calibration import private_calibration


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64
DIGEST_F = "sha256:" + "f" * 64

BLOCK_TYPES = {
    "heading",
    "paragraph",
    "list",
    "table",
    "code",
    "image",
    "caption",
    "footnote",
    "link",
}


def manifest() -> dict:
    return {
        "schema_version": "ao.lore.private-pdf-uat-manifest.v0.1",
        "corpus_id": "ubuntu-pdf-seed-v1",
        "parser_id": "docling",
        "parser_version": "2.118.1",
        "ocr_enabled": False,
        "documents": [
            {
                "item_id": "seed-01",
                "file": "input/seed-01.pdf",
                "source_digest": DIGEST_A,
                "expected_text": ["Shared MIME-info Database"],
                "required_block_types": ["heading", "paragraph"],
            }
        ],
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def unordered_manifest() -> dict:
    value = manifest()
    value["documents"] = [
        {
            "item_id": "seed-02",
            "file": "input/seed-02.pdf",
            "source_digest": DIGEST_B,
            "expected_text": ["Printer Task Information"],
            "required_block_types": ["heading", "table"],
        },
        {
            "item_id": "seed-01",
            "file": "input/seed-01.pdf",
            "source_digest": DIGEST_A,
            "expected_text": ["Shared MIME-info Database"],
            "required_block_types": ["heading", "paragraph"],
        },
    ]
    return value


def readback() -> dict:
    return {
        "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
        "corpus_id": "ubuntu-pdf-seed-v1",
        "corpus_digest": DIGEST_A,
        "configuration_digest": DIGEST_B,
        "qualification_digest": DIGEST_C,
        "aggregate_digest": DIGEST_D,
        "uat_digest": DIGEST_E,
        "campaign_origin_digest": DIGEST_F,
        "lifecycle_status": "partial",
        "tuning_decision": "hold",
        "qualified": False,
        "counts": {
            "total": 2,
            "created": 1,
            "unchanged": 0,
            "rejected": 1,
            "processed": 2,
            "successful": 1,
            "candidate_queue": 1,
            "interrupted": 1,
            "resumed": 1,
            "rerun": 1,
        },
        "conversion_counts": {
            "initial": 1,
            "resumed": 1,
            "rerun": 0,
        },
        "brain_before_digest": DIGEST_F,
        "brain_after_digest": DIGEST_F,
        "results": [
            {
                "item_id": "seed-01",
                "source_digest": DIGEST_A,
                "status": "created",
                "candidate_id": "candidate-seed-01",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "review_status": "unreviewed",
            },
            {
                "item_id": "seed-02",
                "source_digest": DIGEST_D,
                "status": "rejected",
                "error_code": "invalid_pdf",
            },
        ],
        "aggregate": {
            "metrics": {
                "structural_fidelity": 0.5,
                "text_fidelity": 1.0,
                "source_location_fidelity": 0.5,
            },
            "exclusions": [],
            "stable_failures": {
                "conversion_failed": 0,
                "digest_mismatch": 0,
                "encrypted_document": 0,
                "invalid_pdf": 1,
                "no_readable_content": 0,
            },
            "latency_seconds": {
                "minimum": 1.0,
                "maximum": 2.0,
                "mean": 1.5,
            },
            "peak_memory_bytes": {
                "minimum": 1024.0,
                "maximum": 2048.0,
                "mean": 1536.0,
            },
            "repeatability": {
                "runs": 2,
                "identical": True,
                "score": 1.0,
            },
        },
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def all_rejected_readback() -> dict:
    return {
        "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
        "corpus_id": "ubuntu-pdf-seed-v1",
        "corpus_digest": DIGEST_A,
        "configuration_digest": DIGEST_B,
        "qualification_digest": DIGEST_C,
        "aggregate_digest": DIGEST_D,
        "uat_digest": DIGEST_E,
        "campaign_origin_digest": DIGEST_F,
        "lifecycle_status": "partial",
        "tuning_decision": "investigate",
        "qualified": False,
        "counts": {
            "total": 2,
            "created": 0,
            "unchanged": 0,
            "rejected": 2,
            "processed": 2,
            "successful": 0,
            "candidate_queue": 0,
            "interrupted": 1,
            "resumed": 1,
            "rerun": 1,
        },
        "conversion_counts": {
            "initial": 1,
            "resumed": 1,
            "rerun": 0,
        },
        "brain_before_digest": DIGEST_F,
        "brain_after_digest": DIGEST_F,
        "results": [
            {
                "item_id": "seed-01",
                "source_digest": DIGEST_A,
                "status": "rejected",
                "error_code": "invalid_pdf",
            },
            {
                "item_id": "seed-02",
                "source_digest": DIGEST_B,
                "status": "rejected",
                "error_code": "no_readable_content",
            },
        ],
        "aggregate": {
            "metrics": {},
            "exclusions": [
                "structural_fidelity",
                "text_fidelity",
                "source_location_fidelity",
            ],
            "stable_failures": {
                "conversion_failed": 0,
                "digest_mismatch": 0,
                "encrypted_document": 0,
                "invalid_pdf": 1,
                "no_readable_content": 1,
            },
            "latency_seconds": {
                "minimum": 1.0,
                "maximum": 2.0,
                "mean": 1.5,
            },
            "peak_memory_bytes": {
                "minimum": 1024.0,
                "maximum": 2048.0,
                "mean": 1536.0,
            },
            "repeatability": {
                "runs": 2,
                "identical": True,
                "score": 1.0,
            },
        },
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def not_supplied_readback() -> dict:
    return {
        "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
        "lifecycle_status": "not_supplied",
        "qualified": False,
        "ocr_used": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


@private_calibration
class PrivatePdfManifestTests(unittest.TestCase):
    def test_manifest_accepts_valid_value_and_returns_detached_copy(self):
        value = manifest()
        validated = validate_private_pdf_manifest(value)
        value["documents"][0]["item_id"] = "mutated"
        self.assertEqual("seed-01", validated["documents"][0]["item_id"])

    def test_manifest_accepts_arbitrary_nonascending_order_and_preserves_it(self):
        validated = validate_private_pdf_manifest(unordered_manifest())
        self.assertEqual(["seed-02", "seed-01"], [item["item_id"] for item in validated["documents"]])

    def test_manifest_rejects_duplicate_fields_wrong_constants_and_leaky_paths(self):
        duplicate_json = (
            b'{"schema_version":"ao.lore.private-pdf-uat-manifest.v0.1",'
            b'"schema_version":"ao.lore.private-pdf-uat-manifest.v0.1"}'
        )
        with self.assertRaises(ContractError):
            parse_strict_json(duplicate_json, "private manifest")

        invalid = []
        for locator in (
            "/private/seed-01.pdf",
            r"input\seed-01.pdf",
            "input/../seed-01.pdf",
            "input/./seed-01.pdf",
            "input//seed-01.pdf",
            ".",
            "..",
            "input/seed-01.txt",
            "input/.pdf",
        ):
            value = manifest()
            value["documents"][0]["file"] = locator
            invalid.append(value)
        for field, replacement in (
            ("parser_id", "docling-beta"),
            ("parser_version", "2.118.2"),
            ("ocr_enabled", True),
            ("provider_calls", True),
            ("promotion_authority", True),
            ("claims_authority_advance", True),
        ):
            value = manifest()
            value[field] = replacement
            invalid.append(value)
        value = manifest()
        value["documents"].append(copy.deepcopy(value["documents"][0]))
        invalid.append(value)
        value = manifest()
        value["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-01.pdf",
                "source_digest": DIGEST_B,
                "expected_text": ["Another anchor"],
                "required_block_types": ["heading"],
            }
        )
        invalid.append(value)
        value = manifest()
        value["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": DIGEST_B,
                "expected_text": ["Shared MIME-info Database"],
                "required_block_types": ["heading"],
            }
        )
        invalid.append(value)
        value = manifest()
        value["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": DIGEST_B,
                "expected_text": ["Another anchor"],
                "required_block_types": ["paragraph", "heading"],
            }
        )
        invalid.append(value)
        value = manifest()
        value["documents"][0]["expected_text"] = ["Shared MIME-info Database"] * 2
        invalid.append(value)
        value = manifest()
        value["documents"][0]["required_block_types"] = ["heading", "heading"]
        invalid.append(value)
        value = manifest()
        value["documents"][0]["required_block_types"] = ["unknown"]
        invalid.append(value)
        value = manifest()
        value["documents"][0]["expected_text"] = [""]
        invalid.append(value)
        value = manifest()
        value["documents"][0]["source_digest"] = "sha256:ABC"
        invalid.append(value)
        value = manifest()
        value["documents"][0]["unexpected"] = True
        invalid.append(value)
        value = manifest()
        value["unexpected"] = True
        invalid.append(value)
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(PrivatePdfUatError):
                validate_private_pdf_manifest(changed)


@private_calibration
class PrivatePdfSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.root = Path(self.temporary.name)
        self.sources = self.root / "reviewed"
        self.sources.mkdir()
        self.runtime = self.root / "runtime"
        self.seed = []
        anchors = (
            "Shared MIME-info Database",
            "General Commands Manual",
            "TASK INFORMATION",
            "Printer test page",
        )
        block_types = (
            ("heading", "paragraph"),
            ("heading", "paragraph", "list"),
            ("heading", "table"),
            ("heading", "paragraph", "image"),
        )
        for index, (anchor, required) in enumerate(zip(anchors, block_types), 1):
            source = self.sources / f"source-{index}.pdf"
            source.write_bytes(f"%PDF-1.7\n{anchor}\n%%EOF\n".encode())
            self.seed.append(
                private_pdf_uat.SeedDocument(
                    item_id=f"seed-{index:02d}",
                    source=source,
                    expected_text=(anchor,),
                    required_block_types=required,
                )
            )
        self.commands = []

    def tearDown(self):
        self.temporary.cleanup()

    def tool(self, arguments, body):
        self.commands.append(tuple(arguments))
        if arguments[0] == "pdfinfo":
            return b"Pages: 1\nEncrypted: no\n"
        return b"readable text\n"

    def prepare(self):
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ):
            return prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def runtime_contains(self, expected):
        for directory, _, names in os.walk(self.runtime):
            for name in names:
                if (Path(directory) / name).read_bytes() == expected:
                    return True
        return False

    def test_fixed_seed_table_is_reviewed_public_ubuntu_input_only(self):
        self.assertEqual(
            [
                "/usr/share/doc/shared-mime-info/shared-mime-info-spec.pdf",
                "/usr/share/doc/printer-driver-foo2zjs/manual.pdf",
                "/usr/share/cups/data/form_english.pdf",
                "/usr/share/cups/data/default-testpage.pdf",
            ],
            [str(item.source) for item in UBUNTU_PDF_SEED],
        )
        self.assertEqual(4, len({anchor for item in UBUNTU_PDF_SEED for anchor in item.expected_text}))
        self.assertEqual(
            4,
            len({tuple(sorted(item.required_block_types)) for item in UBUNTU_PDF_SEED}),
        )
        self.assertTrue(all(item.item_id.startswith("seed-") for item in UBUNTU_PDF_SEED))

    def test_prepare_copies_verified_bytes_in_order_and_is_idempotent(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual([item.item_id for item in self.seed], [d["item_id"] for d in first["documents"]])
        self.assertEqual(
            [f"input/seed-{index:02d}.pdf" for index in range(1, 5)],
            [d["file"] for d in first["documents"]],
        )
        self.assertEqual(32, len(self.commands))
        self.assertTrue(
            all(
                command in (("pdfinfo", "-"), ("pdftotext", "-", "-"))
                for command in self.commands
            )
        )
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            loaded = load_private_pdf_manifest("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertEqual(first, loaded)
        for document, source in zip(first["documents"], self.seed):
            destination = (
                self.runtime
                / "calibration/private-pdf/ubuntu-pdf-seed-v1"
                / document["file"]
            )
            self.assertEqual(source.source.read_bytes(), destination.read_bytes())

    def test_prepare_rejects_signature_hardlink_special_oversize_and_empty(self):
        self.seed[0].source.write_bytes(b"not-a-pdf")
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.seed[0].source.write_bytes(b"%PDF-1.7\n")
        os.link(self.seed[0].source, self.sources / "second-link.pdf")
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        (self.sources / "second-link.pdf").unlink()
        self.seed[0].source.unlink()
        os.mkfifo(self.seed[0].source)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.seed[0].source.unlink()
        with self.seed[0].source.open("wb") as handle:
            handle.truncate(private_pdf_uat.MAX_PRIVATE_PDF_BYTES + 1)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_prepare_rejects_symlink_source_and_destination_hardlink(self):
        original = self.seed[0].source
        target = self.sources / "target.pdf"
        target.write_bytes(b"%PDF-1.7\nlink target\n")
        original.unlink()
        original.symlink_to(target)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        original.unlink()
        original.write_bytes(b"%PDF-1.7\nrestored\n")
        self.prepare()
        destination = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/input/seed-01.pdf"
        link = self.root / "destination-hardlink.pdf"
        os.link(destination, link)
        with self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed manifest could not be loaded$"
        ):
            load_private_pdf_manifest("ubuntu-pdf-seed-v1", runtime_root=self.runtime)

    def test_interrupted_write_removes_only_owned_temporary(self):
        real_write = os.write
        interrupted = False
        def fail_first_write(descriptor, body):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise OSError("interrupted")
            return real_write(descriptor, body)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat.os, "write", side_effect=fail_first_write), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed intake failed$"
        ):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        corpus_root = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        self.assertFalse(corpus_root.exists())
        private_root = self.runtime / "calibration/private-pdf"
        self.assertFalse(any(path.name.startswith("ubuntu-pdf-seed-stage-") for path in private_root.iterdir()))

    def test_control_interrupt_closes_all_held_runtime_descriptors(self):
        before = len(os.listdir("/proc/self/fd"))
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(
            private_pdf_uat, "_persist_bytes_at", side_effect=KeyboardInterrupt()
        ), self.assertRaises(KeyboardInterrupt):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_publish_never_replaces_destination_created_after_absence_check(self):
        real_stat = os.stat
        injected = False

        def inject_racing_destination(path, *args, **kwargs):
            nonlocal injected
            directory_descriptor = kwargs.get("dir_fd")
            if path == "seed-01.pdf" and directory_descriptor is not None and not injected:
                try:
                    return real_stat(path, *args, **kwargs)
                except FileNotFoundError:
                    injected = True
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                    try:
                        os.write(descriptor, b"foreign destination")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    raise
            return real_stat(path, *args, **kwargs)

        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat.os, "stat", side_effect=inject_racing_destination), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed intake failed$"
        ):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(b"foreign destination"))

    def test_publish_rolls_back_when_interrupted_after_no_replace_link(self):
        directory = self.root / "persist-after-link"
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        real_link = os.link

        def interrupt_after_link(*args, **kwargs):
            real_link(*args, **kwargs)
            raise KeyboardInterrupt()

        try:
            with patch.object(private_pdf_uat.os, "link", side_effect=interrupt_after_link), self.assertRaises(
                KeyboardInterrupt
            ):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertFalse((directory / "target.pdf").exists())

    def test_publish_rolls_back_when_directory_fsync_fails_after_link(self):
        directory = self.root / "persist-after-fsync"
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        real_fsync = os.fsync
        calls = 0

        def fail_second_fsync(target_descriptor):
            nonlocal calls
            calls += 1
            real_fsync(target_descriptor)
            if calls == 2:
                raise OSError("injected directory fsync failure")

        try:
            with patch.object(private_pdf_uat.os, "fsync", side_effect=fail_second_fsync), self.assertRaises(
                OSError
            ):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertFalse((directory / "target.pdf").exists())

    def test_publish_rolls_back_when_interrupted_after_temporary_unlink(self):
        directory = self.root / "persist-after-unlink"
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        real_unlink = os.unlink
        interrupted = False

        def interrupt_after_unlink(*args, **kwargs):
            nonlocal interrupted
            real_unlink(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        try:
            with patch.object(
                private_pdf_uat.os, "unlink", side_effect=interrupt_after_unlink
            ), self.assertRaises(KeyboardInterrupt):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertFalse((directory / "target.pdf").exists())

    def test_publish_rolls_back_when_verified_reopen_fails(self):
        directory = self.root / "persist-reopen"
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.object(
                private_pdf_uat,
                "_read_file_at",
                side_effect=OSError("injected verified reopen failure"),
            ), self.assertRaises(OSError):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertFalse((directory / "target.pdf").exists())

    def test_publish_rollback_preserves_foreign_replacement_at_reopen(self):
        directory = self.root / "persist-foreign-reopen"
        directory.mkdir()
        foreign = self.root / "foreign-reopen.pdf"
        foreign_body = b"foreign replacement"
        foreign.write_bytes(foreign_body)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)

        def replace_before_reopen(*args, **kwargs):
            os.replace(foreign, "target.pdf", dst_dir_fd=descriptor)
            raise OSError("injected foreign replacement")

        try:
            with patch.object(
                private_pdf_uat, "_read_file_at", side_effect=replace_before_reopen
            ), self.assertRaises(OSError):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertEqual(foreign_body, (directory / "target.pdf").read_bytes())

    def test_publish_rollback_preserves_foreign_swap_after_final_stat(self):
        directory = self.root / "persist-foreign-after-stat"
        directory.mkdir()
        foreign = self.root / "foreign-after-stat.pdf"
        foreign_body = b"sole foreign replacement"
        foreign.write_bytes(foreign_body)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        real_stat = os.stat
        swapped = False

        def swap_after_stat(path, *args, **kwargs):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if path == "target.pdf" and kwargs.get("dir_fd") == descriptor and not swapped:
                swapped = True
                os.replace(foreign, path, dst_dir_fd=descriptor)
            return result

        try:
            with patch.object(
                private_pdf_uat, "_read_file_at", side_effect=OSError("reopen failed")
            ), patch.object(private_pdf_uat.os, "stat", side_effect=swap_after_stat), self.assertRaises(OSError):
                private_pdf_uat._persist_bytes_at(descriptor, "target.pdf", b"body")
        finally:
            os.close(descriptor)
        self.assertTrue(
            any(path.is_file() and path.read_bytes() == foreign_body for path in self.root.rglob("*"))
        )

    def test_held_source_parent_survives_ancestor_swap(self):
        first = self.seed[0]
        self.seed = [first]
        original_body = first.source.read_bytes()
        held_directory = self.root / "held-reviewed"
        real_open_source = private_pdf_uat._open_source
        swapped = False
        def swap_after_open(path):
            nonlocal swapped
            opened = real_open_source(path)
            if not swapped:
                swapped = True
                self.sources.rename(held_directory)
                self.sources.mkdir()
                (self.sources / path.name).write_bytes(b"%PDF-1.7\nreplacement\n")
            return opened
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat, "_open_source", side_effect=swap_after_open):
            prepared = prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertEqual(
            "sha256:" + hashlib.sha256(original_body).hexdigest(),
            prepared["documents"][0]["source_digest"],
        )

    def test_prepare_keeps_runtime_root_descriptor_across_path_replacement(self):
        held_runtime = self.root / "held-runtime"
        real_read = private_pdf_uat._read_reviewed_source
        swapped = False

        def swap_runtime_then_read(path):
            nonlocal swapped
            if not swapped:
                swapped = True
                self.runtime.rename(held_runtime)
                self.runtime.mkdir()
                (self.runtime / "foreign-sentinel").write_text("preserve")
            return real_read(path)

        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat, "_read_reviewed_source", side_effect=swap_runtime_then_read):
            prepared = prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertEqual("ubuntu-pdf-seed-v1", prepared["corpus_id"])
        self.assertEqual(["foreign-sentinel"], sorted(path.name for path in self.runtime.iterdir()))
        self.assertTrue(
            (held_runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/manifest.json").is_file()
        )

    def test_prepare_keeps_corpus_descriptor_across_path_replacement(self):
        held_corpus = self.root / "held-corpus"
        real_persist = private_pdf_uat._persist_bytes_at
        persisted = 0

        def swap_corpus_after_documents(descriptor, name, body):
            nonlocal persisted
            real_persist(descriptor, name, body)
            persisted += 1
            if persisted == 4:
                stage = Path(os.readlink(f"/proc/self/fd/{descriptor}")).parent
                stage.rename(held_corpus)
                (stage / "input").mkdir(parents=True)
                (stage / "foreign-sentinel").write_text("preserve")

        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat, "_persist_bytes_at", side_effect=swap_corpus_after_documents), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed intake failed$"
        ):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertTrue(
            (self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/foreign-sentinel").is_file()
        )
        self.assertTrue(self.runtime_contains(b"preserve"))
        self.assertTrue((held_corpus / "manifest.json").is_file())
        self.assertEqual(4, len(list((held_corpus / "input").iterdir())))

    def test_load_keeps_runtime_root_descriptor_across_path_replacement(self):
        self.prepare()
        held_runtime = self.root / "held-load-runtime"
        real_open = private_pdf_uat._open_runtime_root

        def replace_after_open(runtime_root):
            root, descriptor = real_open(runtime_root)
            root.rename(held_runtime)
            root.mkdir()
            (root / "foreign-sentinel").write_text("preserve")
            return root, descriptor

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_open_runtime_root", side_effect=replace_after_open
        ):
            loaded = load_private_pdf_manifest(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertEqual("ubuntu-pdf-seed-v1", loaded["corpus_id"])
        self.assertEqual(["foreign-sentinel"], [path.name for path in self.runtime.iterdir()])

    def test_cleanup_keeps_private_descriptor_across_path_replacement(self):
        self.prepare()
        private_root = self.runtime / "calibration/private-pdf"
        held_private = self.root / "held-private"
        real_load = private_pdf_uat._load_private_pdf_manifest_at

        def replace_after_load(*args, **kwargs):
            value = real_load(*args, **kwargs)
            private_root.rename(held_private)
            replacement = private_root / "ubuntu-pdf-seed-v1/input"
            replacement.mkdir(parents=True)
            (replacement.parent / "foreign-sentinel").write_text("preserve")
            return value

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_load_private_pdf_manifest_at", side_effect=replace_after_load
        ):
            result = cleanup_private_pdf_uat(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertTrue(result["removed"])
        replacement_corpus = private_root / "ubuntu-pdf-seed-v1"
        self.assertEqual(
            ["foreign-sentinel", "input"],
            sorted(path.name for path in replacement_corpus.iterdir()),
        )
        self.assertFalse((held_private / "ubuntu-pdf-seed-v1").exists())

    def test_pdf_tool_uses_argument_array_timeout_and_sanitized_environment(self):
        real_popen = subprocess.Popen

        def complete(arguments, **keywords):
            return real_popen(
                ["python3", "-c", "import os; os.write(1, b'Pages: 1\\n')"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=keywords.get("start_new_session", False),
            )

        with patch.object(private_pdf_uat.subprocess, "Popen", side_effect=complete) as popen:
            self.assertEqual(b"Pages: 1\n", private_pdf_uat._run_pdf_tool(("pdfinfo", "-"), b"%PDF-"))
        arguments, keywords = popen.call_args
        self.assertEqual(["pdfinfo", "-"], arguments[0])
        self.assertNotIn("shell", keywords)
        self.assertEqual(30.0, private_pdf_uat._TOOL_TIMEOUT_SECONDS)
        self.assertIsNot(private_pdf_uat.subprocess.PIPE, keywords["stdin"])
        self.assertIs(private_pdf_uat.subprocess.PIPE, keywords["stdout"])
        self.assertTrue(keywords["close_fds"])
        self.assertTrue(keywords["start_new_session"])
        self.assertEqual(
            {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
            },
            keywords["env"],
        )

    def test_pdf_tool_terminates_process_as_soon_as_output_exceeds_ceiling(self):
        real_popen = subprocess.Popen
        processes = []

        def noisy_process(arguments, **keywords):
            process = real_popen(
                [
                    "python3",
                    "-c",
                    "import os\nwhile True: os.write(1, b'x' * 4096)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=keywords.get("start_new_session", False),
            )
            processes.append(process)
            return process

        completed = type("Completed", (), {"returncode": 0})()
        with patch.object(private_pdf_uat, "_MAX_TOOL_OUTPUT_BYTES", 8), patch.object(
            private_pdf_uat.subprocess, "run", return_value=completed
        ), patch.object(
            private_pdf_uat.subprocess, "Popen", side_effect=noisy_process
        ) as popen, self.assertRaisesRegex(OSError, "PDF verification command failed"):
            private_pdf_uat._run_pdf_tool(("pdfinfo", "-"), b"%PDF-")
        popen.assert_called_once()
        self.assertIsNotNone(processes[0].poll())

    def test_pdf_tool_terminates_process_on_timeout(self):
        real_popen = subprocess.Popen
        processes = []

        def stalled_process(arguments, **keywords):
            process = real_popen(
                ["python3", "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=keywords.get("start_new_session", False),
            )
            processes.append(process)
            return process

        with patch.object(private_pdf_uat, "_TOOL_TIMEOUT_SECONDS", 0.01), patch.object(
            private_pdf_uat.subprocess, "Popen", side_effect=stalled_process
        ), self.assertRaisesRegex(OSError, "PDF verification command failed"):
            private_pdf_uat._run_pdf_tool(("pdfinfo", "-"), b"%PDF-")
        self.assertIsNotNone(processes[0].poll())

    def test_pdf_tool_terminates_child_but_preserves_control_interrupt(self):
        real_popen = subprocess.Popen
        process = real_popen(
            ["python3", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        fake_selector = unittest.mock.MagicMock()
        fake_selector.select.side_effect = KeyboardInterrupt()
        try:
            with patch.object(private_pdf_uat.subprocess, "Popen", return_value=process), patch.object(
                private_pdf_uat.selectors, "DefaultSelector", return_value=fake_selector
            ), self.assertRaises(KeyboardInterrupt):
                private_pdf_uat._run_pdf_tool(("pdfinfo", "-"), b"%PDF-")
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_pdf_tool_terminates_forked_descendant_process_group(self):
        real_popen = subprocess.Popen
        pid_file = self.root / "grandchild.pid"
        processes = []

        def forking_process(arguments, **keywords):
            script = (
                "import os,time\n"
                "child=os.fork()\n"
                "if child == 0:\n"
                f" open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                " while True: time.sleep(1)\n"
                "while True: time.sleep(1)\n"
            )
            process = real_popen(
                ["python3", "-c", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=keywords.get("start_new_session", False),
            )
            processes.append(process)
            return process

        child_pid = None
        try:
            with patch.object(private_pdf_uat, "_TOOL_TIMEOUT_SECONDS", 0.1), patch.object(
                private_pdf_uat.subprocess, "Popen", side_effect=forking_process
            ), self.assertRaisesRegex(OSError, "PDF verification command failed"):
                private_pdf_uat._run_pdf_tool(("pdfinfo", "-"), b"%PDF-")
            for _ in range(100):
                if pid_file.exists():
                    child_pid = int(pid_file.read_text())
                    break
                time.sleep(0.01)
            self.assertIsNotNone(child_pid)
            for _ in range(100):
                status = Path(f"/proc/{child_pid}/stat")
                try:
                    state = status.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("forked PDF-tool descendant survived cleanup")
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_prepare_rejects_encrypted_invalid_pages_and_empty_text(self):
        for output in (
            b"Pages: 1\nEncrypted: yes\n",
            b"Pages: 0\nEncrypted: no\n",
            b"Pages: nope\nEncrypted: no\n",
        ):

            def invalid_info(arguments, body, value=output):
                return value if arguments[0] == "pdfinfo" else b"text"

            with self.subTest(output=output), patch.object(
                private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)
            ), patch.object(
                private_pdf_uat, "_run_pdf_tool", side_effect=invalid_info
            ), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF seed intake failed$"
            ):
                prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

        def empty_text(arguments, body):
            return b"Pages: 1\nEncrypted: no\n" if arguments[0] == "pdfinfo" else b"  \n"
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=empty_text
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_prepare_rejects_source_mutation_during_read(self):
        real_read = private_pdf_uat._read_bounded
        mutated = False
        def mutate_after_read(descriptor, maximum):
            nonlocal mutated
            body = real_read(descriptor, maximum)
            if not mutated:
                mutated = True
                self.seed[0].source.write_bytes(body + b"drift")
            return body
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(private_pdf_uat, "_read_bounded", side_effect=mutate_after_read), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed intake failed$"
        ):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_source_open_closes_file_descriptor_when_fstat_raises_oserror(self):
        real_fstat = os.fstat

        def fail_regular(descriptor):
            info = real_fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                raise OSError("injected fstat failure")
            return info

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(private_pdf_uat.os, "fstat", side_effect=fail_regular), self.assertRaises(
            OSError
        ):
            private_pdf_uat._read_reviewed_source(self.seed[0].source)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_source_open_closes_file_descriptor_when_fstat_is_interrupted(self):
        real_fstat = os.fstat

        def interrupt_regular(descriptor):
            info = real_fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                raise KeyboardInterrupt()
            return info

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(
            private_pdf_uat.os, "fstat", side_effect=interrupt_regular
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._read_reviewed_source(self.seed[0].source)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_prepare_fails_closed_on_symlink_ancestor_and_partial_collision(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.runtime.symlink_to(outside, target_is_directory=True)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.runtime.unlink()
        partial = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/input"
        partial.mkdir(parents=True)
        (partial / "seed-01.pdf").write_bytes(b"collision")
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_load_detects_manifest_and_destination_drift_without_leaking_paths(self):
        self.prepare()
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        target = corpus / "input/seed-01.pdf"
        target.write_bytes(target.read_bytes() + b"drift")
        with self.assertRaises(PrivatePdfUatError) as caught:
            load_private_pdf_manifest("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertNotIn(str(target), str(caught.exception))
        self.assertNotIn(str(self.sources), str(caught.exception))

    def test_load_rechecks_readability_and_encryption_before_returning(self):
        self.prepare()
        def encrypted(arguments, body):
            return b"Pages: 1\nEncrypted: yes\n" if arguments[0] == "pdfinfo" else b"text"
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=encrypted), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed manifest could not be loaded$"
        ):
            load_private_pdf_manifest("ubuntu-pdf-seed-v1", runtime_root=self.runtime)

    def test_prepare_rejects_existing_manifest_drift_as_conflict(self):
        self.prepare()
        manifest_path = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/manifest.json"
        value = json.loads(manifest_path.read_text())
        value["parser_version"] = "2.118.2"
        manifest_path.write_text(json.dumps(value))
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_cleanup_verifies_exact_owned_state_and_rejects_unknown_or_drift(self):
        manifest_value = self.prepare()
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        (corpus / "unknown").write_text("sentinel")
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(corpus.exists())
        (corpus / "unknown").unlink()
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertEqual(
            {"corpus_id": manifest_value["corpus_id"], "removed": True, "verified_documents": 4},
            result,
        )
        self.assertFalse(corpus.exists())
        self.assertEqual(
            {"corpus_id": "ubuntu-pdf-seed-v1", "removed": False, "verified_documents": 0},
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime),
        )

    def test_cleanup_rechecks_manifest_binding_after_validation(self):
        self.prepare()
        manifest_path = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1/manifest.json"
        real_load = private_pdf_uat._load_private_pdf_manifest_at

        def replace_after_load(*args, **kwargs):
            value = real_load(*args, **kwargs)
            manifest_path.write_text("{}")
            return value

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_load_private_pdf_manifest_at", side_effect=replace_after_load
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(b"{}"))
        self.assertTrue(self.runtime_contains(self.seed[0].source.read_bytes()))

    def test_cleanup_rejects_renamed_contract_valid_nonseed_corpus(self):
        self.prepare()
        private_root = self.runtime / "calibration/private-pdf"
        victim = private_root / "victim-corpus"
        (private_root / "ubuntu-pdf-seed-v1").rename(victim)
        manifest_path = victim / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["corpus_id"] = "victim-corpus"
        manifest_path.write_text(
            json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed cleanup failed$"
        ):
            cleanup_private_pdf_uat("victim-corpus", runtime_root=self.runtime)
        self.assertTrue(manifest_path.exists())
        self.assertEqual(4, len(list((victim / "input").iterdir())))

    def test_cleanup_rejects_foreign_hardlink_swapped_after_verified_read(self):
        self.prepare()
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        target = corpus / "input/seed-01.pdf"
        foreign = self.root / "foreign.pdf"
        foreign.write_bytes(b"%PDF-1.7\nforeign bytes\n")
        real_read = private_pdf_uat._read_file_at
        seed_reads = 0

        def swap_after_cleanup_read(descriptor, name, maximum):
            nonlocal seed_reads
            body = real_read(descriptor, name, maximum)
            if name == "seed-01.pdf":
                seed_reads += 1
                if seed_reads == 2:
                    os.link(foreign, "foreign-link", dst_dir_fd=descriptor)
                    os.replace(
                        "foreign-link",
                        name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
            return body

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_read_file_at", side_effect=swap_after_cleanup_read
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(b"%PDF-1.7\nforeign bytes\n"))
        self.assertTrue(
            (self.runtime / "calibration/private-pdf/.ubuntu-pdf-seed.cleanup-quarantine").exists()
        )

    def test_cleanup_quarantines_document_before_last_window_foreign_swap(self):
        self.prepare()
        foreign = self.root / "foreign-document.pdf"
        foreign_body = b"%PDF-1.7\nsole foreign document\n"
        foreign.write_bytes(foreign_body)
        real_read = private_pdf_uat._read_file_at
        reads = 0

        def swap_after_reclamation_read(descriptor, name, maximum):
            nonlocal reads
            body = real_read(descriptor, name, maximum)
            if name == ".delete-seed-01.pdf":
                reads += 1
                if reads == 2:
                    os.replace(foreign, name, dst_dir_fd=descriptor)
            return body

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_read_file_at", side_effect=swap_after_reclamation_read
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(foreign_body))

    def test_cleanup_quarantines_manifest_before_last_window_foreign_swap(self):
        self.prepare()
        foreign = self.root / "foreign-manifest"
        foreign_body = b"sole foreign manifest"
        foreign.write_bytes(foreign_body)
        real_read = private_pdf_uat._read_file_at
        reads = 0

        def swap_after_reclamation_read(descriptor, name, maximum):
            nonlocal reads
            body = real_read(descriptor, name, maximum)
            if name == ".delete-manifest.json":
                reads += 1
                if reads == 2:
                    os.replace(foreign, name, dst_dir_fd=descriptor)
            return body

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_read_file_at", side_effect=swap_after_reclamation_read
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(foreign_body))

    def test_cleanup_preserves_foreign_swap_after_quarantine_read(self):
        self.prepare()
        foreign = self.root / "post-read-foreign.pdf"
        foreign_body = b"%PDF-1.7\npost-read sole foreign\n"
        foreign.write_bytes(foreign_body)
        real_read = private_pdf_uat._read_file_at
        swapped = False

        def swap_after_quarantine_read(descriptor, name, maximum):
            nonlocal swapped
            body = real_read(descriptor, name, maximum)
            if name.startswith(".delete-") and not swapped:
                swapped = True
                os.replace(foreign, name, dst_dir_fd=descriptor)
            return body

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_read_file_at", side_effect=swap_after_quarantine_read
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed cleanup failed$"):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(self.runtime_contains(foreign_body))

    def test_prepare_retry_succeeds_after_interrupted_document_publication(self):
        real_persist = private_pdf_uat._persist_bytes_at
        for interrupt_after in range(1, 6):
            with self.subTest(interrupt_after=interrupt_after):
                runtime = self.root / f"retry-after-{interrupt_after}"
                runtime.mkdir()
                calls = 0

                def interrupt_after_publication(*args, **kwargs):
                    nonlocal calls
                    real_persist(*args, **kwargs)
                    calls += 1
                    if calls == interrupt_after:
                        raise KeyboardInterrupt()

                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ), patch.object(private_pdf_uat, "_persist_bytes_at", side_effect=interrupt_after_publication), self.assertRaises(KeyboardInterrupt):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    result = prepare_ubuntu_pdf_seed(runtime_root=runtime)
                self.assertEqual("ubuntu-pdf-seed-v1", result["corpus_id"])
                private_root = runtime / "calibration/private-pdf"
                self.assertFalse(any(path.name.startswith("ubuntu-pdf-seed-stage-") for path in private_root.iterdir()))

    def test_cleanup_retry_recovers_interrupted_corpus_quarantine(self):
        self.prepare()
        real_rename = private_pdf_uat._rename_noreplace_at
        interrupted = False

        def interrupt_after_corpus_rename(source_fd, source, destination_fd, destination):
            nonlocal interrupted
            real_rename(source_fd, source, destination_fd, destination)
            if source == "ubuntu-pdf-seed-v1" and not interrupted:
                interrupted = True
                raise OSError("injected after corpus quarantine")

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_rename_noreplace_at", side_effect=interrupt_after_corpus_rename
        ), self.assertRaises(PrivatePdfUatError):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(result["removed"])

    def test_cleanup_retry_recovers_after_first_document_quarantine(self):
        self.prepare()
        real_quarantine = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def interrupt_after_first_file(*args, **kwargs):
            nonlocal interrupted
            real_quarantine(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise OSError("injected after document quarantine")

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat, "_quarantine_verified_file_at", side_effect=interrupt_after_first_file
        ), self.assertRaises(PrivatePdfUatError):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(result["removed"])

    def test_three_prepare_cleanup_cycles_leave_only_durable_lock(self):
        private_root = self.runtime / "calibration/private-pdf"
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                self.prepare()
                with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                    result = cleanup_private_pdf_uat(
                        "ubuntu-pdf-seed-v1", runtime_root=self.runtime
                    )
                self.assertTrue(result["removed"])
                self.assertEqual(
                    [private_pdf_uat._LOCK_NAME],
                    sorted(path.name for path in private_root.iterdir()),
                )

    def test_keyboard_interrupt_after_first_document_move_recovers_next_call(self):
        self.prepare()
        real_move = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def interrupt_after_first_move(*args, **kwargs):
            nonlocal interrupted
            real_move(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat,
            "_quarantine_verified_file_at",
            side_effect=interrupt_after_first_move,
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertTrue(result["removed"])
        private_root = self.runtime / "calibration/private-pdf"
        self.assertEqual(
            [private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private_root.iterdir()),
        )

    def test_prepare_refuses_unresolved_cleanup_recovery(self):
        self.prepare()
        real_move = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def interrupt_after_first_move(*args, **kwargs):
            nonlocal interrupted
            real_move(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat,
            "_quarantine_verified_file_at",
            side_effect=interrupt_after_first_move,
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_recovery_bundle_rejects_unknown_entry_and_remains_bounded(self):
        self.prepare()
        real_move = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def interrupt_after_first_move(*args, **kwargs):
            nonlocal interrupted
            real_move(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat,
            "_quarantine_verified_file_at",
            side_effect=interrupt_after_first_move,
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        private_root = self.runtime / "calibration/private-pdf"
        recoveries = [
            path for path in private_root.iterdir()
            if path.name.startswith(private_pdf_uat._RECOVERY_PREFIX)
        ]
        self.assertEqual(1, len(recoveries))
        (recoveries[0] / "files/unknown").write_bytes(b"foreign")
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed cleanup failed$"
        ):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertEqual(b"foreign", (recoveries[0] / "files/unknown").read_bytes())
        self.assertEqual(
            1,
            len(
                [path for path in private_root.iterdir() if path.name.startswith(private_pdf_uat._RECOVERY_PREFIX)]
            ),
        )

    def test_cleanup_recovers_interrupt_before_recovery_schema_creation(self):
        self.prepare()
        real_open = os.open
        interrupted = False

        def interrupt_before_schema(path, flags, *args, **kwargs):
            nonlocal interrupted
            if path == private_pdf_uat._RECOVERY_SCHEMA_NAME and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return real_open(path, flags, *args, **kwargs)

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat.os, "open", side_effect=interrupt_before_schema
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertTrue(result["removed"])
        private_root = self.runtime / "calibration/private-pdf"
        self.assertEqual(
            [private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private_root.iterdir()),
        )

    def test_recovery_staging_accepts_only_absent_or_canonical_schema_prefixes(self):
        for cut in (None, 0, 1, "middle", "last", "full"):
            with self.subTest(cut=cut):
                runtime = self.root / f"prefix-{cut}"
                runtime.mkdir()
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    manifest = prepare_ubuntu_pdf_seed(runtime_root=runtime)
                private_root = runtime / "calibration/private-pdf"
                staging = private_root / private_pdf_uat._RECOVERY_STAGING
                (staging / "files").mkdir(parents=True)
                expected = private_pdf_uat._recovery_body(
                    private_pdf_uat._recovery_value(manifest)
                )
                positions = {
                    0: 0,
                    1: 1,
                    "middle": len(expected) // 2,
                    "last": len(expected) - 1,
                    "full": len(expected),
                }
                if cut is not None:
                    (staging / private_pdf_uat._RECOVERY_SCHEMA_NAME).write_bytes(
                        expected[: positions[cut]]
                    )
                descriptor = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                        recovered = private_pdf_uat._reclaim_recovery_staging_at(descriptor)
                finally:
                    os.close(descriptor)
                self.assertEqual(manifest, recovered)
                self.assertFalse(staging.exists())

    def test_recovery_staging_rejects_foreign_schema_bytes_and_preserves_them(self):
        variants = ("first", "middle", "extra")
        for variant in variants:
            with self.subTest(variant=variant):
                runtime = self.root / f"foreign-schema-{variant}"
                runtime.mkdir()
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    manifest = prepare_ubuntu_pdf_seed(runtime_root=runtime)
                private_root = runtime / "calibration/private-pdf"
                staging = private_root / private_pdf_uat._RECOVERY_STAGING
                (staging / "files").mkdir(parents=True)
                expected = private_pdf_uat._recovery_body(
                    private_pdf_uat._recovery_value(manifest)
                )
                if variant == "extra":
                    foreign = expected + b"x"
                else:
                    index = 0 if variant == "first" else len(expected) // 2
                    foreign = expected[:index] + bytes([expected[index] ^ 1]) + expected[index + 1 :]
                schema = staging / private_pdf_uat._RECOVERY_SCHEMA_NAME
                schema.write_bytes(foreign)
                descriptor = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaises(OSError):
                        private_pdf_uat._reclaim_recovery_staging_at(descriptor)
                finally:
                    os.close(descriptor)
                self.assertEqual(foreign, schema.read_bytes())

    def test_recovery_staging_missing_input_closes_corpus_descriptor(self):
        self.prepare()
        private_root = self.runtime / "calibration/private-pdf"
        corpus = private_root / "ubuntu-pdf-seed-v1"
        (private_root / private_pdf_uat._RECOVERY_STAGING).mkdir()
        (corpus / "input").rename(corpus / "held-input")
        descriptor = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY)
        before = len(os.listdir("/proc/self/fd"))
        try:
            with self.assertRaises(FileNotFoundError):
                private_pdf_uat._reclaim_recovery_staging_at(descriptor)
            self.assertEqual(before, len(os.listdir("/proc/self/fd")))
        finally:
            os.close(descriptor)
        self.assertTrue((corpus / "held-input").is_dir())

    def test_recovery_staging_second_open_interrupt_closes_corpus_descriptor(self):
        self.prepare()
        private_root = self.runtime / "calibration/private-pdf"
        (private_root / private_pdf_uat._RECOVERY_STAGING).mkdir()
        descriptor = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY)
        real_open = os.open

        def interrupt_input_open(path, *args, **kwargs):
            if path == "input":
                raise KeyboardInterrupt()
            return real_open(path, *args, **kwargs)

        before = len(os.listdir("/proc/self/fd"))
        try:
            with patch.object(private_pdf_uat.os, "open", side_effect=interrupt_input_open), self.assertRaises(KeyboardInterrupt):
                private_pdf_uat._reclaim_recovery_staging_at(descriptor)
            self.assertEqual(before, len(os.listdir("/proc/self/fd")))
        finally:
            os.close(descriptor)
        self.assertTrue((private_root / private_pdf_uat._RECOVERY_STAGING).is_dir())

    def test_cleanup_resumes_after_each_final_reclamation_step(self):
        steps = (
            ".delete-seed-01.pdf",
            ".delete-seed-02.pdf",
            ".delete-seed-03.pdf",
            ".delete-seed-04.pdf",
            ".delete-manifest.json",
            private_pdf_uat._RECOVERY_SCHEMA_NAME,
            "files-dir",
            "recovery-dir",
            "files-fsync",
        )
        for step in steps:
            with self.subTest(step=step):
                runtime = self.root / f"reclaim-{step.replace('/', '-') }"
                runtime.mkdir()
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                real_unlink = os.unlink
                real_rmdir = os.rmdir
                real_fsync = os.fsync
                interrupted = False
                first_bundle_unlinked = False

                def interrupt_unlink(path, *args, **kwargs):
                    nonlocal interrupted, first_bundle_unlinked
                    result = real_unlink(path, *args, **kwargs)
                    if path == ".delete-seed-01.pdf":
                        first_bundle_unlinked = True
                    if path == step and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                def interrupt_rmdir(path, *args, **kwargs):
                    nonlocal interrupted
                    result = real_rmdir(path, *args, **kwargs)
                    is_target = step == "files-dir" and path == "files"
                    is_target = is_target or (
                        step == "recovery-dir"
                        and isinstance(path, str)
                        and path.startswith(private_pdf_uat._RECOVERY_PREFIX)
                        and path != private_pdf_uat._RECOVERY_STAGING
                    )
                    if is_target and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                def interrupt_fsync(descriptor):
                    nonlocal interrupted
                    result = real_fsync(descriptor)
                    if step == "files-fsync" and first_bundle_unlinked and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                with patch.object(private_pdf_uat.os, "unlink", side_effect=interrupt_unlink), patch.object(
                    private_pdf_uat.os, "rmdir", side_effect=interrupt_rmdir
                ), patch.object(private_pdf_uat.os, "fsync", side_effect=interrupt_fsync), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ), self.assertRaises(KeyboardInterrupt):
                    cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=runtime)
                self.assertTrue(interrupted)
                with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                    resumed = cleanup_private_pdf_uat(
                        "ubuntu-pdf-seed-v1", runtime_root=runtime
                    )
                self.assertTrue(resumed["removed"])
                private_root = runtime / "calibration/private-pdf"
                self.assertEqual(
                    [private_pdf_uat._LOCK_NAME],
                    sorted(path.name for path in private_root.iterdir()),
                )
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                    cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=runtime)

    def test_cleanup_missing_source_before_reclaim_marker_stays_failed_closed(self):
        self.prepare()
        real_move = private_pdf_uat._quarantine_verified_file_at
        interrupted = False

        def interrupt_after_first_move(*args, **kwargs):
            nonlocal interrupted
            real_move(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat,
            "_quarantine_verified_file_at",
            side_effect=interrupt_after_first_move,
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        private_root = self.runtime / "calibration/private-pdf"
        quarantine = private_root / private_pdf_uat._CLEANUP_QUARANTINE
        held = self.root / "held-missing-source"
        quarantine.rename(held)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed cleanup failed$"
        ):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertTrue(held.is_dir())
        self.assertEqual([], [path for path in private_root.iterdir() if path.name.startswith(private_pdf_uat._RECLAIM_PREFIX)])

    def test_cleanup_recovers_marker_publish_link_return_interrupt(self):
        self.prepare()
        real_link = os.link
        interrupted = False

        def interrupt_after_real_link(*args, **kwargs):
            nonlocal interrupted
            result = real_link(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_pdf_uat.os, "link", side_effect=interrupt_after_real_link), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ):
            result = cleanup_private_pdf_uat(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertTrue(result["removed"])
        self.assertFalse(interrupted)
        private_root = self.runtime / "calibration/private-pdf"
        self.assertEqual(
            [private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private_root.iterdir()),
        )

    def test_cleanup_recovers_each_reclaim_marker_publication_interrupt(self):
        phases = (
            "create",
            "write",
            "file-fsync",
            "dir-fsync-before",
            "publish-before",
            "publish-after",
            "dir-fsync-after",
            "reopen",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                runtime = self.root / f"marker-{phase}"
                runtime.mkdir()
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                real_open = os.open
                real_write = os.write
                real_fsync = os.fsync
                real_rename = private_pdf_uat._rename_noreplace_at
                real_read = private_pdf_uat._read_file_at
                marker_written = False
                marker_fsyncs = 0
                interrupted = False

                def interrupt_open(path, *args, **kwargs):
                    nonlocal interrupted
                    descriptor = real_open(path, *args, **kwargs)
                    if path == private_pdf_uat._RECLAIM_STAGING and phase == "create":
                        os.close(descriptor)
                        interrupted = True
                        raise KeyboardInterrupt()
                    return descriptor

                def interrupt_write(descriptor, body):
                    nonlocal interrupted, marker_written
                    result = real_write(descriptor, body)
                    marker_written = True
                    if phase == "write" and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                def interrupt_fsync(descriptor):
                    nonlocal interrupted, marker_fsyncs
                    result = real_fsync(descriptor)
                    if marker_written:
                        marker_fsyncs += 1
                        target = {
                            "file-fsync": 1,
                            "dir-fsync-before": 2,
                            "dir-fsync-after": 3,
                        }.get(phase)
                        if target == marker_fsyncs and not interrupted:
                            interrupted = True
                            raise KeyboardInterrupt()
                    return result

                def interrupt_rename(source_fd, source, destination_fd, destination):
                    nonlocal interrupted
                    if source == private_pdf_uat._RECLAIM_STAGING and phase == "publish-before":
                        interrupted = True
                        raise KeyboardInterrupt()
                    real_rename(source_fd, source, destination_fd, destination)
                    if source == private_pdf_uat._RECLAIM_STAGING and phase == "publish-after":
                        interrupted = True
                        raise KeyboardInterrupt()

                def interrupt_reopen(descriptor, name, maximum):
                    nonlocal interrupted
                    body = real_read(descriptor, name, maximum)
                    if name.startswith(private_pdf_uat._RECLAIM_PREFIX) and phase == "reopen" and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return body

                with patch.object(private_pdf_uat.os, "open", side_effect=interrupt_open), patch.object(
                    private_pdf_uat.os, "write", side_effect=interrupt_write
                ), patch.object(private_pdf_uat.os, "fsync", side_effect=interrupt_fsync), patch.object(
                    private_pdf_uat, "_rename_noreplace_at", side_effect=interrupt_rename
                ), patch.object(private_pdf_uat, "_read_file_at", side_effect=interrupt_reopen), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ), self.assertRaises(KeyboardInterrupt):
                    cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=runtime)
                self.assertTrue(interrupted)
                with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                    resumed = cleanup_private_pdf_uat(
                        "ubuntu-pdf-seed-v1", runtime_root=runtime
                    )
                self.assertTrue(resumed["removed"])
                self.assertEqual(
                    [private_pdf_uat._LOCK_NAME],
                    sorted(
                        path.name
                        for path in (runtime / "calibration/private-pdf").iterdir()
                    ),
                )

    def test_foreign_reclaim_marker_staging_is_preserved_and_blocks_cleanup(self):
        self.prepare()
        real_write = os.write
        interrupted = False

        def interrupt_marker_write(descriptor, body):
            nonlocal interrupted
            result = real_write(descriptor, body)
            binding = os.readlink(f"/proc/self/fd/{descriptor}")
            if binding.endswith(private_pdf_uat._RECLAIM_STAGING) and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_pdf_uat.os, "write", side_effect=interrupt_marker_write), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        staging = (
            self.runtime
            / "calibration/private-pdf"
            / private_pdf_uat._RECLAIM_STAGING
        )
        foreign = b"foreign marker staging"
        staging.write_bytes(foreign)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF seed cleanup failed$"
        ):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertEqual(foreign, staging.read_bytes())

    def test_prepare_rejects_unknown_marker_rollback_artifact(self):
        self.prepare()
        private_root = self.runtime / "calibration/private-pdf"
        artifact = private_root / ".rollback-foreign.quarantine"
        artifact.write_bytes(b"foreign")
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        self.assertEqual(b"foreign", artifact.read_bytes())

    def test_cleanup_recovers_interrupt_after_atomic_recovery_publish(self):
        self.prepare()
        real_rename = private_pdf_uat._rename_noreplace_at
        interrupted = False

        def interrupt_after_publish(source_fd, source, destination_fd, destination):
            nonlocal interrupted
            real_rename(source_fd, source, destination_fd, destination)
            if source == ".ubuntu-pdf-seed-recovery-staging" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.object(
            private_pdf_uat,
            "_rename_noreplace_at",
            side_effect=interrupt_after_publish,
        ), self.assertRaises(KeyboardInterrupt):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            result = cleanup_private_pdf_uat(
                "ubuntu-pdf-seed-v1", runtime_root=self.runtime
            )
        self.assertTrue(result["removed"])

    def test_cleanup_recovers_interrupt_at_each_recovery_creation_stage(self):
        phases = (
            "stage-mkdir",
            "files-mkdir",
            "schema-write",
            "fsync-1",
            "fsync-2",
            "fsync-3",
            "fsync-4",
            "publish-before",
            "publish-after",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                runtime = self.root / f"recovery-stage-{phase}"
                runtime.mkdir()
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                real_mkdir = os.mkdir
                real_write = os.write
                real_fsync = os.fsync
                real_rename = private_pdf_uat._rename_noreplace_at
                schema_written = False
                recovery_fsyncs = 0
                interrupted = False

                def interrupt_mkdir(path, *args, **kwargs):
                    nonlocal interrupted
                    result = real_mkdir(path, *args, **kwargs)
                    if path == private_pdf_uat._RECOVERY_STAGING and phase == "stage-mkdir":
                        interrupted = True
                        raise KeyboardInterrupt()
                    if path == "files" and phase == "files-mkdir":
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                def interrupt_write(descriptor, body):
                    nonlocal interrupted, schema_written
                    result = real_write(descriptor, body)
                    schema_written = True
                    if phase == "schema-write" and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return result

                def interrupt_fsync(descriptor):
                    nonlocal interrupted, recovery_fsyncs
                    result = real_fsync(descriptor)
                    if schema_written:
                        recovery_fsyncs += 1
                        if phase == f"fsync-{recovery_fsyncs}" and not interrupted:
                            interrupted = True
                            raise KeyboardInterrupt()
                    return result

                def interrupt_publish(source_fd, source, destination_fd, destination):
                    nonlocal interrupted
                    if source == private_pdf_uat._RECOVERY_STAGING and phase == "publish-before":
                        interrupted = True
                        raise KeyboardInterrupt()
                    real_rename(source_fd, source, destination_fd, destination)
                    if source == private_pdf_uat._RECOVERY_STAGING and phase == "publish-after":
                        interrupted = True
                        raise KeyboardInterrupt()

                with patch.object(private_pdf_uat.os, "mkdir", side_effect=interrupt_mkdir), patch.object(
                    private_pdf_uat.os, "write", side_effect=interrupt_write
                ), patch.object(private_pdf_uat.os, "fsync", side_effect=interrupt_fsync), patch.object(
                    private_pdf_uat, "_rename_noreplace_at", side_effect=interrupt_publish
                ), patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaises(KeyboardInterrupt):
                    cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=runtime)
                self.assertTrue(interrupted)
                with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
                    result = cleanup_private_pdf_uat(
                        "ubuntu-pdf-seed-v1", runtime_root=runtime
                    )
                self.assertTrue(result["removed"])
                self.assertEqual(
                    [private_pdf_uat._LOCK_NAME],
                    sorted(
                        path.name
                        for path in (runtime / "calibration/private-pdf").iterdir()
                    ),
                )

    def test_repeated_prepare_interrupts_do_not_grow_rollback_tombstones(self):
        private_root = self.runtime / "calibration/private-pdf"
        real_persist = private_pdf_uat._persist_bytes_at
        for attempt in range(3):
            calls = 0

            def interrupt_after_first_document(*args, **kwargs):
                nonlocal calls
                real_persist(*args, **kwargs)
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt()

            with self.subTest(attempt=attempt), patch.object(
                private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)
            ), patch.object(
                private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
            ), patch.object(
                private_pdf_uat,
                "_persist_bytes_at",
                side_effect=interrupt_after_first_document,
            ), self.assertRaises(KeyboardInterrupt):
                prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
            self.assertEqual(
                [private_pdf_uat._LOCK_NAME],
                sorted(path.name for path in private_root.iterdir()),
            )
        self.prepare()
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool):
            cleanup_private_pdf_uat("ubuntu-pdf-seed-v1", runtime_root=self.runtime)
        self.assertEqual(
            [private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private_root.iterdir()),
        )

    def test_prepare_rollback_drift_uses_one_bounded_recovery_and_blocks_retry(self):
        real_persist = private_pdf_uat._persist_bytes_at
        calls = 0

        def interrupt_after_first_document(*args, **kwargs):
            nonlocal calls
            real_persist(*args, **kwargs)
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()

        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.object(
            private_pdf_uat, "_persist_bytes_at", side_effect=interrupt_after_first_document
        ), patch.object(
            private_pdf_uat, "_unlink_verified_at", side_effect=OSError("injected drift")
        ), self.assertRaises(KeyboardInterrupt):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)
        private_root = self.runtime / "calibration/private-pdf"
        self.assertEqual(
            [private_pdf_uat._PREPARATION_RECOVERY, private_pdf_uat._LOCK_NAME],
            sorted(path.name for path in private_root.iterdir()),
        )
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF seed intake failed$"):
            prepare_ubuntu_pdf_seed(runtime_root=self.runtime)

    def test_prepare_retry_succeeds_when_canonical_publish_is_interrupted(self):
        real_rename = private_pdf_uat._rename_noreplace_at
        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                runtime = self.root / f"canonical-{timing}"
                runtime.mkdir()
                interrupted = False

                def interrupt_canonical(source_fd, source, destination_fd, destination):
                    nonlocal interrupted
                    is_publish = destination == "ubuntu-pdf-seed-v1" and source.startswith(
                        "ubuntu-pdf-seed-stage-"
                    )
                    if is_publish and not interrupted and timing == "before":
                        interrupted = True
                        raise KeyboardInterrupt()
                    real_rename(source_fd, source, destination_fd, destination)
                    if is_publish and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()

                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ), patch.object(private_pdf_uat, "_rename_noreplace_at", side_effect=interrupt_canonical), self.assertRaises(KeyboardInterrupt):
                    prepare_ubuntu_pdf_seed(runtime_root=runtime)
                with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
                    private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
                ):
                    result = prepare_ubuntu_pdf_seed(runtime_root=runtime)
                self.assertEqual("ubuntu-pdf-seed-v1", result["corpus_id"])

    def test_lock_rejects_path_replacement_after_flock(self):
        private = self.root / "lock-private"
        private.mkdir()
        private_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY)
        real_flock = private_pdf_uat.fcntl.flock
        replacement_fd = None

        def replace_after_flock(descriptor, operation):
            nonlocal replacement_fd
            real_flock(descriptor, operation)
            os.unlink(private_pdf_uat._LOCK_NAME, dir_fd=private_fd)
            replacement_fd = os.open(
                private_pdf_uat._LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=private_fd,
            )
            real_flock(replacement_fd, private_pdf_uat.fcntl.LOCK_EX | private_pdf_uat.fcntl.LOCK_NB)

        try:
            with patch.object(private_pdf_uat.fcntl, "flock", side_effect=replace_after_flock), self.assertRaises(OSError):
                private_pdf_uat._open_private_lock(private_fd, exclusive=True)
        finally:
            if replacement_fd is not None:
                os.close(replacement_fd)
            os.close(private_fd)

    def test_runtime_root_must_remain_inside_repository_and_errors_are_redacted(self):
        with patch.object(private_pdf_uat, "UBUNTU_PDF_SEED", tuple(self.seed)), patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=RuntimeError("/private/sentinel.pdf")
        ), self.assertRaises(PrivatePdfUatError) as caught:
            prepare_ubuntu_pdf_seed(runtime_root=Path("/tmp/ao-lore-private-escape"))
        self.assertEqual("private PDF seed intake failed", str(caught.exception))
        self.assertNotIn("sentinel", str(caught.exception))


class CalibrationAdapter:
    def __init__(self, *, drift=False, failure=None, failure_item=None, malformed=False, binding_mutation=None, failures=None, mutate_source=False, network_then=None, network_method="create_connection", result_mutation=None):
        self.capability = {
            "parser_id": "docling",
            "version": "2.118.1",
            "supported_mime_types": ["application/pdf"],
            "supported_extensions": [".pdf"],
            "ocr_capable": False,
            "deterministic_offline": True,
            "frontier_service_required": False,
            "network_access": False,
            "provider_calls": False,
            "expected_monetary_cost": 0,
            "max_file_bytes": private_pdf_uat.MAX_PRIVATE_PDF_BYTES,
        }
        self.calibration_configuration = {
            "configuration_digest": private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            "offline": True,
            "network_access": False,
            "ocr_enabled": False,
            "provider_calls": False,
        }
        self.calls = []
        self.drift = drift
        self.failure = failure
        self.failure_item = failure_item
        self.malformed = malformed
        self.binding_mutation = binding_mutation
        self.failures = list(failures or ())
        self.mutate_source = mutate_source
        self.network_then = network_then
        self.network_method = network_method
        self.result_mutation = result_mutation

    def parse(self, source):
        self.calls.append((source["resource"], source["digest"], source["data"]))
        if self.network_then is not None:
            try:
                if self.network_method == "create_connection":
                    socket.create_connection(("127.0.0.1", 9), timeout=0.01)
                else:
                    connection = socket.socket()
                    try:
                        getattr(connection, self.network_method)(("127.0.0.1", 9))
                    finally:
                        connection.close()
            except Exception:
                if isinstance(self.network_then, BaseException):
                    raise self.network_then
            raise ParsingError("network attempt unexpectedly returned")
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        if self.mutate_source:
            source["digest"] = DIGEST_A
        if self.failure is not None and (
            self.failure_item is None or Path(source["resource"]).stem == self.failure_item
        ):
            raise self.failure
        if self.malformed:
            return {"private": "/private/sentinel.pdf"}
        item_id = Path(source["resource"]).stem
        anchors = {
            "seed-01": "Alpha private anchor",
            "seed-02": "Beta private anchor",
        }
        types = {"seed-01": ("heading", "paragraph"), "seed-02": ("heading", "table")}
        text = anchors[item_id] + (" drift" if self.drift and len(self.calls) % 2 == 0 else "")
        blocks = [
            {
                "id": f"b{index}",
                "type": block_type,
                "text": text,
                "source_span": {"start": 0, "end": len(text), "page": 1},
            }
            for index, block_type in enumerate(types[item_id], 1)
        ]
        document_ir = {
                "schema_version": "ao.lore.document-ir.v0.1",
                "document_id": source["digest"],
                "source": {key: source[key] for key in ("resource", "digest", "media_type")},
                "parser": {
                    "parser_id": "docling",
                    "parser_version": "2.118.1",
                    "configuration_digest": private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
                },
                "blocks": blocks,
                "metadata": {
                    "format": "pdf",
                    "page_count": 1,
                    "ocr_used": False,
                    "configuration_digest": private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
                },
            }
        if self.binding_mutation == "source":
            document_ir["source"]["digest"] = DIGEST_A
        elif self.binding_mutation == "parser":
            document_ir["parser"]["parser_id"] = "foreign"
        elif self.binding_mutation == "configuration":
            document_ir["parser"]["configuration_digest"] = DIGEST_B
        elif self.binding_mutation == "ocr":
            document_ir["metadata"]["ocr_used"] = True
        output = ParseOutput(
            document_ir,
            (
                {"text_coverage": True}
                if self.binding_mutation == "quality"
                else {
                    "text_coverage": 1.0,
                    "structural_completeness": 1.0,
                    "source_span_coverage": 1.0,
                    "document_ir_valid": 1.0,
                }
            ),
        )
        mutation = self.result_mutation
        if mutation == "provider_calls":
            document_ir["provider_calls"] = False
        elif mutation == "network_accessed":
            document_ir["network_accessed"] = False
        elif mutation == "unknown_result_field":
            object.__setattr__(
                output, "unknown_result_field", "/private/result-sentinel"
            )
        elif mutation == "source_unknown":
            document_ir["source"]["private_locator"] = "/private/source-sentinel"
        elif mutation == "block_unknown":
            document_ir["blocks"][0]["provider_calls"] = False
        elif mutation == "parser_unknown":
            document_ir["parser"]["authority"] = True
        elif mutation == "metadata_unknown":
            document_ir["metadata"]["network_accessed"] = False
        elif mutation == "duplicate_block":
            document_ir["blocks"][1]["id"] = document_ir["blocks"][0]["id"]
        elif mutation == "block_order":
            document_ir["blocks"][0]["id"] = "b2"
        elif mutation == "malformed_span":
            document_ir["blocks"][0]["source_span"]["start"] = True
        elif mutation == "unknown_attribute_authority":
            document_ir["blocks"][0]["attributes"] = {"promotion_authority": False}
        elif mutation == "private_attribute_locator":
            document_ir["blocks"][0]["attributes"] = {
                "target": "/private/attribute-sentinel"
            }
        return output


@private_calibration
class PrivatePdfCalibrationTests(unittest.TestCase):
    def setUp(self):
        # Real Docling integration tests can leave tqdm/Torch daemon threads in
        # unittest's shared process. These unit tests model the dedicated
        # calibration worker's exclusive process; the explicit thread-safety
        # regression below temporarily restores the real enumeration.
        self.thread_enumeration = patch.object(
            private_pdf_uat.threading,
            "enumerate",
            side_effect=lambda: [threading.current_thread()],
        )
        self.thread_enumeration.start()
        self.addCleanup(self.thread_enumeration.stop)
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root() / "working")
        self.runtime = Path(self.temporary.name)
        self.corpus_root = self.runtime / "calibration" / "private-pdf" / "ubuntu-pdf-seed-v1"
        (self.corpus_root / "input").mkdir(parents=True)
        self.bodies = {
            "seed-01": b"%PDF-1.4\nalpha\n%%EOF\n",
            "seed-02": b"%PDF-1.4\nbeta\n%%EOF\n",
        }
        self.value = manifest()
        self.value["documents"] = [
            {
                "item_id": "seed-01",
                "file": "input/seed-01.pdf",
                "source_digest": "sha256:" + hashlib.sha256(self.bodies["seed-01"]).hexdigest(),
                "expected_text": ["Alpha private anchor"],
                "required_block_types": ["heading", "paragraph"],
            },
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": "sha256:" + hashlib.sha256(self.bodies["seed-02"]).hexdigest(),
                "expected_text": ["Beta private anchor"],
                "required_block_types": ["heading", "table"],
            },
        ]
        for item_id, body in self.bodies.items():
            (self.corpus_root / "input" / f"{item_id}.pdf").write_bytes(body)
        (self.corpus_root / "manifest.json").write_text(
            json.dumps(self.value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def tool(command, _body):
        return b"Pages: 1\nEncrypted: no\n" if command[0] == "pdfinfo" else b"readable"

    def evaluate(self, adapter=None, *, measurement=None, value=None):
        if measurement is None:
            values = iter(((0.1, 100), (0.2, 120), (0.3, 140), (0.4, 160)))

            def measurement(operation):
                output = operation()
                duration, peak = next(values)
                return output, duration, peak
        offline = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), patch.dict(
            os.environ, offline, clear=False
        ):
            return evaluate_private_pdf_corpus(
                self.value if value is None else value,
                adapter=adapter or CalibrationAdapter(),
                corpus_root=self.corpus_root,
                measurement=measurement,
            )

    def test_evaluates_twice_in_manifest_order_and_projects_strict_aggregate(self):
        adapter = CalibrationAdapter()
        report = self.evaluate(adapter)
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["fixtures/seed-01.pdf", "fixtures/seed-01.pdf", "fixtures/seed-02.pdf", "fixtures/seed-02.pdf"],
        )
        self.assertEqual([call[1] for call in adapter.calls], [self.value["documents"][0]["source_digest"]] * 2 + [self.value["documents"][1]["source_digest"]] * 2)
        self.assertEqual([call[2] for call in adapter.calls], [self.bodies["seed-01"]] * 2 + [self.bodies["seed-02"]] * 2)
        self.assertEqual(set(report), {
            "metrics", "exclusions", "stable_failures", "latency_seconds",
            "peak_memory_bytes", "repeatability",
        })
        self.assertEqual(report["metrics"], {
            "source_location_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "text_fidelity": 1.0,
        })
        self.assertEqual(report["exclusions"], [])
        self.assertTrue(all(value == 0 for value in report["stable_failures"].values()))
        self.assertEqual(report["latency_seconds"], {"minimum": 0.1, "maximum": 0.4, "mean": 0.25})
        self.assertEqual(report["peak_memory_bytes"], {"minimum": 100, "maximum": 160, "mean": 130.0})
        self.assertEqual(report["repeatability"], {"runs": 2, "identical": True, "score": 1.0})
        self.assertEqual(derive_tuning_decision(report), "hold")
        with open("schemas/ao-lore/private-pdf-uat-readback-v0.1.schema.json", encoding="utf-8") as handle:
            readback_schema = json.load(handle)
        aggregate_schema = {
            "$schema": readback_schema["$schema"],
            "$defs": readback_schema["$defs"],
            "$ref": "#/$defs/aggregate",
        }
        Draft202012Validator(aggregate_schema).validate(report)
        serialized = json.dumps(report, sort_keys=True)
        for forbidden in ("qualified", "ocr", "network", "provider", "promotion", "authority", "sha256:"):
            self.assertNotIn(forbidden, serialized)

    def test_result_is_deterministic_for_identical_measurements(self):
        with patch.object(private_pdf_uat, "run_pdf_benchmark", wraps=private_pdf_uat.run_pdf_benchmark) as runner:
            first = self.evaluate()
        translated = runner.call_args.args[1]
        self.assertEqual(
            translated["configuration"]["max_file_size"],
            private_pdf_uat.MAX_PRIVATE_PDF_BYTES,
        )
        self.assertEqual(
            private_pdf_uat.configuration_digest(
                translated["configuration"]["max_file_size"],
                translated["configuration"]["max_num_pages"],
                translated["configuration"]["max_blocks"],
            ),
            private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
        )
        second = self.evaluate()
        self.assertEqual(first, second)
        self.assertEqual(runner.call_args.kwargs["fixture_data"], {
            "seed-01.pdf": self.bodies["seed-01"],
            "seed-02.pdf": self.bodies["seed-02"],
        })

    def test_repeatability_drift_investigates_without_authority(self):
        report = self.evaluate(CalibrationAdapter(drift=True))
        self.assertEqual(report["repeatability"], {"runs": 2, "identical": False, "score": 0.0})
        self.assertEqual(derive_tuning_decision(report), "investigate")
        self.assertNotIn("candidate_change", json.dumps(report))

    def test_adapter_failures_are_closed_and_private_material_never_escapes(self):
        sentinel = "/private/customer.pdf Alpha private anchor EXTRACTED-SECRET"
        report = self.evaluate(CalibrationAdapter(failure=ParsingError(sentinel)))
        serialized = json.dumps(report, sort_keys=True, separators=(",", ":"))
        for private in (sentinel, "/private", "customer.pdf", "Alpha private anchor", "Beta private anchor", "EXTRACTED-SECRET", "manifest.json"):
            self.assertNotIn(private, serialized)
        self.assertEqual(report["metrics"], {})
        self.assertEqual(set(report["exclusions"]), set(private_pdf_uat._METRIC_NAMES))
        self.assertEqual(report["stable_failures"]["conversion_failed"], 2)
        self.assertEqual(derive_tuning_decision(report), "investigate")

    def test_partial_failure_is_counted_once_after_both_attempts(self):
        adapter = CalibrationAdapter(
            failure=ParsingError("/private/do-not-leak.pdf"), failure_item="seed-02"
        )
        report = self.evaluate(adapter)
        self.assertEqual(len(adapter.calls), 4)
        self.assertEqual(report["stable_failures"]["conversion_failed"], 1)
        self.assertNotEqual(report["metrics"], {})
        self.assertEqual(derive_tuning_decision(report), "investigate")

    def test_adapter_cannot_mutate_expected_source_binding(self):
        adapter = CalibrationAdapter(mutate_source=True)
        with self.assertRaisesRegex(PrivatePdfUatError, "private PDF calibration failed"):
            self.evaluate(adapter)
        self.assertTrue(adapter.calls)
        self.assertTrue(all(call[1] != DIGEST_A for call in adapter.calls))

    def test_two_attempt_failures_must_be_stable_and_complete(self):
        unstable = CalibrationAdapter(
            failures=[
                PdfBenchmarkRejection("invalid_pdf"),
                PdfBenchmarkRejection("encrypted_document"),
            ]
        )
        mixed = CalibrationAdapter(
            failures=[PdfBenchmarkRejection("invalid_pdf"), None]
        )
        for adapter in (unstable, mixed):
            with self.subTest(adapter=adapter), self.assertRaisesRegex(
                PrivatePdfUatError, "private PDF calibration failed"
            ):
                self.evaluate(adapter)

    def test_requires_exact_offline_environment_and_safe_capability(self):
        offline = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        for key in offline:
            changed = dict(offline)
            changed[key] = "0"
            with self.subTest(environment=key), patch.object(
                private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
            ), patch.dict(os.environ, changed, clear=False), self.assertRaisesRegex(
                PrivatePdfUatError, "private PDF calibration failed"
            ):
                evaluate_private_pdf_corpus(
                    self.value,
                    adapter=CalibrationAdapter(),
                    corpus_root=self.corpus_root,
                    measurement=lambda operation: (operation(), 0.1, 1),
                )
        with patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            PrivatePdfUatError, "private PDF calibration failed"
        ):
            evaluate_private_pdf_corpus(
                self.value,
                adapter=CalibrationAdapter(),
                corpus_root=self.corpus_root,
                measurement=lambda operation: (operation(), 0.1, 1),
            )
        mutations = {
            "deterministic_offline": False,
            "ocr_capable": True,
            "frontier_service_required": True,
            "expected_monetary_cost": 1,
            "max_file_bytes": 1,
            "network_access": True,
            "provider_calls": True,
        }
        for field, unsafe in mutations.items():
            adapter = CalibrationAdapter()
            adapter.capability[field] = unsafe
            with self.subTest(capability=field), self.assertRaisesRegex(
                PrivatePdfUatError, "private PDF calibration failed"
            ):
                self.evaluate(adapter)
        for field, unsafe in (
            ("offline", False),
            ("network_access", True),
            ("ocr_enabled", True),
            ("provider_calls", True),
            ("configuration_digest", DIGEST_A),
        ):
            adapter = CalibrationAdapter()
            adapter.calibration_configuration[field] = unsafe
            with self.subTest(configuration=field), self.assertRaisesRegex(
                PrivatePdfUatError, "private PDF calibration failed"
            ):
                self.evaluate(adapter)

    def test_socket_guard_blocks_attempts_and_restores_on_all_exit_paths(self):
        original_create_connection = socket.create_connection
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        cases = (
            ("create_connection", None, PrivatePdfUatError),
            ("connect", RuntimeError("after blocked network"), PrivatePdfUatError),
            ("connect_ex", KeyboardInterrupt(), KeyboardInterrupt),
        )
        for method, after, expected in cases:
            adapter = CalibrationAdapter(
                network_then=after if after is not None else False,
                network_method=method,
            )
            with self.subTest(method=method, after=after), self.assertRaises(expected):
                self.evaluate(adapter)
            self.assertIs(socket.create_connection, original_create_connection)
            self.assertIs(socket.socket.connect, original_connect)
            self.assertIs(socket.socket.connect_ex, original_connect_ex)

    def test_fails_closed_on_manifest_adapter_output_measurement_and_digest_drift(self):
        cases = []
        changed = copy.deepcopy(self.value)
        changed["documents"].reverse()
        cases.append((CalibrationAdapter(), changed, None))
        for duration, peak in (
            (True, 1), (float("nan"), 1), (-1.0, 1), (0.1, True), (0.1, -1), (0.1, 1.5)
        ):
            cases.append((CalibrationAdapter(), None, lambda operation, duration=duration, peak=peak: (operation(), duration, peak)))
        cases.append((CalibrationAdapter(malformed=True), None, None))
        for mutation in ("source", "parser", "configuration", "ocr", "quality"):
            cases.append((CalibrationAdapter(binding_mutation=mutation), None, None))
        cases.append((CalibrationAdapter(), None, object()))
        for adapter, value, measurement in cases:
            with self.subTest(
                value=value,
                measurement=measurement,
                malformed=adapter.malformed,
                mutation=adapter.binding_mutation,
            ), self.assertRaisesRegex(PrivatePdfUatError, "private PDF calibration failed"):
                self.evaluate(adapter, value=value, measurement=measurement)
        bad_identity = CalibrationAdapter()
        bad_identity.capability = {"parser_id": "foreign", "version": "2.118.1"}
        with self.assertRaisesRegex(PrivatePdfUatError, "private PDF calibration failed"):
            self.evaluate(bad_identity)
        (self.corpus_root / "input" / "seed-01.pdf").write_bytes(b"%PDF-1.4\ndrift\n%%EOF\n")
        with self.assertRaisesRegex(PrivatePdfUatError, "private PDF calibration failed"):
            self.evaluate()

    def test_keyboard_interrupt_and_base_exception_are_not_reclassified(self):
        for failure in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(failure=failure), self.assertRaises(type(failure)):
                self.evaluate(CalibrationAdapter(failure=failure))

    def test_corpus_must_use_the_fixed_private_calibration_layout(self):
        wrong_root = self.runtime / "elsewhere" / self.value["corpus_id"]
        wrong_root.parent.mkdir()
        self.corpus_root.rename(wrong_root)
        with patch.object(private_pdf_uat, "_run_pdf_tool", side_effect=self.tool), self.assertRaisesRegex(
            PrivatePdfUatError, "private PDF calibration failed"
        ):
            evaluate_private_pdf_corpus(
                self.value,
                adapter=CalibrationAdapter(),
                corpus_root=wrong_root,
                measurement=lambda operation: (operation(), 0.1, 1),
            )

    def test_tuning_decision_rejects_forged_aggregate_values(self):
        aggregate = self.evaluate()
        changes = []
        unknown = copy.deepcopy(aggregate)
        unknown["candidate_change"] = True
        changes.append(unknown)
        nested_exclusion = copy.deepcopy(aggregate)
        nested_exclusion["exclusions"] = [["text_fidelity"]]
        changes.append(nested_exclusion)
        bool_failure = copy.deepcopy(aggregate)
        bool_failure["stable_failures"]["conversion_failed"] = True
        changes.append(bool_failure)
        nonfinite = copy.deepcopy(aggregate)
        nonfinite["latency_seconds"]["mean"] = float("inf")
        changes.append(nonfinite)
        contradiction = copy.deepcopy(aggregate)
        contradiction["repeatability"]["identical"] = False
        changes.append(contradiction)
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(PrivatePdfUatError):
                derive_tuning_decision(changed)

    def test_rejects_unknown_and_malformed_parse_output_shape(self):
        mutations = (
            "provider_calls",
            "network_accessed",
            "unknown_result_field",
            "source_unknown",
            "block_unknown",
            "parser_unknown",
            "metadata_unknown",
            "duplicate_block",
            "block_order",
            "malformed_span",
            "unknown_attribute_authority",
            "private_attribute_locator",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(
                    PrivatePdfUatError, "^private PDF calibration failed$"
                ) as caught:
                    self.evaluate(CalibrationAdapter(result_mutation=mutation))
                self.assertNotIn("sentinel", str(caught.exception))

    def _quality_output(self):
        source = {
            "resource": "fixtures/seed-01.pdf",
            "digest": self.value["documents"][0]["source_digest"],
            "media_type": "application/pdf",
            "data": self.bodies["seed-01"],
        }
        blocks = [
            {
                "id": "b1",
                "type": "paragraph",
                "text": "one",
                "source_span": {"start": 0, "end": 3, "page": 1},
            },
            {
                "id": "b2",
                "type": "paragraph",
                "text": "two",
                "source_span": {"start": 4, "end": 7},
            },
        ]
        output = CalibrationAdapter().parse(source)
        output.document_ir["blocks"] = blocks
        output.quality_components.update(
            {
                "text_coverage": 1.0,
                "structural_completeness": 0.5,
                "source_span_coverage": 0.5,
                "document_ir_valid": 1.0,
            }
        )
        return source, output

    def test_rejects_each_forged_quality_component_above_and_below_emitter_value(self):
        cases = {
            "text_coverage": (0.9, 1.0000000000000002),
            "structural_completeness": (0.4, 0.6),
            "source_span_coverage": (0.4, 0.6),
            "document_ir_valid": (0.9, 1.0000000000000002),
        }
        for component, values in cases.items():
            for value in values:
                source, output = self._quality_output()
                output.quality_components[component] = value
                with self.subTest(component=component, value=value), self.assertRaisesRegex(
                    ParsingError, "^private PDF calibration result binding is invalid$"
                ):
                    private_pdf_uat._validate_private_parse_output(output, source)

    def test_accepts_exact_quality_for_paragraph_mixed_located_and_unlocated_blocks(self):
        source, output = self._quality_output()
        del output.document_ir["blocks"][0]["source_span"]["page"]
        output.quality_components["source_span_coverage"] = 0.0
        private_pdf_uat._validate_private_parse_output(output, source)
        output.document_ir["blocks"][0]["source_span"]["page"] = 1
        output.quality_components["source_span_coverage"] = 0.5
        output.document_ir["blocks"][1]["type"] = "heading"
        output.quality_components["structural_completeness"] = 1.0
        private_pdf_uat._validate_private_parse_output(output, source)
        output.document_ir["blocks"][1]["source_span"]["coordinates"] = [0.0, 0.0, 1.0, 1.0]
        output.quality_components["source_span_coverage"] = 1.0
        private_pdf_uat._validate_private_parse_output(output, source)

    def test_accepts_quality_from_real_docling_emitter_formula(self):
        class Backend:
            version = "2.118.1"
            allowed_formats = ("pdf",)
            ocr_enabled = False

            def convert(self, *_args, **_kwargs):
                return ConvertedPdf(
                    status="success",
                    page_count=2,
                    blocks=(
                        {"type": "paragraph", "text": "one", "page": 1},
                        {"type": "heading", "text": "two"},
                    ),
                )

        adapter = object.__new__(DoclingPdfAdapter)
        adapter._backend = Backend()
        adapter._max_file_size = private_pdf_uat.MAX_PRIVATE_PDF_BYTES
        adapter._max_num_pages = private_pdf_uat.PRIVATE_PDF_MAX_PAGES
        adapter._max_blocks = private_pdf_uat.PRIVATE_PDF_MAX_BLOCKS
        adapter._configuration_digest = private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST
        adapter._capability = base_capability(
            "docling", "2.118.1", ["application/pdf"], [".pdf"], ["headings"]
        )
        source = {
            "resource": "fixtures/seed-01.pdf",
            "digest": self.value["documents"][0]["source_digest"],
            "media_type": "application/pdf",
            "data": self.bodies["seed-01"],
        }
        output = adapter.parse(source)
        self.assertEqual(output.quality_components, {
            "text_coverage": 1.0,
            "structural_completeness": 1.0,
            "source_span_coverage": 0.5,
            "document_ir_valid": 1.0,
        })
        private_pdf_uat._validate_private_parse_output(output, source)

    def test_measurement_seam_must_observe_exactly_once_and_preserve_identity(self):
        def zero(_operation):
            return object(), 0.1, 1

        def twice(operation):
            first = operation()
            operation()
            return first, 0.1, 1

        def replacement(operation):
            output = operation()
            return copy.deepcopy(output), 0.1, 1

        def catch_and_forge(operation):
            try:
                operation()
            except Exception:
                return object(), 0.1, 1
            raise AssertionError("operation was expected to fail")

        for measurement, adapter in (
            (zero, CalibrationAdapter()),
            (twice, CalibrationAdapter()),
            (replacement, CalibrationAdapter()),
            (catch_and_forge, CalibrationAdapter(failure=ParsingError("private"))),
        ):
            with self.subTest(measurement=measurement.__name__), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF calibration failed$"
            ):
                self.evaluate(adapter, measurement=measurement)

    def test_measurement_cannot_rewrite_wrong_text_or_snapshot_fields(self):
        class WrongTextAdapter(CalibrationAdapter):
            def parse(self, source):
                output = super().parse(source)
                output.document_ir["blocks"][0]["text"] = "wrong text"
                return output

        def rewrite_anchor(operation):
            output = operation()
            output.document_ir["blocks"][0]["text"] = "Alpha private anchor"
            return output, 0.1, 1

        with self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF calibration failed$"
        ):
            self.evaluate(WrongTextAdapter(), measurement=rewrite_anchor)

        def mutation(field):
            def mutate(operation):
                output = operation()
                if field == "text":
                    output.document_ir["blocks"][0]["text"] += " forged"
                elif field == "block_type":
                    output.document_ir["blocks"][0]["type"] = "paragraph"
                elif field == "parser":
                    output.document_ir["parser"]["parser_id"] = "foreign"
                elif field == "source":
                    output.document_ir["source"]["digest"] = DIGEST_A
                elif field == "metadata":
                    output.document_ir["metadata"]["page_count"] = 2
                elif field == "quality":
                    output.quality_components["text_coverage"] = 0.5
                return output, 0.1, 1

            return mutate

        for field in ("text", "block_type", "parser", "source", "metadata", "quality"):
            with self.subTest(field=field), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF calibration failed$"
            ):
                self.evaluate(measurement=mutation(field))

    def test_measurement_rejects_hostile_numeric_subclasses_before_late_mutation(self):
        hostile_calls = []

        class MutatingPeak(int):
            def __new__(cls, output):
                value = super().__new__(cls, 1)
                value.output = output
                return value

            def __lt__(self, other):
                hostile_calls.append("peak.__lt__")
                self.output.document_ir["blocks"][0]["text"] = "forged"
                return False

            def __int__(self):
                hostile_calls.append("peak.__int__")
                self.output.document_ir["blocks"][0]["type"] = "table"
                return 1

        class MutatingDuration(float):
            def __new__(cls, output):
                value = super().__new__(cls, 0.1)
                value.output = output
                return value

            def __float__(self):
                hostile_calls.append("duration.__float__")
                self.output.document_ir["blocks"][0]["text"] = "forged"
                return 0.1

            def __lt__(self, other):
                hostile_calls.append("duration.__lt__")
                self.output.document_ir["blocks"][0]["type"] = "table"
                return False

        def hostile_peak(operation):
            output = operation()
            return output, 0.1, MutatingPeak(output)

        def hostile_duration(operation):
            output = operation()
            return output, MutatingDuration(output), 1

        for measurement in (hostile_peak, hostile_duration):
            with self.subTest(measurement=measurement.__name__), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF calibration failed$"
            ):
                self.evaluate(measurement=measurement)
        self.assertEqual(hostile_calls, [])

    def test_snapshots_reused_and_post_operation_mutated_outputs(self):
        source, live = self._quality_output()
        snapshot = private_pdf_uat._validate_private_parse_output(live, source)
        live.document_ir["blocks"][0]["text"] = "post-return mutation"
        live.quality_components["text_coverage"] = 0.0
        self.assertEqual(snapshot.document_ir["blocks"][0]["text"], "one")
        self.assertEqual(snapshot.quality_components["text_coverage"], 1.0)

        class ReusingAdapter(CalibrationAdapter):
            def __init__(self):
                super().__init__()
                self.output = None

            def parse(self, source):
                if self.output is None:
                    self.output = super().parse(source)
                else:
                    self.calls.append((source["resource"], source["digest"], source["data"]))
                    self.output.document_ir["blocks"][0]["text"] += " drift"
                    self.output.document_ir["source"] = {
                        key: source[key] for key in ("resource", "digest", "media_type")
                    }
                    self.output.document_ir["document_id"] = source["digest"]
                return self.output

        reused = self.evaluate(ReusingAdapter())
        self.assertEqual(reused["repeatability"]["score"], 0.0)
        self.assertEqual(derive_tuning_decision(reused), "investigate")

        sequence = iter((" first", " second", " third", " fourth"))

        def mutate_after_operation(operation):
            output = operation()
            output.document_ir["blocks"][0]["text"] += next(sequence)
            return output, 0.1, 1

        with self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF calibration failed$"
        ):
            self.evaluate(measurement=mutate_after_operation)

    def test_retained_measurement_references_cannot_rewrite_prior_attempts(self):
        class WrongThenCorrectAdapter(CalibrationAdapter):
            def __init__(self):
                super().__init__()
                self.attempts = {}

            def parse(self, source):
                output = super().parse(source)
                resource = source["resource"]
                self.attempts[resource] = self.attempts.get(resource, 0) + 1
                if self.attempts[resource] == 1:
                    for block in output.document_ir["blocks"]:
                        block["text"] = "wrong"
                return output

        honest = self.evaluate(WrongThenCorrectAdapter())
        self.assertEqual(honest["metrics"]["text_fidelity"], 0.0)
        self.assertEqual(honest["repeatability"]["score"], 0.0)
        self.assertEqual(derive_tuning_decision(honest), "investigate")

        retained = {}

        def forge_prior_attempt(operation):
            output = operation()
            resource = output.document_ir["source"]["resource"]
            if resource in retained:
                prior = retained[resource]
                prior.document_ir["blocks"] = output.document_ir["blocks"]
                prior.quality_components["text_coverage"] = 0.0
            else:
                retained[resource] = output
                for prior_resource, prior in retained.items():
                    if prior_resource != resource:
                        prior.document_ir["blocks"][0]["text"] = "cross-fixture forgery"
            return output, 0.1, 1

        forged = self.evaluate(
            WrongThenCorrectAdapter(), measurement=forge_prior_attempt
        )
        for field in (
            "metrics", "exclusions", "stable_failures", "repeatability"
        ):
            self.assertEqual(forged[field], honest[field])
        self.assertEqual(derive_tuning_decision(forged), "investigate")

    def test_tuning_decision_enforces_existing_fidelity_floors(self):
        aggregate = self.evaluate()
        for score, expected in (
            (0.0, "investigate"),
            (0.499999999999, "investigate"),
            (0.5, "hold"),
            (0.500000000001, "hold"),
            (1.0, "hold"),
        ):
            changed = copy.deepcopy(aggregate)
            changed["metrics"] = {
                name: score for name in private_pdf_uat._METRIC_NAMES
            }
            with self.subTest(score=score):
                self.assertEqual(derive_tuning_decision(changed), expected)
                representative = (
                    "candidate_change" if score < 0.5 else "hold"
                )
                self.assertEqual(
                    derive_tuning_decision(
                        changed, representative_domain=True
                    ),
                    representative,
                )
        excluded = copy.deepcopy(aggregate)
        del excluded["metrics"]["source_location_fidelity"]
        excluded["exclusions"] = ["source_location_fidelity"]
        self.assertEqual(derive_tuning_decision(excluded), "hold")
        failed = copy.deepcopy(aggregate)
        failed["stable_failures"]["invalid_pdf"] = 1
        self.assertEqual(derive_tuning_decision(failed), "investigate")
        repeated = copy.deepcopy(aggregate)
        repeated["repeatability"] = {"runs": 2, "identical": False, "score": 0.0}
        self.assertEqual(derive_tuning_decision(repeated), "investigate")
        self.assertEqual(
            derive_tuning_decision(failed, representative_domain=True),
            "investigate",
        )
        self.assertEqual(
            derive_tuning_decision(repeated, representative_domain=True),
            "investigate",
        )
        with self.assertRaises(PrivatePdfUatError):
            derive_tuning_decision(aggregate, representative_domain=1)

    def test_adapter_getter_is_guarded_and_preexisting_thread_fails_before_patch(self):
        original_create_connection = socket.create_connection
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        class NetworkCapabilityAdapter:
            def __init__(self):
                self.delegate = CalibrationAdapter()
                self._capability = self.delegate.capability
                self.calibration_configuration = self.delegate.calibration_configuration
                self.guard_blocked = False

            @property
            def capability(self):
                try:
                    socket.create_connection(("127.0.0.1", 9), timeout=0.01)
                except OSError as exc:
                    self.guard_blocked = (
                        str(exc) == "private PDF calibration network is disabled"
                    )
                    raise
                return self._capability

            def parse(self, source):
                return self.delegate.parse(source)

        guarded_adapter = NetworkCapabilityAdapter()
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF calibration failed$"):
            self.evaluate(guarded_adapter)
        self.assertTrue(guarded_adapter.guard_blocked)
        self.assertIs(socket.create_connection, original_create_connection)

        stop = threading.Event()
        started = threading.Event()

        def unrelated():
            started.set()
            stop.wait()

        thread = threading.Thread(target=unrelated)
        thread.start()
        started.wait(1)
        self.thread_enumeration.stop()
        try:
            with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF calibration failed$"):
                self.evaluate(CalibrationAdapter())
            self.assertIs(socket.create_connection, original_create_connection)
            self.assertIs(socket.socket.connect, original_connect)
            self.assertIs(socket.socket.connect_ex, original_connect_ex)
        finally:
            stop.set()
            thread.join(1)
            self.thread_enumeration.start()

    def test_reentrant_evaluation_fails_and_restores_global_guards(self):
        adapter = CalibrationAdapter()
        real_validate = private_pdf_uat._validate_private_calibration_adapter
        entered = False

        def reenter(value):
            nonlocal entered
            if not entered:
                entered = True
                self.evaluate(CalibrationAdapter())
            return real_validate(value)

        original_create_connection = socket.create_connection
        with patch.object(
            private_pdf_uat,
            "_validate_private_calibration_adapter",
            side_effect=reenter,
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF calibration failed$"):
            self.evaluate(adapter)
        self.assertIs(socket.create_connection, original_create_connection)

    def test_default_measurement_restores_tracemalloc_on_base_exception(self):
        tracing_before = tracemalloc.is_tracing()
        offline = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        with patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.dict(os.environ, offline, clear=False), self.assertRaises(
            KeyboardInterrupt
        ):
            evaluate_private_pdf_corpus(
                self.value,
                adapter=CalibrationAdapter(failure=KeyboardInterrupt()),
                corpus_root=self.corpus_root,
            )
        self.assertEqual(tracemalloc.is_tracing(), tracing_before)

    def test_preexisting_tracemalloc_is_rejected_without_stopping_it(self):
        self.assertFalse(tracemalloc.is_tracing())
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF calibration failed$"
            ):
                self.evaluate(CalibrationAdapter())
            self.assertTrue(tracemalloc.is_tracing())
        finally:
            tracemalloc.stop()

    def test_calibration_rejects_a_corpus_above_the_explicit_aggregate_byte_cap(self):
        with patch.object(
            private_pdf_uat,
            "_PRIVATE_CALIBRATION_MAX_TOTAL_BYTES",
            sum(len(body) for body in self.bodies.values()) - 1,
        ), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF calibration failed$"
        ):
            self.evaluate(CalibrationAdapter())

    def test_worker_item_deadline_is_unswallowable_by_the_benchmark(self):
        class HangingCalibrationAdapter(CalibrationAdapter):
            def parse(self, source):
                time.sleep(0.2)
                return super().parse(source)

        offline = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        with patch.object(
            private_pdf_uat, "_run_pdf_tool", side_effect=self.tool
        ), patch.dict(os.environ, offline, clear=False), self.assertRaises(
            private_pdf_uat._PrivateCalibrationDeadline
        ):
            evaluate_private_pdf_corpus(
                self.value,
                adapter=HangingCalibrationAdapter(),
                corpus_root=self.corpus_root,
                item_timeout_seconds=0.01,
            )


@private_calibration
class PrivatePdfSealedModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.root = Path(self.temporary.name)
        self.original = self.root / "cache" / "huggingface"
        self.state = self.root / "uat" / "private-pdf" / "batch-test"
        for model in (
            "models--docling-project--docling-layout-heron",
            "models--docling-project--docling-models",
        ):
            blob = self.original / "hub" / model / "blobs" / "model"
            snapshot = self.original / "hub" / model / "snapshots" / "revision"
            blob.parent.mkdir(parents=True, exist_ok=True)
            snapshot.mkdir(parents=True, exist_ok=True)
            blob.write_bytes((model + "\n").encode())
            (snapshot / "model.bin").symlink_to("../../blobs/model")
        self.state.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_seal_dereferences_internal_links_and_binds_exact_tree(self):
        sealed = private_pdf_uat._seal_private_pdf_model_cache(
            self.original, self.state
        )
        self.assertEqual(4, sealed.file_count)
        self.assertEqual(
            canonical_digest(sealed.tree_manifest), sealed.tree_digest
        )
        private_pdf_uat._validate_sealed_private_pdf_model_cache(sealed)
        self.assertEqual(0o555, stat.S_IMODE(sealed.root.stat().st_mode))
        for path in sealed.root.rglob("*"):
            self.assertFalse(path.is_symlink())
            self.assertEqual(
                0o555 if path.is_dir() else 0o444,
                stat.S_IMODE(path.stat().st_mode),
            )

    def test_intake_rejects_directory_external_and_hard_links(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        cases = []

        directory_link = self.original / "directory-link"
        directory_link.symlink_to(self.original / "hub", target_is_directory=True)
        cases.append(directory_link)

        external_link = self.original / "external-link"
        external_link.symlink_to(outside)
        cases.append(external_link)

        source = self.original / "hardlink-source"
        source.write_bytes(b"hardlink")
        hardlink = self.original / "hardlink-peer"
        os.link(source, hardlink)
        cases.append(hardlink)

        for unsafe in cases:
            with self.subTest(unsafe=unsafe.name), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF model cache is invalid$"
            ):
                private_pdf_uat._inspect_private_pdf_model_cache(self.original)
            if unsafe.is_symlink():
                unsafe.unlink()
            elif unsafe == hardlink:
                hardlink.unlink()
                source.unlink()

    def test_intake_rejects_special_mutation_and_each_bound(self):
        fifo = self.original / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._inspect_private_pdf_model_cache(self.original)
        fifo.unlink()

        with patch.object(private_pdf_uat, "PRIVATE_PDF_CACHE_MAX_DEPTH", 2):
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._inspect_private_pdf_model_cache(self.original)
        with patch.object(private_pdf_uat, "PRIVATE_PDF_CACHE_MAX_FILES", 1):
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._inspect_private_pdf_model_cache(self.original)
        with patch.object(private_pdf_uat, "PRIVATE_PDF_CACHE_MAX_TOTAL_BYTES", 1):
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._inspect_private_pdf_model_cache(self.original)

        real_read = private_pdf_uat._hash_cache_file_descriptor
        mutated = False

        def mutate(descriptor, maximum):
            nonlocal mutated
            result = real_read(descriptor, maximum)
            if not mutated:
                mutated = True
                target = next((self.original / "hub").rglob("model"))
                target.chmod(0o600)
            return result

        with patch.object(
            private_pdf_uat, "_hash_cache_file_descriptor", side_effect=mutate
        ), self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._inspect_private_pdf_model_cache(self.original)

    def test_sealed_mutation_fails_and_interrupted_copy_leaves_no_residue(self):
        sealed = private_pdf_uat._seal_private_pdf_model_cache(
            self.original, self.state
        )
        target = next(path for path in sealed.root.rglob("model") if path.is_file())
        target.chmod(0o644)
        with self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF sealed model cache drifted$"
        ):
            private_pdf_uat._validate_sealed_private_pdf_model_cache(sealed)
        target.write_bytes(b"drift")
        with self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF sealed model cache drifted$"
        ):
            private_pdf_uat._validate_sealed_private_pdf_model_cache(sealed)

        with patch.object(
            private_pdf_uat,
            "_copy_cache_file",
            side_effect=KeyboardInterrupt(),
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._seal_private_pdf_model_cache(self.original, self.state)
        self.assertEqual([], list(self.state.glob("sealed-cache-*.partial")))

    def test_verified_sealed_cache_cleanup_leaves_no_residue(self):
        sealed = private_pdf_uat._seal_private_pdf_model_cache(
            self.original, self.state
        )
        private_pdf_uat._remove_sealed_private_pdf_model_cache(sealed)
        self.assertEqual([], list(self.state.glob("sealed-cache-*")))

    def test_verified_sealed_cache_cleanup_preserves_a_swapped_container(self):
        sealed = private_pdf_uat._seal_private_pdf_model_cache(
            self.original, self.state
        )
        owned = sealed.container.with_name(sealed.container.name + ".owned")
        original_validate = private_pdf_uat._validate_sealed_private_pdf_model_cache

        def swap_after_validation(value):
            result = original_validate(value)
            sealed.container.rename(owned)
            sealed.container.mkdir()
            (sealed.container / "foreign.txt").write_bytes(b"preserve")
            return result

        with patch.object(
            private_pdf_uat,
            "_validate_sealed_private_pdf_model_cache",
            side_effect=swap_after_validation,
        ), self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._remove_sealed_private_pdf_model_cache(sealed)
        self.assertEqual(
            b"preserve", (sealed.container / "foreign.txt").read_bytes()
        )
        self.assertTrue(owned.is_dir())

    def test_seal_collision_preserves_foreign_partial_and_final_directories(self):
        token = "1" * 24
        for suffix in (".partial", ""):
            with self.subTest(suffix=suffix):
                foreign = self.state / f"sealed-cache-{token}{suffix}"
                foreign.mkdir()
                (foreign / "foreign.txt").write_bytes(b"preserve")
                with patch.object(
                    private_pdf_uat.secrets, "token_hex", return_value=token
                ), self.assertRaises(OSError):
                    private_pdf_uat._seal_private_pdf_model_cache(
                        self.original, self.state
                    )
                self.assertEqual(b"preserve", (foreign / "foreign.txt").read_bytes())
                shutil.rmtree(foreign)

    def test_seal_base_exception_removes_only_the_owned_partial_binding(self):
        token = "2" * 24
        with patch.object(
            private_pdf_uat.secrets, "token_hex", return_value=token
        ), patch.object(
            private_pdf_uat, "_copy_cache_file", side_effect=KeyboardInterrupt()
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._seal_private_pdf_model_cache(self.original, self.state)
        self.assertFalse((self.state / f"sealed-cache-{token}.partial").exists())
        self.assertFalse((self.state / f"sealed-cache-{token}").exists())

    def test_seal_cleanup_preserves_a_foreign_replacement_of_owned_partial(self):
        token = "3" * 24
        partial = self.state / f"sealed-cache-{token}.partial"
        displaced = self.state / f"sealed-cache-{token}.owned-displaced"

        def replace_then_interrupt(*_args):
            partial.rename(displaced)
            partial.mkdir()
            (partial / "foreign.txt").write_bytes(b"preserve")
            raise KeyboardInterrupt()

        with patch.object(
            private_pdf_uat.secrets, "token_hex", return_value=token
        ), patch.object(
            private_pdf_uat, "_copy_cache_file", side_effect=replace_then_interrupt
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._seal_private_pdf_model_cache(self.original, self.state)
        self.assertEqual(b"preserve", (partial / "foreign.txt").read_bytes())
        shutil.rmtree(partial)
        shutil.rmtree(displaced)

    def test_seal_cleanup_preserves_a_foreign_replacement_of_owned_final(self):
        token = "4" * 24
        final = self.state / f"sealed-cache-{token}"
        displaced = self.state / f"sealed-cache-{token}.owned-displaced"

        def replace_then_interrupt(_sealed):
            final.rename(displaced)
            final.mkdir()
            (final / "foreign.txt").write_bytes(b"preserve")
            raise KeyboardInterrupt()

        with patch.object(
            private_pdf_uat.secrets, "token_hex", return_value=token
        ), patch.object(
            private_pdf_uat,
            "_validate_sealed_private_pdf_model_cache",
            side_effect=replace_then_interrupt,
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._seal_private_pdf_model_cache(self.original, self.state)
        self.assertEqual(b"preserve", (final / "foreign.txt").read_bytes())
        shutil.rmtree(final)
        os.chmod(displaced, 0o700)
        for directory in displaced.rglob("*"):
            if directory.is_dir():
                os.chmod(directory, 0o700)
        shutil.rmtree(displaced)


@private_calibration
class PrivatePdfUatHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.runtime = Path(self.temporary.name) / "runtime"
        for model in (
            "models--docling-project--docling-layout-heron",
            "models--docling-project--docling-models",
        ):
            snapshot = (
                self.runtime / "cache/huggingface/hub" / model
                / "snapshots/revision"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "model.bin").write_bytes(b"sealed fixture")
        self.returned_staging_names = set()
        real_prepare = private_pdf_uat._default_prepare_uat_run

        def track_prepared_run(*args, **kwargs):
            prepared = real_prepare(*args, **kwargs)
            self.returned_staging_names.add(prepared.staging_name)
            return prepared

        self.prepare_patch = patch.object(
            private_pdf_uat,
            "_default_prepare_uat_run",
            side_effect=track_prepared_run,
        )
        self.prepare_patch.start()
        self.queue_patch = patch.object(
            private_pdf_uat, "_default_queue_items", return_value=[]
        )
        self.queue_patch.start()
        self.events = []
        self.source_bodies = {
            "seed-01": b"%PDF-1.7\nfirst private UAT fixture\n%%EOF\n",
            "seed-02": b"%PDF-1.7\nsecond private UAT fixture\n%%EOF\n",
        }
        self.source_digests = {
            item_id: "sha256:" + hashlib.sha256(body).hexdigest()
            for item_id, body in self.source_bodies.items()
        }
        self.staging_name = "private-pdf-uat-" + "0" * 24
        self.private_manifest = manifest()
        self.private_manifest["documents"][0]["source_digest"] = self.source_digests["seed-01"]
        self.private_manifest["documents"].append({
            "item_id": "seed-02",
            "file": "input/seed-02.pdf",
            "source_digest": self.source_digests["seed-02"],
            "expected_text": ["Second bounded anchor"],
            "required_block_types": ["heading", "table"],
        })
        self.batch_manifest = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
            "batch_id": "batch-private-uat-00000001",
            "documents": [{
                "item_id": "seed-01",
                "source": f"sources/{self.staging_name}/seed-01.pdf",
                "source_digest": self.source_digests["seed-01"],
            }, {
                "item_id": "seed-02",
                "source": f"sources/{self.staging_name}/seed-02.pdf",
                "source_digest": self.source_digests["seed-02"],
            }],
            "continue_on_error": True,
        }
        self.batch_digest = canonical_digest(self.batch_manifest)
        self.success = {
            "item_id": "seed-01",
            "source_digest": self.source_digests["seed-01"],
            "status": "created",
            "candidate_id": "candidate-seed-01",
            "candidate_digest": DIGEST_B,
            "provenance_digest": DIGEST_C,
            "review_status": "unreviewed",
        }
        self.checkpoint = _reconstruct_checkpoint(
            self.batch_manifest, self.batch_digest, [self.success]
        )
        self.rejected = {
            "item_id": "seed-02",
            "source_digest": self.source_digests["seed-02"],
            "status": "rejected",
            "error_code": "parser_rejected",
        }
        self.final = {
            "schema_version": "ao.lore.ingest-batch-readback.v0.1",
            "batch_id": self.batch_manifest["batch_id"],
            "manifest_digest": self.batch_digest,
            "status": "partial",
            "total": 2,
            "created": 1,
            "unchanged": 0,
            "rejected": 1,
            "processed": 2,
            "items": [self.success, self.rejected],
            "next_commands": [
                "ao-lore candidate inspect --candidate-id candidate-seed-01",
                "ao-lore candidate review --candidate-id candidate-seed-01 --decision accept --reviewer <reviewer-id>",
                "ao-lore candidate list --json",
            ],
            "canonical": False,
            "promotion_authority": False,
        }
        self.aggregate = readback()["aggregate"]
        self.aggregate["stable_failures"]["invalid_pdf"] = 0
        self.aggregate["stable_failures"]["conversion_failed"] = 1
        self.process_index = 0
        self.qualification = build_manifest(
            fixture_corpus_digest=private_pdf_uat._QUALIFIED_PDF_CORPUS_DIGEST,
            parser_id="docling",
            parser_version="2.118.1",
            parser_configuration_digest=private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            runtime="python-test",
            platform="linux-test",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["ao-lore", "benchmark", "pdf"]],
            raw_metrics={}, normalized_scores={}, failures=[], exclusions=[],
        )

    def tearDown(self):
        import shutil
        self.prepare_patch.stop()
        self.queue_patch.stop()
        shutil.rmtree(repository_root() / "sources" / ".private-pdf-uat", ignore_errors=True)
        shutil.rmtree(repository_root() / "sources" / self.staging_name, ignore_errors=True)
        for staging_name in self.returned_staging_names:
            self.assertRegex(staging_name, r"^private-pdf-uat-[0-9a-f]{24}$")
            shutil.rmtree(
                repository_root() / "sources" / staging_name,
                ignore_errors=True,
            )
        self.temporary.cleanup()

    def assert_no_new_repository_staging(self):
        before = set(
            (repository_root() / "sources").glob("private-pdf-uat-*")
        )

        def verify():
            self.assertEqual(
                before,
                set((repository_root() / "sources").glob("private-pdf-uat-*")),
            )

        self.addCleanup(verify)

    def prepare_default_run(self, *, qualification_body=None):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True, exist_ok=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir(exist_ok=True)
        (benchmarks / "docling-2.118.1.json").write_bytes(
            private_pdf_uat._manifest_body(self.qualification)
            if qualification_body is None else qualification_body
        )
        for model in (
            "models--docling-project--docling-layout-heron",
            "models--docling-project--docling-models",
        ):
            snapshot = (
                self.runtime / "cache/huggingface/hub" / model
                / "snapshots/test-snapshot"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "model.bin").write_bytes(b"contained fixture")
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            return private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )

    def dependencies(self, **overrides):
        processes = ["interrupted-process", "resumed-process", "rerun-process"]
        results = [
            {"returncode": -signal.SIGTERM, "stdout": b"", "stderr": b"controlled interruption"},
            {"returncode": 0, "stdout": (json.dumps(self.final, sort_keys=True, separators=(",", ":")) + "\n").encode(), "stderr": b""},
            {"returncode": 0, "stdout": (json.dumps(self.final, sort_keys=True, separators=(",", ":")) + "\n").encode(), "stderr": b""},
        ]
        conversion_values = iter((0, 1, 2, 2))

        def prepare(value, runtime):
            self.events.append("prepare")
            source_root = repository_root() / "sources" / self.staging_name
            control_root = repository_root() / "sources" / ".private-pdf-uat"
            source_root.mkdir(parents=True, exist_ok=True)
            control_root.mkdir(parents=True, exist_ok=True)
            for item_id, body in self.source_bodies.items():
                (source_root / f"{item_id}.pdf").write_bytes(body)
            (runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1").mkdir(
                parents=True, exist_ok=True
            )
            (control_root / "run-manifest.json").write_bytes(
                private_pdf_uat._manifest_body(self.batch_manifest)
            )
            corpus_root = runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
            journal = runtime / "uat/private-pdf/batch-private-uat-00000001/conversion-journal.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.touch(exist_ok=True)
            qualification_path = runtime / "benchmarks/docling-2.118.1.json"
            qualification_path.parent.mkdir(parents=True, exist_ok=True)
            qualification_path.write_bytes(
                private_pdf_uat._manifest_body(self.qualification)
            )
            cache = runtime / "cache" / "huggingface"
            for model in (
                "models--docling-project--docling-layout-heron",
                "models--docling-project--docling-models",
            ):
                snapshot = cache / "hub" / model / "snapshots" / "revision"
                snapshot.mkdir(parents=True, exist_ok=True)
                (snapshot / "model.bin").write_bytes(b"sealed fixture")
            sealed = private_pdf_uat._seal_private_pdf_model_cache(
                cache, journal.parent
            )
            self.sealed_cache = sealed
            prepared = PrivatePdfUatPreparedRun(
                corpus_manifest=value,
                corpus_digest=canonical_digest(value),
                qualification_digest=self.qualification["result_digest"],
                qualification_manifest=self.qualification,
                qualification_body=private_pdf_uat._manifest_body(
                    self.qualification
                ),
                qualification_locator=qualification_path,
                qualification_identity=(
                    qualification_path.stat().st_dev,
                    qualification_path.stat().st_ino,
                ),
                batch_manifest=self.batch_manifest,
                batch_manifest_digest=self.batch_digest,
                manifest_locator="sources/.private-pdf-uat/run-manifest.json",
                runtime_root=runtime,
                corpus_root=corpus_root,
                corpus_root_identity=(corpus_root.stat().st_dev, corpus_root.stat().st_ino),
                staging_control_identity=(control_root.stat().st_dev, control_root.stat().st_ino),
                staging_source_identity=(source_root.stat().st_dev, source_root.stat().st_ino),
                staging_name=self.staging_name,
                conversion_journal=journal,
                conversion_journal_identity=(journal.stat().st_dev, journal.stat().st_ino),
                sealed_cache_root=sealed.root,
                sealed_cache_container=sealed.container,
                sealed_cache_root_identity=sealed.root_identity,
                sealed_cache_tree_manifest=sealed.tree_manifest,
                sealed_cache_tree_digest=sealed.tree_digest,
                sealed_cache_file_count=sealed.file_count,
                sealed_cache_total_bytes=sealed.total_bytes,
                origin=private_pdf_uat._seed_campaign_origin(value),
            )
            private_pdf_uat._persist_prepared_run_evidence(prepared)
            return prepared

        def launch(run, phase):
            self.events.append("launch")
            return processes.pop(0)

        def wait(process):
            self.events.append("wait")
            return results.pop(0)

        defaults = dict(
            load_manifest=lambda runtime: self.events.append("load") or copy.deepcopy(self.private_manifest),
            prepare_run=prepare,
            brain_snapshot=lambda: self.events.append("brain") or DIGEST_F,
            candidate_snapshot=lambda: self.events.append("candidate-snapshot") or {},
            conversion_snapshot=lambda: self.events.append("conversion") or next(conversion_values),
            launch_process=launch,
            process_poll=lambda process: None,
            inspect_checkpoint=lambda run: self.events.append("checkpoint") or copy.deepcopy(self.checkpoint),
            inspect_barrier=lambda run, checkpoint: True,
            send_signal=lambda process: self.events.append("signal"),
            terminate_process=lambda process: self.events.append("terminate"),
            wait_process=wait,
            load_final=lambda run: self.events.append("final") or copy.deepcopy(self.final),
            load_queue=lambda: self.events.append("queue") or [{
                "candidate_id": "candidate-seed-01",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "source_digest": self.source_digests["seed-01"],
                "review_status": "unreviewed",
            }],
            evaluate_calibration=lambda value, root: self.events.append("calibrate") or PrivatePdfCalibrationEvidence(
                manifest_digest=canonical_digest(value),
                corpus_digest=canonical_digest(value),
                configuration_digest=private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
                qualification_digest=self.qualification["result_digest"],
                result_digest=canonical_digest(self.aggregate),
                aggregate=copy.deepcopy(self.aggregate),
                sealed_cache_tree_digest=self.sealed_cache.tree_digest,
                sealed_cache_file_count=self.sealed_cache.file_count,
                sealed_cache_total_bytes=self.sealed_cache.total_bytes,
                sandbox_digest=DIGEST_E,
                sandbox_verified=True,
                network_isolated=True,
                campaign_origin_digest=private_pdf_uat._campaign_origin_digest(
                    private_pdf_uat._seed_campaign_origin(value)
                ),
            ),
            cleanup_run=lambda run: self.events.append("cleanup") or True,
            monotonic=lambda: 0.0,
            sleep=lambda value: self.events.append("sleep"),
            checkpoint_timeout=1.0,
            poll_interval=0.01,
        )
        defaults.update(overrides)
        return PrivatePdfUatDependencies(**defaults)

    def test_orchestrates_exact_interrupted_resume_rerun_calibration_and_cleanup_order(self):
        report = run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies())
        self.assertEqual(
            ["load", "prepare", "brain", "candidate-snapshot", "conversion", "launch", "checkpoint", "signal", "wait",
             "conversion", "launch", "wait", "final", "conversion", "launch", "wait", "final", "conversion", "queue", "calibrate",
             "brain", "cleanup", "brain"],
            self.events,
        )
        self.assertEqual("partial", report["lifecycle_status"])
        self.assertEqual({"initial": 1, "resumed": 1, "rerun": 0}, report["conversion_counts"])
        self.assertEqual(1, report["counts"]["interrupted"])
        self.assertEqual(False, report["qualified"])
        self.assertEqual(False, report["provider_calls"])
        self.assertNotIn(str(self.runtime), json.dumps(report, sort_keys=True))

    def test_pdf_wrapper_projects_shared_document_policy_and_bindings(self):
        prepared = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        shared = private_pdf_uat._private_document_prepared_run(prepared)
        self.assertEqual(
            private_document_policy_digest(private_pdf_uat.PRIVATE_PDF_UAT_POLICY),
            shared.policy_digest,
        )
        self.assertEqual("pdf", shared.format_id)
        self.assertEqual(prepared.batch_manifest["batch_id"], shared.batch_id)
        self.assertEqual(
            ["format_id", "policy_digest"],
            list(shared.qualification_binding)[:2],
        )
        self.assertEqual(
            ["format_id", "policy_digest"],
            list(shared.expectation_binding)[:2],
        )

    def test_recovery_cleanup_precedes_candidate_and_brain_baselines(self):
        prior_present = True
        base_prepare = self.dependencies().prepare_run

        def prepare(value, runtime):
            nonlocal prior_present
            prepared = base_prepare(value, runtime)
            prior_present = False
            return prepared

        prior = {
            "candidate_id": self.success["candidate_id"],
            "candidate_digest": self.success["candidate_digest"],
            "provenance_digest": self.success["provenance_digest"],
            "source_digest": self.success["source_digest"],
            "review_status": "unreviewed",
        }
        report = run_private_pdf_uat(
            runtime_root=self.runtime,
            dependencies=self.dependencies(
                prepare_run=prepare,
                candidate_snapshot=lambda: (
                    {prior["candidate_id"]: prior} if prior_present else {}
                ),
            ),
        )
        self.assertEqual(1, report["counts"]["successful"])
        self.assertLess(
            self.events.index("prepare"), self.events.index("brain")
        )

    def test_full_readback_is_retained_and_digest_bound_only_after_cleanup(self):
        runs = []
        dependencies = self.dependencies()

        def prepare(value, runtime):
            prepared = dependencies.prepare_run(value, runtime)
            runs.append(prepared)
            return prepared

        report = run_private_pdf_uat(
            runtime_root=self.runtime,
            dependencies=replace(dependencies, prepare_run=prepare),
        )
        evidence_directory = (
            self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]
        )
        self.assertEqual(
            {"private-pdf-uat-readback.json", "cleaned-state.json"},
            {path.name for path in evidence_directory.iterdir()},
        )
        retained_path = evidence_directory / "private-pdf-uat-readback.json"
        retained_body = retained_path.read_bytes()
        retained = json.loads(retained_body)
        self.assertEqual(report, retained)
        self.assertEqual(
            {
                "metrics", "exclusions", "stable_failures",
                "latency_seconds", "peak_memory_bytes", "repeatability",
            },
            set(retained["aggregate"]),
        )
        retained_digest = "sha256:" + hashlib.sha256(retained_body).hexdigest()
        cleaned = json.loads((evidence_directory / "cleaned-state.json").read_bytes())
        self.assertEqual(retained_digest, cleaned["readback_digest"])
        self.assertEqual(DIGEST_F, cleaned["brain_before_digest"])
        self.assertEqual(DIGEST_F, cleaned["brain_post_run_digest"])
        self.assertEqual(DIGEST_F, cleaned["brain_post_cleanup_digest"])
        self.assertEqual(
            report,
            private_pdf_uat._load_retained_private_uat_readback(
                runs[0], retained_digest
            ),
        )
        retained_path.write_bytes(retained_body + b" ")
        with self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._load_retained_private_uat_readback(
                runs[0], retained_digest
            )

    def test_canonical_evidence_loader_rejects_symlink_unknown_and_cleaned_state_tamper(self):
        runs = []
        dependencies = self.dependencies()

        def prepare(value, runtime):
            prepared = dependencies.prepare_run(value, runtime)
            runs.append(prepared)
            return prepared

        report = run_private_pdf_uat(
            runtime_root=self.runtime,
            dependencies=replace(dependencies, prepare_run=prepare),
        )
        evidence_directory = (
            self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]
        )
        body = (evidence_directory / "private-pdf-uat-readback.json").read_bytes()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        self.assertEqual(
            report,
            private_pdf_uat._load_retained_private_uat_readback(runs[0], digest),
        )

        (evidence_directory / "unknown.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._load_retained_private_uat_readback(runs[0], digest)
        (evidence_directory / "unknown.json").unlink()

        cleaned_path = evidence_directory / "cleaned-state.json"
        cleaned_body = cleaned_path.read_bytes()
        cleaned_path.unlink()
        cleaned_path.symlink_to("private-pdf-uat-readback.json")
        with self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._load_retained_private_uat_readback(runs[0], digest)
        cleaned_path.unlink()
        cleaned_path.write_bytes(cleaned_body.replace(DIGEST_F.encode(), DIGEST_E.encode(), 1))
        with self.assertRaises(PrivatePdfUatError):
            private_pdf_uat._load_retained_private_uat_readback(runs[0], digest)

    def test_legacy_readback_loader_classifies_it_as_unverified_not_terminal(self):
        run = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        report = readback()
        report["corpus_id"] = self.private_manifest["corpus_id"]
        report["corpus_digest"] = canonical_digest(self.private_manifest)
        report["results"] = [copy.deepcopy(self.success), {
            "item_id": "seed-02",
            "source_digest": self.source_digests["seed-02"],
            "status": "rejected",
            "error_code": "conversion_failed",
        }]
        report["counts"] = {
            "total": 2, "created": 1, "unchanged": 0, "rejected": 1,
            "processed": 2, "successful": 1, "candidate_queue": 1,
            "interrupted": 1, "resumed": 1, "rerun": 1,
        }
        report["lifecycle_status"] = "partial"
        report["aggregate"]["stable_failures"]["invalid_pdf"] = 0
        report["aggregate"]["stable_failures"]["conversion_failed"] = 1
        validated = validate_private_pdf_readback(
            report, expected_item_ids=["seed-01", "seed-02"]
        )
        legacy_path = run.conversion_journal.parent / "private-pdf-uat-readback.json"
        legacy_path.write_bytes(private_pdf_uat._manifest_body(validated))
        digest = "sha256:" + hashlib.sha256(legacy_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            PrivatePdfUatError,
            "^private PDF UAT legacy readback is unverified$",
        ):
            private_pdf_uat._load_retained_private_uat_readback(run, digest)
        self.assertFalse(
            (self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]).exists()
        )

    def test_legacy_completed_cleanup_is_read_only_compatible_when_absent(self):
        state = (
            self.runtime / "uat/private-pdf" / self.batch_manifest["batch_id"]
        )
        state.mkdir(parents=True)
        legacy = {
            "schema_version": "ao.lore.private-pdf-uat-prepared-run.v0.1",
            "batch_manifest": copy.deepcopy(self.batch_manifest),
            "batch_manifest_digest": self.batch_digest,
            "corpus_manifest": copy.deepcopy(self.private_manifest),
            "corpus_manifest_digest": canonical_digest(self.private_manifest),
            "qualification_body_digest": DIGEST_A,
            "qualification_identity": [1, 2],
            "qualification_result_digest": DIGEST_B,
            "configuration_digest": private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            "staging_name": self.staging_name,
            "staging_control_identity": [3, 4],
            "staging_source_identity": [5, 6],
            "corpus_root_identity": [7, 8],
            "journal_identity": [9, 10],
            "cleanup": {
                "checkpoint_digest": DIGEST_C,
                "items": [],
                "targets": [
                    {
                        "root": "batches",
                        "root_identity": [11, 12],
                        "name": self.batch_manifest["batch_id"],
                        "quarantine": "batches-0-" + "1" * 24,
                        "identity": [13, 14],
                        "entries": [],
                    },
                    {
                        "root": "sources",
                        "root_identity": [15, 16],
                        "name": self.staging_name,
                        "quarantine": "sources-0-" + "2" * 24,
                        "identity": [17, 18],
                        "entries": [],
                    },
                ],
                "quarantine_name": "cleanup-quarantine-" + "3" * 24,
                "control_absent": True,
            },
            "authority": False,
        }
        (state / "prepared-run-evidence.json").write_bytes(
            private_pdf_uat._manifest_body(legacy)
        )
        self.assertTrue(
            private_pdf_uat._completed_legacy_orphan_cleanup_is_absent(
                self.runtime, state
            )
        )
        live_batch = self.runtime / "batches" / self.batch_manifest["batch_id"]
        live_batch.mkdir(parents=True)
        try:
            self.assertFalse(
                private_pdf_uat._completed_legacy_orphan_cleanup_is_absent(
                    self.runtime, state
                )
            )
        finally:
            live_batch.rmdir()

    def test_readback_persistence_failure_leaves_no_terminal_evidence(self):
        with patch.object(
            private_pdf_uat,
            "_persist_retained_private_uat_readback",
            side_effect=OSError("sentinel private persistence failure"),
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(
                runtime_root=self.runtime, dependencies=self.dependencies()
            )
        self.assertIn("cleanup", self.events)
        self.assertFalse(
            (self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]).exists()
        )

    def test_brain_drift_or_crash_before_and_after_cleanup_leaves_no_terminal_evidence(self):
        cases = {
            "pre-cleanup-drift": lambda: iter((DIGEST_F, DIGEST_E)).__next__,
            "pre-cleanup-crash": lambda: iter((DIGEST_F, RuntimeError("crash"))).__next__,
            "post-cleanup-drift": lambda: iter((DIGEST_F, DIGEST_F, DIGEST_E)).__next__,
            "post-cleanup-crash": lambda: iter((DIGEST_F, DIGEST_F, RuntimeError("crash"))).__next__,
        }
        for name, factory in cases.items():
            values = factory()

            def snapshot():
                value = values()
                if isinstance(value, BaseException):
                    raise value
                return value

            with self.subTest(name=name), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF UAT failed$"
            ):
                run_private_pdf_uat(
                    runtime_root=self.runtime,
                    dependencies=self.dependencies(brain_snapshot=snapshot),
                )
            self.assertFalse(
                (self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]).exists()
            )
            self.events.clear()

    def test_cleanup_drift_leaves_no_terminal_evidence_and_evidence_is_not_allowlisted(self):
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(
                runtime_root=self.runtime,
                dependencies=self.dependencies(cleanup_run=lambda run: False),
            )
        self.assertFalse(
            (self.runtime / "evidence/private-pdf-uat" / self.batch_manifest["batch_id"]).exists()
        )
        with self.assertRaises(OSError):
            private_pdf_uat._cleanup_root(self.runtime, "evidence")

    def test_signal_waits_for_exact_checkpoint_bound_worker_barrier(self):
        observations = iter((False, True))

        def barrier(run, checkpoint):
            self.events.append("barrier")
            return next(observations)

        report = run_private_pdf_uat(
            runtime_root=self.runtime,
            dependencies=self.dependencies(inspect_barrier=barrier),
        )
        signal_index = self.events.index("signal")
        barrier_indexes = [
            index for index, event in enumerate(self.events) if event == "barrier"
        ]
        self.assertEqual(2, len(barrier_indexes))
        self.assertLess(barrier_indexes[-1], signal_index)
        self.assertEqual(1, report["conversion_counts"]["initial"])

    def test_checkpoint_must_be_nonterminal_and_signal_is_exactly_once(self):
        zero = _reconstruct_checkpoint(self.batch_manifest, self.batch_digest, [])
        times = iter((0.0, 0.5, 1.1))
        deps = self.dependencies(
            inspect_checkpoint=lambda run: zero,
            monotonic=lambda: next(times),
            sleep=lambda value: None,
        )
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(runtime_root=self.runtime, dependencies=deps)
        self.assertNotIn("signal", self.events)
        self.assertEqual("cleanup", self.events[-1])

        terminal = _reconstruct_checkpoint(
            self.batch_manifest, self.batch_digest, [self.success, self.rejected]
        )
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies(
                inspect_checkpoint=lambda run: terminal
            ))

    def test_unexpected_process_or_output_and_rerun_drift_fail_closed(self):
        for changed in (
            {"returncode": 0, "stdout": b"", "stderr": b""},
            {"returncode": -signal.SIGTERM, "stdout": b"private", "stderr": b""},
            {"returncode": -signal.SIGTERM, "stdout": b"", "stderr": b"x" * (1024 * 1024 + 1)},
        ):
            with self.subTest(changed=changed["returncode"]):
                calls = iter((changed,))
                with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
                    run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies(
                        wait_process=lambda process: next(calls)
                    ))
                self.events.clear()

        drift = copy.deepcopy(self.final)
        drift["items"][0]["candidate_digest"] = DIGEST_E
        launch_count = 0
        def wait_with_drift(process):
            nonlocal launch_count
            launch_count += 1
            if launch_count == 1:
                return {"returncode": -signal.SIGTERM, "stdout": b"", "stderr": b"controlled"}
            body = self.final if launch_count == 2 else drift
            return {"returncode": 0, "stdout": (json.dumps(body) + "\n").encode(), "stderr": b""}
        with self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies(
                wait_process=wait_with_drift,
                load_final=lambda run: copy.deepcopy(self.final if launch_count == 2 else drift),
            ))

    def test_successful_worker_diagnostics_are_bounded_and_not_projected(self):
        diagnostic = b"docling diagnostic /private/sentinel.pdf\n"
        outcomes = iter((
            {
                "returncode": -signal.SIGTERM,
                "stdout": b"",
                "stderr": b"controlled interruption",
            },
            {
                "returncode": 0,
                "stdout": (
                    json.dumps(self.final, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode(),
                "stderr": diagnostic,
            },
            {
                "returncode": 0,
                "stdout": (
                    json.dumps(self.final, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode(),
                "stderr": diagnostic,
            },
        ))
        report = run_private_pdf_uat(
            runtime_root=self.runtime,
            dependencies=self.dependencies(wait_process=lambda process: next(outcomes)),
        )
        public = json.dumps(report, sort_keys=True, separators=(",", ":"))
        self.assertNotIn("diagnostic", public)
        self.assertNotIn("sentinel", public)
        self.assertNotIn("/private", public)
        self.assertEqual(3, self.events.count("launch"))
        self.assertEqual(0, report["conversion_counts"]["rerun"])

    def test_successful_worker_diagnostics_remain_bounded_and_nonzero_fails(self):
        terminal = (
            json.dumps(self.final, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        cases = (
            {"returncode": 0, "stdout": terminal, "stderr": b"x" * (1024 * 1024 + 1)},
            {"returncode": 7, "stdout": terminal, "stderr": b"bounded"},
        )
        for result in cases:
            with self.subTest(returncode=result["returncode"], size=len(result["stderr"])):
                outcomes = iter((
                    {
                        "returncode": -signal.SIGTERM,
                        "stdout": b"",
                        "stderr": b"controlled interruption",
                    },
                    result,
                ))
                with self.assertRaisesRegex(
                    PrivatePdfUatError, "^private PDF UAT failed$"
                ):
                    run_private_pdf_uat(
                        runtime_root=self.runtime,
                        dependencies=self.dependencies(
                            wait_process=lambda process: next(outcomes)
                        ),
                    )
                self.events.clear()

    def test_manifest_checkpoint_queue_brain_and_cleanup_drift_fail_closed(self):
        cases = {
            "manifest": dict(load_manifest=lambda runtime: {**manifest(), "provider_calls": True}),
            "checkpoint": dict(inspect_checkpoint=lambda run: {**self.checkpoint, "manifest_digest": DIGEST_E}),
            "queue": dict(load_queue=lambda: []),
            "brain": dict(brain_snapshot=iter((DIGEST_F, DIGEST_E)).__next__),
            "cleanup": dict(cleanup_run=lambda run: False),
        }
        for name, override in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
                run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies(**override))
            self.events.clear()

    def test_keyboard_interrupt_and_system_exit_are_preserved_after_cleanup(self):
        for raised in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(raised=type(raised).__name__), self.assertRaises(type(raised)):
                run_private_pdf_uat(runtime_root=self.runtime, dependencies=self.dependencies(
                launch_process=lambda run, phase, raised=raised: (_ for _ in ()).throw(raised)
                ))
            self.assertEqual("cleanup", self.events[-1])
            self.events.clear()

    def test_conversion_observation_is_authoritative_and_rerun_delta_must_be_zero(self):
        for observed in (
            (0, 0, 2, 2),
            (0, 1, 1, 2),
            (0, 1, 2, 3),
            (0, 1, 2, True),
        ):
            values = iter(observed)
            with self.subTest(observed=observed), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF UAT failed$"
            ):
                run_private_pdf_uat(
                    runtime_root=self.runtime,
                    dependencies=self.dependencies(conversion_snapshot=lambda: next(values)),
                )
            self.events.clear()

    def test_wait_control_interrupt_terminates_process_group_then_cleans_run(self):
        with self.assertRaises(KeyboardInterrupt):
            run_private_pdf_uat(
                runtime_root=self.runtime,
                dependencies=self.dependencies(
                    wait_process=lambda process: (_ for _ in ()).throw(KeyboardInterrupt())
                ),
            )
        self.assertEqual(["terminate", "cleanup"], self.events[-2:])

    def test_production_launcher_is_exact_offline_no_shell_and_contained(self):
        run = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        process = type("Process", (), {})()
        completed = subprocess.CompletedProcess(
            ["/usr/bin/bwrap", "--version"], 0, b"bubblewrap 0.8.0\n", b""
        )
        with patch.object(
            private_pdf_uat.subprocess, "run", return_value=completed
        ) as probe, patch.object(
            private_pdf_uat.subprocess, "Popen", return_value=process
        ) as launch:
            self.assertIs(process, private_pdf_uat._default_launch_process(run, "rerun"))
        arguments, options = launch.call_args
        argv = arguments[0]
        self.assertEqual("/usr/bin/bwrap", argv[0])
        self.assertIn("--unshare-net", argv)
        self.assertEqual(
            ["--tmpfs", "/"],
            argv[argv.index("--tmpfs"):argv.index("--tmpfs") + 2],
        )
        self.assertNotIn(
            ["--ro-bind", "/", "/"],
            [argv[index:index + 3] for index in range(len(argv) - 2)],
        )
        self.assertIn("--tmpfs", argv)
        self.assertEqual([sys.executable, "-m", "ao_lore.private_pdf_uat_worker"], argv[-3:])
        self.assertEqual(
            [["/usr/bin/bwrap", "--version"], private_pdf_uat._BWRAP_PROBE_ARGV],
            [call.args[0] for call in probe.call_args_list],
        )
        self.assertEqual(repository_root(), options["cwd"])
        self.assertFalse(options["shell"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual({}, options["env"])
        self.assertNotIn("SSH_AUTH_SOCK", options["env"])
        contract = json.loads(
            (run.conversion_journal.parent / "worker-run-contract.json").read_text()
        )
        sandbox = contract["sandbox"]
        self.assertEqual("/usr/bin/bwrap", sandbox["mechanism"])
        self.assertEqual("bubblewrap 0.8.0", sandbox["version"])
        self.assertEqual(argv, sandbox["argv"])
        self.assertEqual("unshared", sandbox["network_namespace"])
        self.assertEqual("allowlisted", sandbox["root_filesystem"])
        self.assertEqual("read-only", sandbox["sealed_cache"]["mode"])
        expected_mount_names = {
            mount["name"]
            for mount in private_pdf_uat._private_pdf_sandbox_foundation_mounts()
        } | {
            "staged-sources", "control-manifest", "qualification",
            "batch-state", "candidate-state", "run-state", "sealed-cache",
        }
        self.assertEqual(
            expected_mount_names,
            {mount["name"] for mount in sandbox["mounts"]},
        )
        self.assertEqual(
            {
                "AO_LORE_HOME", "HF_HOME", "HOME", "LANG", "LC_ALL", "PATH",
                "PYTHONNOUSERSITE", "PYTHONPATH", "PWD", "TORCH_HOME", "TRANSFORMERS_CACHE",
                "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                "HF_HUB_OFFLINE", "XDG_CACHE_HOME",
            },
            set(sandbox["environment"]),
        )
        self.assertNotIn("HTTP_PROXY", sandbox["environment"])
        self.assertNotIn("AWS_ACCESS_KEY_ID", sandbox["environment"])

    def test_sandboxes_do_not_scan_or_mount_unrelated_repository_sources(self):
        run = self.prepare_default_run()
        unrelated = repository_root() / "sources" / ".private-pdf-sandbox-unrelated"
        unrelated.mkdir()
        socket_path = unrelated / "unrelated.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_directory = Path.cwd()
        os.chdir(unrelated.parent)
        try:
            listener.bind(f"{unrelated.name}/{socket_path.name}")
        finally:
            os.chdir(previous_directory)
        self.addCleanup(unrelated.rmdir)
        self.addCleanup(socket_path.unlink, missing_ok=True)
        self.addCleanup(listener.close)

        sandboxes = (
            private_pdf_uat._private_pdf_sandbox_spec(
                run,
                "rerun",
                command=[sys.executable, "-m", "ao_lore.private_pdf_uat_worker"],
                pythonpath=str(repository_root() / "src"),
                bwrap_version="bubblewrap 0.8.0",
            ),
            private_pdf_uat._private_pdf_calibration_sandbox_spec(
                run, bwrap_version="bubblewrap 0.8.0"
            ),
        )

        for sandbox in sandboxes:
            targets = {mount["target"] for mount in sandbox["mounts"]}
            self.assertNotIn(str(repository_root()), targets)
            self.assertNotIn(str(repository_root() / "sources"), targets)
            self.assertNotIn(str(unrelated), targets)

        run_mounts = {mount["name"]: mount for mount in sandboxes[0]["mounts"]}
        self.assertEqual(
            str(repository_root() / "sources" / run.staging_name),
            run_mounts["staged-sources"]["target"],
        )
        self.assertEqual(
            str(repository_root() / run.manifest_locator),
            run_mounts["control-manifest"]["target"],
        )
        calibration_mounts = {
            mount["name"]: mount for mount in sandboxes[1]["mounts"]
        }
        self.assertEqual(
            str(run.corpus_root),
            calibration_mounts["calibration-corpus"]["target"],
        )

    def test_default_calibration_is_delegated_to_the_sandbox_worker(self):
        run = self.prepare_default_run()
        expected = PrivatePdfCalibrationEvidence(
            manifest_digest=canonical_digest(self.private_manifest),
            corpus_digest=canonical_digest(self.private_manifest),
            configuration_digest=private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            qualification_digest=self.qualification["result_digest"],
            result_digest=canonical_digest(self.aggregate),
            aggregate=copy.deepcopy(self.aggregate),
            sealed_cache_tree_digest=run.sealed_cache_tree_digest,
            sealed_cache_file_count=run.sealed_cache_file_count,
            sealed_cache_total_bytes=run.sealed_cache_total_bytes,
            sandbox_digest=DIGEST_E,
            sandbox_verified=True,
            network_isolated=True,
            campaign_origin_digest=private_pdf_uat._campaign_origin_digest(
                run.origin
            ),
        )
        with patch.object(
            private_pdf_uat,
            "_run_private_pdf_calibration_worker",
            return_value=expected,
        ) as delegated, patch.object(
            private_pdf_uat, "DoclingPdfAdapter"
        ) as parent_adapter, patch.object(
            private_pdf_uat, "_default_prepare_uat_run", return_value=run
        ):
            dependencies = private_pdf_uat._default_private_pdf_uat_dependencies(
                self.runtime
            )
            dependencies.prepare_run(self.private_manifest, self.runtime)
            actual = dependencies.evaluate_calibration(
                self.private_manifest, run.corpus_root
            )
        self.assertEqual(expected, actual)
        delegated.assert_called_once()
        parent_adapter.assert_not_called()

    def test_calibration_launcher_uses_fixed_networkless_bwrap_and_atomic_contract(self):
        run = self.prepare_default_run()
        process = type("Process", (), {})()
        completed = subprocess.CompletedProcess(
            ["/usr/bin/bwrap", "--version"], 0, b"bubblewrap 0.8.0\n", b""
        )
        with patch.object(
            private_pdf_uat.subprocess, "run", return_value=completed
        ), patch.object(
            private_pdf_uat.subprocess, "Popen", return_value=process
        ) as launch, patch.object(
            private_pdf_uat, "_verify_pdf", return_value=None
        ):
            private_pdf_uat._launch_private_pdf_calibration_worker(run)
        argv = launch.call_args.args[0]
        self.assertEqual("/usr/bin/bwrap", argv[0])
        self.assertIn("--unshare-net", argv)
        self.assertEqual(
            [sys.executable, "-m", "ao_lore.private_pdf_calibration_worker"],
            argv[-3:],
        )
        contract_path = run.conversion_journal.parent / "calibration-run-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "ao.lore.private-pdf-calibration-run.v0.1",
            contract["schema_version"],
        )
        self.assertEqual(
            [item["item_id"] for item in self.private_manifest["documents"]],
            [item["item_id"] for item in contract["sources"]],
        )
        self.assertEqual("unshared", contract["sandbox"]["network_namespace"])
        self.assertEqual("read-only", contract["sandbox"]["sealed_cache"]["mode"])
        self.assertFalse(contract["authority"])

    def test_calibration_worker_revalidates_exact_contract_environment_and_mounts(self):
        run = self.prepare_default_run()
        sandbox = private_pdf_uat._private_pdf_calibration_sandbox_spec(
            run, bwrap_version="bubblewrap 0.8.0"
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            contract = private_pdf_uat._private_pdf_calibration_contract(run, sandbox)
            with patch.dict(os.environ, sandbox["environment"], clear=True):
                validated, qualification, sealed, corpus, output = (
                    private_pdf_uat._validate_calibration_worker_contract(
                        contract, runtime=run.runtime_root
                    )
                )
        self.assertEqual(contract, validated)
        self.assertEqual(run.qualification_digest, qualification["result_digest"])
        self.assertEqual(run.sealed_cache_tree_digest, sealed.tree_digest)
        self.assertEqual(run.corpus_root, corpus)
        self.assertEqual(
            run.conversion_journal.parent / "calibration-result.json", output
        )

    def test_calibration_sandbox_unavailable_starts_nothing_and_leaves_no_contract(self):
        run = self.prepare_default_run()
        contract_path = run.conversion_journal.parent / "calibration-run-contract.json"
        with patch.object(
            private_pdf_uat,
            "_probe_private_pdf_sandbox",
            side_effect=PrivatePdfUatError("private PDF UAT sandbox is unavailable"),
        ), patch.object(private_pdf_uat.subprocess, "Popen") as launch, self.assertRaisesRegex(
            PrivatePdfUatError, "sandbox is unavailable"
        ):
            private_pdf_uat._launch_private_pdf_calibration_worker(run)
        launch.assert_not_called()
        self.assertFalse(contract_path.exists())

    def test_calibration_launcher_preserves_foreign_contract_and_result_collisions(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        for name in ("calibration-run-contract.json", "calibration-result.json"):
            with self.subTest(name=name):
                path = state / name
                path.write_bytes(b"foreign\n")
                with patch.object(
                    private_pdf_uat,
                    "_probe_private_pdf_sandbox",
                    return_value="bubblewrap 0.8.0",
                ), patch.object(
                    private_pdf_uat, "_verify_pdf", return_value=None
                ), patch.object(subprocess, "Popen") as launch, self.assertRaises(
                    (OSError, PrivatePdfUatError)
                ):
                    private_pdf_uat._launch_private_pdf_calibration_worker(run)
                launch.assert_not_called()
                self.assertEqual(b"foreign\n", path.read_bytes())
                path.unlink()

    def test_calibration_popen_failure_preserves_raced_foreign_contract(self):
        run = self.prepare_default_run()
        contract = run.conversion_journal.parent / "calibration-run-contract.json"
        displaced = contract.with_name("calibration-run-contract.owned")

        def replace_then_fail(*_args, **_kwargs):
            contract.rename(displaced)
            contract.write_bytes(b"foreign\n")
            raise KeyboardInterrupt()

        with patch.object(
            private_pdf_uat,
            "_probe_private_pdf_sandbox",
            return_value="bubblewrap 0.8.0",
        ), patch.object(
            private_pdf_uat, "_verify_pdf", return_value=None
        ), patch.object(
            private_pdf_uat.subprocess, "Popen", side_effect=replace_then_fail
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._launch_private_pdf_calibration_worker(run)
        self.assertEqual(b"foreign\n", contract.read_bytes())
        contract.unlink()
        displaced.unlink()

    def test_calibration_timeout_preserves_unowned_contract_and_result(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        contract_path = state / "calibration-run-contract.json"
        result_path = state / "calibration-result.json"
        process = SimpleNamespace(pid=4567, poll=lambda: None)

        sandbox = private_pdf_uat._private_pdf_calibration_sandbox_spec(
            run, bwrap_version="bubblewrap 0.8.0"
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            foreign_contract = private_pdf_uat._private_pdf_calibration_contract(
                run, sandbox
            )
        contract_body = private_pdf_uat._manifest_body(foreign_contract)
        result_body = b'{"foreign":true}\n'

        def launch(_run):
            contract_path.write_bytes(contract_body)
            result_path.write_bytes(result_body)
            return process

        with patch.object(
            private_pdf_uat,
            "_launch_private_pdf_calibration_worker",
            side_effect=launch,
        ), patch.object(
            private_pdf_uat,
            "_default_wait_process",
            side_effect=PrivatePdfUatError("private PDF UAT process timed out"),
        ), patch.object(
            private_pdf_uat, "_terminate_process_group"
        ), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF calibration failed$"
        ):
            private_pdf_uat._run_private_pdf_calibration_worker(run)
        self.assertEqual(contract_body, contract_path.read_bytes())
        self.assertEqual(result_body, result_path.read_bytes())

    def test_calibration_artifact_publish_failure_removes_only_owned_final(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        result_path = state / "calibration-result.json"
        descriptor = private_pdf_uat._open_directory(
            state, run.runtime_root, create=False
        )
        real_fsync = os.fsync
        parent_fsyncs = 0

        def fail_after_publish(file_descriptor):
            nonlocal parent_fsyncs
            if file_descriptor == descriptor:
                parent_fsyncs += 1
                raise KeyboardInterrupt()
            return real_fsync(file_descriptor)

        try:
            with patch.object(
                private_pdf_uat.os, "fsync", side_effect=fail_after_publish
            ), self.assertRaises(KeyboardInterrupt):
                private_pdf_uat._publish_calibration_artifact_at(
                    descriptor, result_path.name, b"owned\n", 64
                )
        finally:
            os.close(descriptor)
        self.assertGreaterEqual(parent_fsyncs, 1)
        self.assertFalse(result_path.exists())

    def test_calibration_artifact_publish_failure_preserves_final_replacement(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        result_path = state / "calibration-result.json"
        displaced = state / "calibration-result.owned"
        descriptor = private_pdf_uat._open_directory(
            state, run.runtime_root, create=False
        )
        real_fsync = os.fsync

        def replace_after_publish(file_descriptor):
            if file_descriptor == descriptor:
                result_path.rename(displaced)
                result_path.write_bytes(b"foreign\n")
                raise KeyboardInterrupt()
            return real_fsync(file_descriptor)

        try:
            with patch.object(
                private_pdf_uat.os, "fsync", side_effect=replace_after_publish
            ), self.assertRaises(KeyboardInterrupt):
                private_pdf_uat._publish_calibration_artifact_at(
                    descriptor, result_path.name, b"owned\n", 64
                )
        finally:
            os.close(descriptor)
        self.assertEqual(b"foreign\n", result_path.read_bytes())
        self.assertEqual(b"owned\n", displaced.read_bytes())
        result_path.unlink()
        displaced.unlink()

    def test_calibration_output_digest_tamper_is_untrusted_and_removed(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        sandbox = private_pdf_uat._private_pdf_calibration_sandbox_spec(
            run, bwrap_version="bubblewrap 0.8.0"
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            contract = private_pdf_uat._private_pdf_calibration_contract(run, sandbox)
        private_pdf_uat._persist_uat_document(
            state, run.runtime_root, "calibration-run-contract.json", contract
        )
        result = {
            "schema_version": private_pdf_uat._CALIBRATION_RESULT_SCHEMA_VERSION,
            "run_digest": contract["run_digest"],
            "manifest_digest": canonical_digest(run.corpus_manifest),
            "configuration_digest": private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            "qualification_digest": run.qualification_digest,
            "sealed_cache_tree_digest": run.sealed_cache_tree_digest,
            "sealed_cache_file_count": run.sealed_cache_file_count,
            "sealed_cache_total_bytes": run.sealed_cache_total_bytes,
            "sandbox_digest": canonical_digest(sandbox),
            "sandbox_verified": True,
            "network_isolated": True,
            "aggregate": copy.deepcopy(self.aggregate),
            "aggregate_digest": DIGEST_A,
            "authority": False,
        }
        private_pdf_uat._persist_uat_document(
            state, run.runtime_root, "calibration-result.json", result
        )
        process = SimpleNamespace(
            pid=4567,
            _ao_lore_sandbox_phase="calibration",
            _ao_lore_sandbox_verified=True,
            _ao_lore_sandbox_digest=canonical_digest(sandbox),
            poll=lambda: 0,
        )
        parent_descriptor = private_pdf_uat._open_directory(
            state, run.runtime_root, create=False
        )
        process._ao_lore_artifact_parent_descriptor = parent_descriptor
        process._ao_lore_contract_artifact = (
            private_pdf_uat._capture_calibration_artifact_at(
                parent_descriptor,
                "calibration-run-contract.json",
                private_pdf_uat._PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
            )
        )
        with patch.object(
            private_pdf_uat,
            "_launch_private_pdf_calibration_worker",
            return_value=process,
        ), patch.object(
            private_pdf_uat,
            "_default_wait_process",
            return_value={"returncode": 0, "stdout": b"", "stderr": b""},
        ), self.assertRaisesRegex(
            PrivatePdfUatError, "^private PDF calibration failed$"
        ):
            private_pdf_uat._run_private_pdf_calibration_worker(run)
        self.assertFalse((state / "calibration-run-contract.json").exists())
        self.assertFalse((state / "calibration-result.json").exists())

    def test_calibration_contract_uses_the_reviewed_exact_corpus_item_deadline(self):
        self.assertEqual(
            600.0, private_pdf_uat._CALIBRATION_ITEM_TIMEOUT_SECONDS
        )
        self.assertGreater(
            private_pdf_uat._CALIBRATION_PROCESS_TIMEOUT_SECONDS,
            2 * private_pdf_uat._CALIBRATION_ITEM_TIMEOUT_SECONDS,
        )

    def test_calibration_result_publication_never_replaces_a_racing_file(self):
        run = self.prepare_default_run()
        state = run.conversion_journal.parent
        result = state / "calibration-result.json"
        result.write_bytes(b"foreign\n")
        with self.assertRaises(OSError):
            private_pdf_uat._persist_calibration_result(
                state, run.runtime_root, {"authority": False}
            )
        self.assertEqual(b"foreign\n", result.read_bytes())

    def test_calibration_sandbox_denies_udp_dns_and_descendant_network(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("Ubuntu bubblewrap is unavailable")
        run = self.prepare_default_run()
        spec = private_pdf_uat._private_pdf_calibration_sandbox_spec(
            run, bwrap_version="bubblewrap 0.8.0"
        )
        script = r'''
import os, socket, subprocess
target = ".".join(("10", "0", "0", "1"))
child = "import socket; socket.create_connection((" + repr(target) + ",9),.05)"
attempts = [
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", (target, 9)),
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: subprocess.run(["/usr/bin/python3", "-c", child], check=True),
    lambda: open(os.path.join(os.environ["HF_HOME"], "mutation"), "wb").write(b"x"),
]
denied = 0
for attempt in attempts:
    try: attempt()
    except BaseException: denied += 1
raise SystemExit(0 if denied == len(attempts) else 9)
'''
        argv = list(spec["argv"][:-3]) + ["/usr/bin/python3", "-c", script]
        completed = subprocess.run(
            argv, cwd=repository_root(), env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))

    def test_ingest_and_calibration_sandboxes_hide_host_unix_sockets(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("Ubuntu bubblewrap is unavailable")
        run = self.prepare_default_run()
        script = r'''
import socket, sys
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    client.connect(sys.argv[1])
    client.sendall(b"escaped")
except OSError:
    raise SystemExit(0)
raise SystemExit(9)
'''
        with tempfile.TemporaryDirectory(dir="/var/tmp") as outside:
            socket_path = str(Path(outside) / "host-control.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)
            try:
                sandboxes = (
                    private_pdf_uat._private_pdf_sandbox_spec(
                        run,
                        "rerun",
                        command=["/usr/bin/python3", "-c", script, socket_path],
                        pythonpath=str(repository_root() / "src"),
                        bwrap_version="bubblewrap 0.8.0",
                    ),
                    private_pdf_uat._private_pdf_calibration_sandbox_spec(
                        run, bwrap_version="bubblewrap 0.8.0"
                    ),
                )
                for index, sandbox in enumerate(sandboxes):
                    argv = list(sandbox["argv"])
                    if index == 1:
                        argv = argv[:-3] + [
                            "/usr/bin/python3", "-c", script, socket_path
                        ]
                    with self.subTest(sandbox=index):
                        completed = subprocess.run(
                            argv,
                            cwd=repository_root(),
                            env={},
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=15,
                            check=False,
                        )
                        self.assertEqual(
                            0,
                            completed.returncode,
                            completed.stderr.decode(errors="replace"),
                        )
            finally:
                listener.close()

    def test_sandbox_rejects_a_unix_socket_inside_an_allowlisted_mount(self):
        run = self.prepare_default_run()
        candidate_root = repository_root() / "working" / "candidates"
        socket_path = candidate_root / ".private-pdf-sandbox-test.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_directory = Path.cwd()
        os.chdir(candidate_root)
        listener.bind(socket_path.name)
        try:
            with self.assertRaisesRegex(
                PrivatePdfUatError,
                "^private PDF UAT sandbox mount is invalid$",
            ):
                private_pdf_uat._private_pdf_sandbox_spec(
                    run,
                    "rerun",
                    command=["/usr/bin/true"],
                    pythonpath=str(repository_root() / "src"),
                    bwrap_version="bubblewrap 0.8.0",
                )
        finally:
            listener.close()
            socket_path.unlink(missing_ok=True)
            os.chdir(previous_directory)

    def test_sandbox_probe_failure_starts_no_worker_and_persists_no_contract(self):
        run = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        contract_path = run.conversion_journal.parent / "worker-run-contract.json"
        with patch.object(
            private_pdf_uat.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/bwrap", "--version"], 1, b"", b"unavailable"
            ),
        ), patch.object(private_pdf_uat.subprocess, "Popen") as launch, self.assertRaisesRegex(
            PrivatePdfUatError, "sandbox is unavailable"
        ):
            private_pdf_uat._default_launch_process(run, "rerun")
        launch.assert_not_called()
        self.assertFalse(contract_path.exists())

    def test_actual_bwrap_denies_network_and_outside_writes_but_allows_run_state(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("Ubuntu bubblewrap is unavailable")
        run = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        state = run.conversion_journal.parent
        script = r'''
import json, os, socket, subprocess
failures = []
target = ".".join(("10", "0", "0", "1"))
child = "import socket; socket.create_connection((" + repr(target) + ",9),.05)"
attempts = [
    lambda: socket.create_connection((target, 9), timeout=.05),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", (target, 9)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, (target, 9)),
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: subprocess.run(["/usr/bin/python3", "-c", child], check=True),
    lambda: subprocess.run(["/usr/bin/bash", "-c", f"echo x >/dev/tcp/{target}/9"], check=True),
    lambda: open("/etc/ao-lore-sandbox-write", "wb").write(b"x"),
]
for attempt in attempts:
    try: attempt()
    except BaseException: failures.append(True)
open(os.path.join(os.environ["HOME"], "allowed.json"), "w").write(json.dumps({"denied": len(failures)}))
raise SystemExit(0 if len(failures) == len(attempts) else 7)
'''
        private_pdf_uat._probe_private_pdf_sandbox()
        spec = private_pdf_uat._private_pdf_sandbox_spec(
            run,
            "rerun",
            command=["/usr/bin/python3", "-c", script],
            pythonpath=str(repository_root() / "src"),
            bwrap_version="bubblewrap 0.8.0",
        )
        completed = subprocess.run(
            spec["argv"], cwd=repository_root(), env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        allowed = json.loads((state / "sandbox-home/allowed.json").read_text())
        self.assertEqual(7, allowed["denied"])
        self.assertFalse(Path("/etc/ao-lore-sandbox-write").exists())

    def test_actual_bwrap_exposes_the_active_interpreter_dependency_roots(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("Ubuntu bubblewrap is unavailable")
        run = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        spec = private_pdf_uat._private_pdf_sandbox_spec(
            run,
            "rerun",
            command=[sys.executable, "-c", "import pydantic"],
            pythonpath=str(repository_root() / "src"),
            bwrap_version="bubblewrap 0.8.0",
        )
        completed = subprocess.run(
            spec["argv"], cwd=repository_root(), env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr.decode(errors="replace"),
        )

    def test_process_group_termination_has_bounded_term_and_kill_waits(self):
        class Stuck:
            pid = 12345
            def __init__(self):
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                raise subprocess.TimeoutExpired("private-uat", timeout)
            def poll(self):
                return None
            def terminate(self):
                pass
            def kill(self):
                pass
        process = Stuck()
        with patch.object(
            private_pdf_uat.os, "getpgid", return_value=process.pid
        ), patch.object(private_pdf_uat.os, "killpg"):
            with self.assertRaises(OSError):
                private_pdf_uat._terminate_process_group(process)
        self.assertEqual([0.2, 0.2], process.waits)

    def test_process_group_termination_rejects_unbound_pids_without_signaling(self):
        for unsafe_pid in (object(), True, 0, 1, os.getpid(), os.getpgrp()):
            with self.subTest(pid=unsafe_pid), patch.object(
                private_pdf_uat.os, "killpg"
            ) as kill_group, self.assertRaises(OSError):
                private_pdf_uat._terminate_process_group(
                    SimpleNamespace(pid=unsafe_pid)
                )
            kill_group.assert_not_called()

    def test_prepared_run_rejects_context_qualification_and_staging_drift(self):
        base = self.dependencies().prepare_run

        def with_context(value, runtime):
            prepared = base(value, runtime)
            batch = copy.deepcopy(prepared.batch_manifest)
            batch["documents"][0]["candidate_context"] = {
                "schema_version": "ao.lore.candidate-comparison-context.v0.1",
                "candidates": [],
            }
            return replace(
                prepared,
                batch_manifest=batch,
                batch_manifest_digest=canonical_digest(batch),
            )

        def with_qualification_drift(value, runtime):
            prepared = base(value, runtime)
            qualification = copy.deepcopy(prepared.qualification_manifest)
            qualification["fixture_corpus_digest"] = DIGEST_E
            return replace(prepared, qualification_manifest=qualification)

        def with_foreign_stage(value, runtime):
            prepared = base(value, runtime)
            (repository_root() / "sources" / self.staging_name / "foreign.pdf").write_bytes(b"foreign")
            return prepared

        for prepare in (with_context, with_qualification_drift, with_foreign_stage):
            with self.subTest(prepare=prepare.__name__), self.assertRaisesRegex(
                PrivatePdfUatError, "^private PDF UAT failed$"
            ):
                run_private_pdf_uat(
                    runtime_root=self.runtime,
                    dependencies=self.dependencies(prepare_run=prepare),
                )

    def test_queue_preserves_all_prior_candidates_and_new_candidates_are_unreviewed(self):
        created_queue = [{
            "candidate_id": self.success["candidate_id"],
            "candidate_digest": self.success["candidate_digest"],
            "provenance_digest": self.success["provenance_digest"],
            "source_digest": self.success["source_digest"],
            "review_status": "unreviewed",
        }]
        projected = private_pdf_uat._verify_queue(
            created_queue, self.final, {}
        )
        self.assertEqual("unreviewed", projected[0]["review_status"])
        for status in ("accepted", "rejected"):
            changed = copy.deepcopy(created_queue)
            changed[0]["review_status"] = status
            with self.subTest(new_status=status), self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._verify_queue(changed, self.final, {})

        unchanged = copy.deepcopy(self.success)
        unchanged["status"] = "unchanged"
        unchanged["review_status"] = "accepted"
        terminal = {**self.final, "items": [unchanged, self.rejected], "created": 0, "unchanged": 1}
        prior_item = {
            key: unchanged[key]
            for key in ("candidate_id", "candidate_digest", "provenance_digest", "source_digest", "review_status")
        }
        self.assertEqual(
            "accepted",
            private_pdf_uat._verify_queue(
                [prior_item], terminal, {prior_item["candidate_id"]: prior_item}
            )[0]["review_status"],
        )
        foreign = {**prior_item, "candidate_id": "candidate-foreign"}
        for queue in ([], [foreign], [{**prior_item, "candidate_digest": DIGEST_E}]):
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._verify_queue(
                    queue, terminal, {prior_item["candidate_id"]: prior_item}
                )

    def test_brain_snapshot_rejects_symlink_hardlink_special_and_mutation(self):
        brain = repository_root() / "brain"
        source = brain / ".private-pdf-uat-snapshot-test"
        peer = brain / ".private-pdf-uat-snapshot-peer"
        source.write_bytes(b"stable")
        try:
            peer.symlink_to(source.name)
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._default_brain_snapshot()
            peer.unlink()

            os.link(source, peer)
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._default_brain_snapshot()
            peer.unlink()
            source.unlink()

            os.mkfifo(source)
            with self.assertRaises(PrivatePdfUatError):
                private_pdf_uat._default_brain_snapshot()
            source.unlink()

            source.write_bytes(b"stable")
            real_read = private_pdf_uat._read_bounded
            mutated = False
            def mutate_after_read(descriptor, maximum):
                nonlocal mutated
                body = real_read(descriptor, maximum)
                if not mutated and body == b"stable":
                    mutated = True
                    source.write_bytes(b"changed")
                return body
            with patch.object(private_pdf_uat, "_read_bounded", side_effect=mutate_after_read):
                with self.assertRaises(PrivatePdfUatError):
                    private_pdf_uat._default_brain_snapshot()
        finally:
            for path in (peer, source):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def test_default_prepare_stages_exact_seed_and_qualification_without_docling(self):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir()
        write_exclusive_json(
            benchmarks / "docling-2.118.1.json",
            self.qualification,
            root=self.runtime,
        )
        for model in (
            "models--docling-project--docling-layout-heron",
            "models--docling-project--docling-models",
        ):
            snapshot = (
                self.runtime / "cache/huggingface/hub" / model
                / "snapshots/revision"
            )
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "model.bin").write_bytes(b"sealed fixture")
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            prepared = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertEqual(self.qualification["result_digest"], prepared.qualification_digest)
        self.assertEqual(
            (benchmarks / "docling-2.118.1.json").read_bytes(),
            prepared.qualification_body,
        )
        self.assertEqual(
            [
                f"sources/{prepared.staging_name}/seed-01.pdf",
                f"sources/{prepared.staging_name}/seed-02.pdf",
            ],
            [item["source"] for item in prepared.batch_manifest["documents"]],
        )
        self.assertTrue(all("candidate_context" not in item for item in prepared.batch_manifest["documents"]))
        self.assertTrue(prepared.sealed_cache_root.is_relative_to(
            prepared.conversion_journal.parent
        ))
        self.assertNotEqual(
            self.runtime / "cache" / "huggingface", prepared.sealed_cache_root
        )
        private_pdf_uat._validate_sealed_private_pdf_model_cache(
            private_pdf_uat._sealed_cache_from_prepared_run(prepared)
        )
        validated = private_pdf_uat._validate_prepared_run(
            prepared, self.private_manifest, self.runtime
        )
        self.assertEqual(prepared.batch_manifest_digest, validated.batch_manifest_digest)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        # The production default uses the standard runtime batch location.
        standard_batch = self.runtime / "batches" / prepared.batch_manifest["batch_id"]
        self.assertTrue((standard_batch / "checkpoint.json").is_file())
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        self.assertFalse(standard_batch.exists())
        self.assertFalse((repository_root() / "sources/.private-pdf-uat").exists())
        self.assertFalse((repository_root() / "sources" / prepared.staging_name).exists())

    def test_prepare_validation_failure_is_recovered_before_retry(self):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir()
        write_exclusive_json(
            benchmarks / "docling-2.118.1.json",
            self.qualification,
            root=self.runtime,
        )
        defaults = private_pdf_uat._default_private_pdf_uat_dependencies(
            self.runtime
        )
        cleanup_errors = []

        def cleanup(run):
            try:
                return defaults.cleanup_run(run)
            except Exception as exc:
                cleanup_errors.append(exc)
                raise

        preexisting_staging = set(
            (repository_root() / "sources").glob("private-pdf-uat-*")
        )
        dependencies = replace(
            defaults,
            brain_snapshot=lambda: DIGEST_F,
            cleanup_run=cleanup,
        )
        original_validate = private_pdf_uat._validate_prepared_run
        first = True

        def reject_first(value, supplied, runtime):
            nonlocal first
            if first:
                first = False
                raise PrivatePdfUatError("injected prepared-run rejection")
            return original_validate(value, supplied, runtime)

        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), patch.object(
            private_pdf_uat, "_validate_prepared_run", side_effect=reject_first
        ), self.assertRaisesRegex(PrivatePdfUatError, "^private PDF UAT failed$"):
            run_private_pdf_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertEqual([], cleanup_errors)
        self.assertFalse((repository_root() / "sources/.private-pdf-uat").exists())
        self.assertEqual(
            preexisting_staging,
            set((repository_root() / "sources").glob("private-pdf-uat-*")),
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            retry = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        load_or_initialize_checkpoint(
            retry.batch_manifest,
            retry.batch_manifest_digest,
            batch_root=self.runtime / "batches" / retry.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(retry, {})

    def test_prepare_recovers_exact_interrupted_orphan_before_new_run(self):
        orphan = self.prepare_default_run()
        checkpoint = load_or_initialize_checkpoint(
            orphan.batch_manifest,
            orphan.batch_manifest_digest,
            batch_root=self.runtime / "batches" / orphan.batch_manifest["batch_id"],
        )
        self.assertEqual(0, checkpoint["processed"])
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            replacement = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertNotEqual(
            orphan.batch_manifest["batch_id"],
            replacement.batch_manifest["batch_id"],
        )
        self.assertFalse(
            (repository_root() / "sources" / orphan.staging_name).exists()
        )
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=(
                self.runtime / "batches" / replacement.batch_manifest["batch_id"]
            ),
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_prepare_recovers_exact_orphan_when_control_is_already_absent(self):
        orphan = self.prepare_default_run()
        load_or_initialize_checkpoint(
            orphan.batch_manifest,
            orphan.batch_manifest_digest,
            batch_root=self.runtime / "batches" / orphan.batch_manifest["batch_id"],
        )
        control = repository_root() / "sources/.private-pdf-uat"
        (control / "run-manifest.json").unlink()
        control.rmdir()
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            replacement = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertFalse(
            (repository_root() / "sources" / orphan.staging_name).exists()
        )
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=(
                self.runtime / "batches" / replacement.batch_manifest["batch_id"]
            ),
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_prepare_persists_intent_before_creating_random_staging(self):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir()
        (benchmarks / "docling-2.118.1.json").write_bytes(
            private_pdf_uat._manifest_body(self.qualification)
        )
        real_open = private_pdf_uat._open_directory

        def require_intent_before_staging(path, root, *, create):
            selected = Path(path)
            if (
                create
                and selected.parent == repository_root() / "sources"
                and selected.name.startswith("private-pdf-uat-")
            ):
                self.assertTrue((
                    self.runtime / "uat/private-pdf/preparation-intent.json"
                ).is_file())
            return real_open(path, root, create=create)

        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), patch.object(
            private_pdf_uat, "_open_directory", side_effect=require_intent_before_staging
        ):
            prepared = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertFalse((
            self.runtime / "uat/private-pdf/preparation-intent.json"
        ).exists())
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_prepare_intent_rejects_reappeared_foreign_staging(self):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir()
        (benchmarks / "docling-2.118.1.json").write_bytes(
            private_pdf_uat._manifest_body(self.qualification)
        )
        real_persist = private_pdf_uat._persist_bytes_at
        interrupted = False

        def interrupt_after_first_source(descriptor, name, body):
            nonlocal interrupted
            result = real_persist(descriptor, name, body)
            if name == "seed-01.pdf" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), patch.object(
            private_pdf_uat, "_persist_bytes_at", side_effect=interrupt_after_first_source
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        intent_path = self.runtime / "uat/private-pdf/preparation-intent.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        foreign = repository_root() / "sources" / intent["staging_name"]
        retained = foreign.with_name(foreign.name + "-owned-retained")
        foreign.rename(retained)
        foreign.mkdir()
        (foreign / "foreign").write_bytes(b"preserve")
        self.addCleanup(shutil.rmtree, foreign, True)
        self.addCleanup(shutil.rmtree, retained, True)
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), self.assertRaises(
            PrivatePdfUatError
        ):
            private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertEqual(b"preserve", (foreign / "foreign").read_bytes())

    def test_prepare_intent_resumes_exact_owned_partial_staging(self):
        corpus = self.runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
        inputs = corpus / "input"
        inputs.mkdir(parents=True)
        for item_id, body in self.source_bodies.items():
            (inputs / f"{item_id}.pdf").write_bytes(body)
        (corpus / "manifest.json").write_bytes(
            private_pdf_uat._manifest_body(self.private_manifest)
        )
        benchmarks = self.runtime / "benchmarks"
        benchmarks.mkdir()
        (benchmarks / "docling-2.118.1.json").write_bytes(
            private_pdf_uat._manifest_body(self.qualification)
        )
        real_persist = private_pdf_uat._persist_bytes_at
        interrupted = False

        def interrupt_after_first_source(descriptor, name, body):
            nonlocal interrupted
            result = real_persist(descriptor, name, body)
            if name == "seed-01.pdf" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), patch.object(
            private_pdf_uat, "_persist_bytes_at", side_effect=interrupt_after_first_source
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        intent_path = self.runtime / "uat/private-pdf/preparation-intent.json"
        old_stage = json.loads(intent_path.read_text(encoding="utf-8"))["staging_name"]
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            prepared = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertFalse((repository_root() / "sources" / old_stage).exists())
        self.assertFalse(intent_path.exists())
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_worker_journal_counts_actual_events_and_detects_rerun_conversion(self):
        prepared = self.prepare_default_run()
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=type("Process", (), {})()):
            private_pdf_uat._default_launch_process(prepared, "resume")
        from ao_lore.private_pdf_uat_worker import (
            _append_conversion_event, _load_run_contract,
        )
        sandbox_env = json.loads(
            (prepared.conversion_journal.parent / "worker-run-contract.json").read_text()
        )["sandbox"]["environment"]
        with patch.dict(os.environ, sandbox_env, clear=True):
            held = _load_run_contract()
            try:
                _append_conversion_event(held)
                self.assertEqual(1, private_pdf_uat._read_conversion_journal(prepared))
                resumed = private_pdf_uat._read_conversion_journal(prepared)
                _append_conversion_event(held)
                rerun = private_pdf_uat._read_conversion_journal(prepared)
            finally:
                held.close()
        self.assertEqual(2, rerun)
        self.assertNotEqual(resumed, rerun)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_worker_rejects_post_validation_journal_swap_without_mutating_replacement(self):
        prepared = self.prepare_default_run()
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=type("Process", (), {})()):
            private_pdf_uat._default_launch_process(prepared, "resume")
        from ao_lore import private_pdf_uat_worker as worker
        sandbox_env = json.loads(
            (prepared.conversion_journal.parent / "worker-run-contract.json").read_text()
        )["sandbox"]["environment"]
        with patch.dict(os.environ, sandbox_env, clear=True):
            held = worker._load_run_contract()
            journal = prepared.conversion_journal
            retained = journal.with_name("conversion-journal.retained")
            replacement = b"foreign replacement must remain unchanged\n"
            os.replace(journal, retained)
            journal.write_bytes(replacement)
            try:
                with self.assertRaises(OSError):
                    worker._append_conversion_event(held)
                self.assertEqual(replacement, journal.read_bytes())
                self.assertEqual(b"", retained.read_bytes())
                with self.assertRaises(PrivatePdfUatError):
                    private_pdf_uat._read_conversion_journal(prepared)
            finally:
                held.close()
                journal.unlink()
                os.replace(retained, journal)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_worker_rejects_cache_outside_runtime(self):
        prepared = self.prepare_default_run()
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=type("Process", (), {})()):
            private_pdf_uat._default_launch_process(prepared, "resume")
        from ao_lore import private_pdf_uat_worker as worker
        with patch.dict(os.environ, {
            "AO_LORE_HOME": str(self.runtime),
            "HF_HOME": str(repository_root() / ".foreign-cache"),
        }), self.assertRaisesRegex(OSError, "cache environment is invalid"):
            worker._load_run_contract()
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_worker_cache_rejects_symlinked_directory_chain_and_nested_directory(self):
        from ao_lore import private_pdf_uat_worker as worker
        self.assert_no_new_repository_staging()

        def populate(root):
            hub = root / "cache/huggingface/hub"
            for model in worker._REQUIRED_OFFLINE_MODELS:
                snapshot = hub / model / "snapshots/test-snapshot"
                snapshot.mkdir(parents=True)
                (snapshot / "model.bin").write_bytes(b"contained fixture")
            return hub

        for component in ("huggingface", "hub", "model", "snapshots", "snapshot"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(
                dir=self.runtime.parent
            ) as temporary:
                root = Path(temporary) / "runtime"
                hub = populate(root)
                model = hub / worker._REQUIRED_OFFLINE_MODELS[0]
                targets = {
                    "huggingface": root / "cache/huggingface",
                    "hub": hub,
                    "model": model,
                    "snapshots": model / "snapshots",
                    "snapshot": model / "snapshots/test-snapshot",
                }
                selected = targets[component]
                displaced = selected.with_name(selected.name + "-real")
                selected.rename(displaced)
                selected.symlink_to(displaced, target_is_directory=True)
                with patch.dict(os.environ, {
                    "HF_HOME": str(root / "cache/huggingface")
                }), self.assertRaisesRegex(OSError, "worker cache is invalid"):
                    worker._validate_offline_cache(root)

        with tempfile.TemporaryDirectory(dir=self.runtime.parent) as temporary:
            root = Path(temporary) / "runtime"
            hub = populate(root)
            snapshot = (
                hub / worker._REQUIRED_OFFLINE_MODELS[0]
                / "snapshots/test-snapshot"
            )
            weights = hub / "contained-weights"
            weights.mkdir()
            (weights / "weights.bin").write_bytes(b"weights")
            (snapshot / "weights").symlink_to(weights, target_is_directory=True)
            with patch.dict(os.environ, {
                "HF_HOME": str(root / "cache/huggingface")
            }), self.assertRaisesRegex(OSError, "worker cache is invalid"):
                worker._validate_offline_cache(root)

    def test_worker_cache_accepts_only_contained_regular_file_symlinks(self):
        from ao_lore import private_pdf_uat_worker as worker
        self.assert_no_new_repository_staging()

        hub = self.runtime / "cache/huggingface/hub"
        blobs = hub / "blobs"
        blobs.mkdir(parents=True)
        for index in range(14):
            (blobs / f"blob-{index}").write_bytes(f"blob-{index}".encode())
        for model_index, model in enumerate(worker._REQUIRED_OFFLINE_MODELS):
            snapshot = hub / model / "snapshots/test-snapshot"
            snapshot.mkdir(parents=True)
            for offset in range(7):
                index = model_index * 7 + offset
                (snapshot / f"weight-{index}.bin").symlink_to(
                    blobs / f"blob-{index}"
                )
        with patch.dict(os.environ, {
            "HF_HOME": str(self.runtime / "cache/huggingface")
        }):
            worker._validate_offline_cache(self.runtime)

        external = self.runtime.parent / "external-weight.bin"
        external.write_bytes(b"external")
        (hub / worker._REQUIRED_OFFLINE_MODELS[0] / "snapshots/test-snapshot/escape.bin").symlink_to(
            external
        )
        with patch.dict(os.environ, {
            "HF_HOME": str(self.runtime / "cache/huggingface")
        }), self.assertRaisesRegex(OSError, "worker cache is invalid"):
            worker._validate_offline_cache(self.runtime)

    def test_worker_accepts_parent_qualification_between_64k_and_1mib(self):
        from ao_lore import private_pdf_uat_worker as worker
        self.assert_no_new_repository_staging()

        self.qualification = build_manifest(
            fixture_corpus_digest=private_pdf_uat._QUALIFIED_PDF_CORPUS_DIGEST,
            parser_id="docling",
            parser_version="2.118.1",
            parser_configuration_digest=private_pdf_uat.PRIVATE_PDF_CONFIGURATION_DIGEST,
            runtime="python-test",
            platform="linux-test",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["x" * (73 * 1024)]],
            raw_metrics={}, normalized_scores={}, failures=[], exclusions=[],
        )
        qualification_body = json.dumps(
            self.qualification, sort_keys=True, separators=(",", ":")
        ).encode()
        prepared = self.prepare_default_run(
            qualification_body=qualification_body
        )
        self.assertGreater(len(prepared.qualification_body), 64 * 1024)
        self.assertLessEqual(
            len(prepared.qualification_body),
            private_pdf_uat._MAX_QUALIFICATION_BYTES,
        )
        private_pdf_uat._validate_prepared_run(
            prepared, self.private_manifest, self.runtime
        )
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=type("Process", (), {})()):
            private_pdf_uat._default_launch_process(prepared, "resume")
        sandbox_env = json.loads(
            (prepared.conversion_journal.parent / "worker-run-contract.json").read_text()
        )["sandbox"]["environment"]
        with patch.dict(os.environ, sandbox_env, clear=True):
            held = worker._load_run_contract()
            held.close()

    def test_parent_and_worker_reject_qualification_above_1mib(self):
        from ao_lore import private_pdf_uat_worker as worker
        self.assert_no_new_repository_staging()

        prepared = self.prepare_default_run()
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=type("Process", (), {})()):
            private_pdf_uat._default_launch_process(prepared, "resume")
        oversized = prepared.qualification_body + b" " * (
            private_pdf_uat._MAX_QUALIFICATION_BYTES
            + 1 - len(prepared.qualification_body)
        )
        prepared.qualification_locator.write_bytes(oversized)
        with self.assertRaisesRegex(
            PrivatePdfUatError, "qualification binding is invalid"
        ):
            private_pdf_uat._validate_prepared_run(
                prepared, self.private_manifest, self.runtime
            )
        sandbox_env = json.loads(
            (prepared.conversion_journal.parent / "worker-run-contract.json").read_text()
        )["sandbox"]["environment"]
        with patch.dict(os.environ, sandbox_env, clear=True), self.assertRaises(ContractError):
            worker._load_run_contract()

    def test_worker_network_guard_is_unswallowable_and_restores_socket_apis(self):
        from ao_lore import private_pdf_uat_worker as worker
        held = type("Held", (), {"network_attempts": []})()
        originals = (
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
        )
        caught: list[bool] = []
        with self.assertRaises(worker._NetworkAttempt):
            with worker._network_guard(held):
                try:
                    socket.create_connection(("127.0.0.1", 9))
                except Exception:
                    caught.append(True)
        self.assertEqual([], caught)
        self.assertEqual(["create_connection"], held.network_attempts)
        self.assertEqual(originals, (
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
        ))
        for api in ("connect", "connect_ex"):
            held.network_attempts.clear()
            with socket.socket() as channel, self.assertRaises(worker._NetworkAttempt):
                with worker._network_guard(held):
                    getattr(channel, api)(("127.0.0.1", 9))
            self.assertEqual([api], held.network_attempts)
            self.assertEqual(originals, (
                socket.socket.connect,
                socket.socket.connect_ex,
                socket.create_connection,
            ))

    def test_worker_main_restores_backend_on_repeated_and_exceptional_embedded_runs(self):
        from ao_lore import private_pdf_uat_worker as worker
        from ao_lore.docling_pdf import DoclingBackend

        class Held:
            def __init__(self):
                self.contract = {"phase": "resume", "batch_manifest_digest": DIGEST_A}
                self.batch = {"batch_id": "batch-private-uat-00000001"}
                self.qualification = {}
                self.sources = {}
                self.network_attempts = []
                self.closed = 0

            def close(self):
                self.closed += 1

        original_convert = DoclingBackend.convert
        outcomes = ({}, RuntimeError("ordinary"), KeyboardInterrupt(), SystemExit(7))
        for outcome in outcomes:
            held = Held()
            side_effect = outcome if isinstance(outcome, BaseException) else None
            with (
                patch.object(worker, "_load_run_contract", return_value=held),
                patch("ao_lore.__main__._ingest_profiles", return_value=({}, {})),
                patch.object(
                    worker, "ingest_verified_batch",
                    return_value=outcome if side_effect is None else None,
                    side_effect=side_effect,
                ),
                patch("builtins.print"),
            ):
                if side_effect is None:
                    self.assertEqual(0, worker.main())
                else:
                    with self.assertRaises(type(side_effect)):
                        worker.main()
            self.assertIs(original_convert, DoclingBackend.convert)
            self.assertEqual(1, held.closed)

    def test_worker_main_rejects_network_attempt_even_when_adapter_catches_baseexception(self):
        from ao_lore import private_pdf_uat_worker as worker

        class Held:
            contract = {"phase": "rerun", "batch_manifest_digest": DIGEST_A}
            batch = {"batch_id": "batch-private-uat-00000001"}
            qualification = {}
            sources = {}

            def __init__(self):
                self.network_attempts = []
                self.closed = False

            def close(self):
                self.closed = True

        held = Held()

        def caught_attempt(*_args, **_kwargs):
            try:
                socket.create_connection(("127.0.0.1", 9))
            except BaseException:
                pass
            return {}

        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            patch("ao_lore.__main__._ingest_profiles", return_value=({}, {})),
            patch.object(worker, "ingest_verified_batch", side_effect=caught_attempt),
            self.assertRaises(worker._NetworkAttempt),
        ):
            worker.main()
        self.assertEqual(["create_connection"], held.network_attempts)
        self.assertTrue(held.closed)

    def test_worker_main_restores_backend_when_profile_loading_fails(self):
        from ao_lore import private_pdf_uat_worker as worker
        from ao_lore.docling_pdf import DoclingBackend

        held = type("Held", (), {
            "contract": {"phase": "resume"},
            "batch": {},
            "qualification": {},
            "sources": {},
            "network_attempts": [],
            "close": lambda self: None,
        })()
        original = DoclingBackend.convert
        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            patch(
                "ao_lore.__main__._ingest_profiles",
                side_effect=RuntimeError("profile failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            worker.main()
        self.assertIs(original, DoclingBackend.convert)

    def test_worker_guard_precedes_interrupted_phase_validation_callback(self):
        from ao_lore import private_pdf_uat_worker as worker

        class Held:
            contract = {"phase": "interrupted"}
            network_attempts: list[str] = []
            closed = False

            def close(self):
                self.closed = True

        held = Held()
        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            self.assertRaises(worker._NetworkAttempt),
        ):
            worker.main(after_validation=lambda _held: socket.create_connection(
                ("127.0.0.1", 9)
            ))
        self.assertEqual(["create_connection"], held.network_attempts)
        self.assertTrue(held.closed)

    def test_interrupted_worker_caught_conversion_attempt_aborts_before_accounting(self):
        from ao_lore import private_pdf_uat_worker as worker
        from ao_lore.docling_pdf import DoclingBackend

        held = type("Held", (), {
            "contract": {"phase": "interrupted", "batch_manifest_digest": DIGEST_A},
            "batch": {"batch_id": "batch-private-uat-00000001"},
            "qualification": {}, "sources": {}, "network_attempts": [],
            "close": lambda self: None,
        })()

        def caught_conversion(_backend, *_args, **_kwargs):
            try:
                socket.create_connection(("127.0.0.1", 9))
            except BaseException:
                pass
            return object()

        def invoke_conversion(*_args, **_kwargs):
            DoclingBackend.convert(object(), b"pdf", "source.pdf")
            return {}

        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            patch("ao_lore.__main__._ingest_profiles", return_value=({}, {})),
            patch.object(DoclingBackend, "convert", caught_conversion),
            patch.object(worker, "ingest_verified_batch", side_effect=invoke_conversion),
            patch.object(worker, "_journal_count", return_value=0),
            patch.object(worker, "_append_conversion_event") as append,
            patch.object(worker, "_persist_barrier") as barrier,
            patch.object(signal, "pause", side_effect=RuntimeError("barrier reached")),
            self.assertRaises(worker._NetworkAttempt),
        ):
            worker.main()
        append.assert_not_called()
        barrier.assert_not_called()

    def test_interrupted_worker_preexisting_attempt_aborts_before_second_call_barrier(self):
        from ao_lore import private_pdf_uat_worker as worker
        from ao_lore.docling_pdf import DoclingBackend

        held = type("Held", (), {
            "contract": {"phase": "interrupted", "batch_manifest_digest": DIGEST_A},
            "batch": {"batch_id": "batch-private-uat-00000001"},
            "qualification": {}, "sources": {}, "network_attempts": ["connect"],
            "close": lambda self: None,
        })()

        def invoke_conversion(*_args, **_kwargs):
            DoclingBackend.convert(object(), b"pdf", "source.pdf")
            return {}

        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            patch("ao_lore.__main__._ingest_profiles", return_value=({}, {})),
            patch.object(worker, "ingest_verified_batch", side_effect=invoke_conversion),
            patch.object(worker, "_journal_count", return_value=1),
            patch.object(worker, "_append_conversion_event") as append,
            patch.object(worker, "_persist_barrier") as barrier,
            patch.object(signal, "pause", side_effect=RuntimeError("barrier reached")),
            self.assertRaises(worker._NetworkAttempt),
        ):
            worker.main()
        append.assert_not_called()
        barrier.assert_not_called()

    def test_worker_accounts_ordinary_parsing_error_once_and_reraises_identity(self):
        from ao_lore import private_pdf_uat_worker as worker
        from ao_lore.docling_pdf import DoclingBackend

        held = type("Held", (), {
            "contract": {"phase": "resume", "batch_manifest_digest": DIGEST_A},
            "batch": {"batch_id": "batch-private-uat-00000001"},
            "qualification": {}, "sources": {}, "network_attempts": [],
            "close": lambda self: None,
        })()
        expected = ParsingError("ordinary parser rejection")

        def reject(_backend, *_args, **_kwargs):
            raise expected

        def invoke(*_args, **_kwargs):
            DoclingBackend.convert(object(), b"pdf", "source.pdf")

        with (
            patch.object(worker, "_load_run_contract", return_value=held),
            patch("ao_lore.__main__._ingest_profiles", return_value=({}, {})),
            patch.object(DoclingBackend, "convert", reject),
            patch.object(worker, "ingest_verified_batch", side_effect=invoke),
            patch.object(worker, "_journal_count", return_value=0),
            patch.object(worker, "_append_conversion_event") as append,
            self.assertRaises(ParsingError) as raised,
        ):
            worker.main()
        self.assertIs(expected, raised.exception)
        append.assert_called_once_with(held)

    def test_default_launch_persists_exact_phase_bound_worker_contract(self):
        prepared = self.prepare_default_run()
        process = type("Process", (), {})()
        with patch.object(
            private_pdf_uat, "_probe_private_pdf_sandbox", return_value="bubblewrap 0.8.0"
        ), patch.object(subprocess, "Popen", return_value=process):
            returned = private_pdf_uat._default_launch_process(
                prepared, "interrupted"
            )
        self.assertIs(process, returned)
        contract_path = prepared.conversion_journal.parent / "worker-run-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual("interrupted", contract["phase"])
        self.assertEqual(prepared.batch_manifest["batch_id"], contract["batch_id"])
        self.assertEqual(
            list(prepared.staging_source_identity),
            contract["staging_identity"],
        )
        self.assertFalse(contract["authority"])
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})

    def test_uat_cleanup_recovers_after_each_file_quarantine_interruption(self):
        prepared = self.prepare_default_run()
        verifier = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        real_unlink = private_pdf_uat._unlink_verified_at
        interrupted = False

        def interrupt_after_first_unlink(*args, **kwargs):
            nonlocal interrupted
            result = real_unlink(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(
            private_pdf_uat, "_unlink_verified_at",
            side_effect=interrupt_after_first_unlink,
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        marker = prepared.conversion_journal.parent / "cleanup-recovery.json"
        self.assertTrue(marker.is_file())
        recovery = json.loads(marker.read_text(encoding="utf-8"))
        reclaiming = [
            state for state in recovery["states"].values()
            if state["phase"] == "reclaiming"
        ]
        self.assertEqual(1, len(reclaiming))
        self.assertIsInstance(reclaiming[0]["pending"], str)
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            replacement = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertFalse(marker.exists())
        self.assertFalse(
            (repository_root() / "sources" / prepared.staging_name).exists()
        )
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=self.runtime / "batches" / replacement.batch_manifest["batch_id"],
        )
        private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_uat_cleanup_rejects_namespace_swap_and_preserves_foreign_tree(self):
        prepared = self.prepare_default_run()
        verifier = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        real_rename = private_pdf_uat._rename_noreplace_at
        interrupted = False

        def interrupt_after_first_move(*args, **kwargs):
            nonlocal interrupted
            result = real_rename(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(
            private_pdf_uat, "_rename_noreplace_at",
            side_effect=interrupt_after_first_move,
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        stage = repository_root() / "sources" / prepared.staging_name
        displaced = repository_root() / "sources" / f"{prepared.staging_name}-retained"
        stage.rename(displaced)
        stage.mkdir()
        (stage / "foreign").write_bytes(b"preserve")
        try:
            with patch.object(private_pdf_uat, "_verify_pdf", return_value=None), self.assertRaises(
                PrivatePdfUatError
            ):
                private_pdf_uat._default_prepare_uat_run(
                    self.private_manifest, self.runtime
                )
            self.assertEqual(b"preserve", (stage / "foreign").read_bytes())
        finally:
            shutil.rmtree(stage)
            displaced.rename(stage)
        with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
            replacement = private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=self.runtime / "batches" / replacement.batch_manifest["batch_id"],
        )
        private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_cleanup_marker_cannot_authorize_unrelated_source_deletion(self):
        prepared = self.prepare_default_run()
        verifier = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)
        stage = repository_root() / "sources" / prepared.staging_name
        self.addCleanup(shutil.rmtree, stage, True)
        foreign = repository_root() / "sources/private-pdf-uat-foreign-preserve"
        foreign.mkdir()
        (foreign / "foreign.bin").write_bytes(b"must survive")
        self.addCleanup(shutil.rmtree, foreign, True)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(
            private_pdf_uat, "_rename_noreplace_at", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        marker = prepared.conversion_journal.parent / "cleanup-recovery.json"
        forged = json.loads(marker.read_text(encoding="utf-8"))
        forged["targets"] = [private_pdf_uat._capture_uat_cleanup_target(
            prepared,
            "sources",
            foreign.name,
            {"foreign.bin": (
                private_pdf_uat._MAX_MANIFEST_BYTES,
                "sha256:" + hashlib.sha256(b"must survive").hexdigest(),
            )},
        )]
        marker.write_bytes(private_pdf_uat._manifest_body(forged))
        with self.assertRaises((PrivatePdfUatError, OSError)):
            private_pdf_uat._default_prepare_uat_run(
                self.private_manifest, self.runtime
            )
        self.assertEqual(b"must survive", (foreign / "foreign.bin").read_bytes())

    def test_cleanup_recovers_marker_fsynced_before_quarantine_creation(self):
        prepared = self.prepare_default_run()
        verifier = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        real_open = private_pdf_uat._open_directory

        def interrupt_quarantine_create(path, root, *, create):
            if create and Path(path).name.startswith("cleanup-quarantine-"):
                raise KeyboardInterrupt()
            return real_open(path, root, create=create)

        with patch.object(
            private_pdf_uat, "_open_directory", side_effect=interrupt_quarantine_create
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        marker = prepared.conversion_journal.parent / "cleanup-recovery.json"
        recovery = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIsNone(recovery["quarantine_identity"])
        self.assertTrue(all(
            state["phase"] == "planned" for state in recovery["states"].values()
        ))
        replacement = private_pdf_uat._default_prepare_uat_run(
            self.private_manifest, self.runtime
        )
        self.assertFalse(marker.exists())
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=self.runtime / "batches" / replacement.batch_manifest["batch_id"],
        )
        private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_cleanup_recovers_quarantine_created_before_first_rename(self):
        prepared = self.prepare_default_run()
        verifier = patch.object(private_pdf_uat, "_verify_pdf", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)
        load_or_initialize_checkpoint(
            prepared.batch_manifest,
            prepared.batch_manifest_digest,
            batch_root=self.runtime / "batches" / prepared.batch_manifest["batch_id"],
        )
        with patch.object(
            private_pdf_uat, "_rename_noreplace_at", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            private_pdf_uat._cleanup_partial_uat_state(prepared, {})
        marker = prepared.conversion_journal.parent / "cleanup-recovery.json"
        recovery = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIsNotNone(recovery["quarantine_identity"])
        self.assertTrue(all(
            state["phase"] == "planned" for state in recovery["states"].values()
        ))
        replacement = private_pdf_uat._default_prepare_uat_run(
            self.private_manifest, self.runtime
        )
        self.assertFalse(marker.exists())
        load_or_initialize_checkpoint(
            replacement.batch_manifest,
            replacement.batch_manifest_digest,
            batch_root=self.runtime / "batches" / replacement.batch_manifest["batch_id"],
        )
        private_pdf_uat._cleanup_partial_uat_state(replacement, {})

    def test_live_checkpoint_reader_does_not_wait_for_writer_flock(self):
        prepared = self.dependencies().prepare_run(self.private_manifest, self.runtime)
        batch = self.runtime / "batches" / prepared.batch_manifest["batch_id"]
        batch.mkdir(parents=True)
        (batch / "checkpoint.json").write_bytes(
            private_pdf_uat._manifest_body(self.checkpoint)
        )
        code = (
            "import fcntl,os,sys,time; "
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
            "fcntl.flock(fd,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(5)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(batch)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("ready", child.stdout.readline().strip())
            started = time.monotonic()
            observed = private_pdf_uat._read_live_checkpoint_snapshot(prepared)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(1, observed["processed"])
            os.kill(child.pid, signal.SIGTERM)
            child.wait(timeout=2)
            self.assertEqual(-signal.SIGTERM, child.returncode)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=2)
            child.stdout.close()


@private_calibration
class PrivatePdfReadbackTests(unittest.TestCase):
    def test_candidate_change_is_limited_to_representative_corpus(self):
        seed = readback()
        seed["tuning_decision"] = "candidate_change"
        with self.assertRaises(PrivatePdfUatError):
            validate_private_pdf_readback(
                seed, expected_item_ids=["seed-01", "seed-02"]
            )
        representative = readback()
        representative["corpus_id"] = private_pdf_uat.DOMAIN_CORPUS_ID
        representative["tuning_decision"] = "candidate_change"
        validated = validate_private_pdf_readback(
            representative, expected_item_ids=["seed-01", "seed-02"]
        )
        self.assertEqual("candidate_change", validated["tuning_decision"])

    def test_readback_accepts_valid_value_and_returns_detached_copy(self):
        value = readback()
        validated = validate_private_pdf_readback(value, expected_item_ids=["seed-01", "seed-02"])
        value["results"][0]["item_id"] = "mutated"
        self.assertEqual("seed-01", validated["results"][0]["item_id"])

    def test_readback_preserves_actual_closed_candidate_review_status(self):
        for status in ("unreviewed", "accepted", "rejected"):
            value = readback()
            value["results"][0]["review_status"] = status
            validated = validate_private_pdf_readback(
                value, expected_item_ids=["seed-01", "seed-02"]
            )
            self.assertEqual(status, validated["results"][0]["review_status"])
        value = readback()
        value["results"][0]["review_status"] = "pending"
        with self.assertRaises(PrivatePdfUatError):
            validate_private_pdf_readback(
                value, expected_item_ids=["seed-01", "seed-02"]
            )

    def test_readback_accepts_all_rejected_partial_when_expected_order_matches(self):
        validated = validate_private_pdf_readback(
            all_rejected_readback(), expected_item_ids=["seed-01", "seed-02"]
        )
        self.assertEqual({}, validated["aggregate"]["metrics"])
        self.assertEqual(
            ["structural_fidelity", "text_fidelity", "source_location_fidelity"],
            validated["aggregate"]["exclusions"],
        )
        self.assertEqual(["seed-01", "seed-02"], [item["item_id"] for item in validated["results"]])

    def test_readback_accepts_terminal_nonascending_order_when_context_matches(self):
        value = readback()
        value["results"] = [value["results"][1], value["results"][0]]
        validated = validate_private_pdf_readback(
            value, expected_item_ids=["seed-02", "seed-01"]
        )
        self.assertEqual(["seed-02", "seed-01"], [item["item_id"] for item in validated["results"]])

    def test_readback_rejects_arithmetic_status_privacy_and_authority_drift(self):
        invalid = []
        for metric_name in ("structural_fidelity", "text_fidelity", "source_location_fidelity"):
            value = readback()
            value["aggregate"]["metrics"][metric_name] = True
            invalid.append(value)
        for summary_name in ("latency_seconds", "peak_memory_bytes"):
            for field in ("minimum", "maximum", "mean"):
                value = readback()
                value["aggregate"][summary_name][field] = True
                invalid.append(value)
        value = readback()
        value["aggregate"]["repeatability"]["score"] = True
        invalid.append(value)
        value = readback()
        value["counts"]["created"] = True
        invalid.append(value)
        value = readback()
        with self.assertRaises(PrivatePdfUatError):
            validate_private_pdf_readback(value)
        value = readback()
        value["counts"]["processed"] = 1
        invalid.append(value)
        value = readback()
        value["counts"]["successful"] = 2
        invalid.append(value)
        value = readback()
        value["counts"]["candidate_queue"] = 0
        invalid.append(value)
        value = readback()
        value["counts"]["rerun"] = 0
        invalid.append(value)
        value = readback()
        value["conversion_counts"]["rerun"] = 1
        invalid.append(value)
        value = readback()
        value["conversion_counts"]["initial"] = 2
        invalid.append(value)
        value = readback()
        value["lifecycle_status"] = "completed"
        invalid.append(value)
        value = readback()
        value["tuning_decision"] = "candidate_change"
        invalid.append(value)
        value = readback()
        value["brain_after_digest"] = DIGEST_A
        invalid.append(value)
        value = readback()
        value["aggregate"]["metrics"]["text_fidelity"] = float("nan")
        invalid.append(value)
        value = readback()
        value["aggregate"]["peak_memory_bytes"]["mean"] = float("inf")
        invalid.append(value)
        value = readback()
        value["aggregate"]["repeatability"]["runs"] = True
        invalid.append(value)
        value = readback()
        value["aggregate"]["exclusions"] = ["source_location_fidelity", "source_location_fidelity"]
        invalid.append(value)
        value = readback()
        value["aggregate"]["stable_failures"]["invalid_pdf"] = -1
        invalid.append(value)
        value = readback()
        value["results"][0]["candidate_id"] = "private/seed-01"
        invalid.append(value)
        value = readback()
        value["results"][1]["error_code"] = "exception:/private/seed-02.pdf"
        invalid.append(value)
        value = readback()
        value["results"][1]["content"] = "secret document content"
        invalid.append(value)
        value = readback()
        value["provider_calls"] = True
        invalid.append(value)
        value = readback()
        value["promotion_authority"] = True
        invalid.append(value)
        value = readback()
        value["claims_authority_advance"] = True
        invalid.append(value)
        value = readback()
        value["network_accessed"] = True
        invalid.append(value)
        value = readback()
        value["qualified"] = True
        invalid.append(value)
        value = readback()
        value["results"].append(copy.deepcopy(value["results"][0]))
        invalid.append(value)
        value = readback()
        value["results"] = list(reversed(value["results"]))
        invalid.append(value)
        value = all_rejected_readback()
        value["aggregate"]["exclusions"] = ["structural_fidelity", "text_fidelity"]
        invalid.append(value)
        value = all_rejected_readback()
        value["counts"]["successful"] = 1
        invalid.append(value)
        value = all_rejected_readback()
        value["counts"]["candidate_queue"] = 1
        invalid.append(value)
        value = all_rejected_readback()
        value["lifecycle_status"] = "completed"
        invalid.append(value)
        value = not_supplied_readback()
        value["provider_calls"] = True
        invalid.append(value)
        value = not_supplied_readback()
        value["unknown"] = True
        invalid.append(value)
        value = not_supplied_readback()
        value["tuning_decision"] = "hold"
        invalid.append(value)
        value = not_supplied_readback()
        value["counts"] = {"total": 0}
        invalid.append(value)
        value = not_supplied_readback()
        value["network_attempts"] = 0
        invalid.append(value)
        value = readback()
        value["network_attempts"] = 0
        invalid.append(value)
        for expected in (
            ["seed-01", "seed-03"],
            ["seed-01", "seed-01"],
            ["Bad ID", "seed-02"],
            [],
            "seed-01",
        ):
            with self.subTest(expected=expected), self.assertRaises(PrivatePdfUatError):
                validate_private_pdf_readback(readback(), expected_item_ids=expected)
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(PrivatePdfUatError):
                validate_private_pdf_readback(changed, expected_item_ids=["seed-01", "seed-02"])

    def test_readback_accepts_explicit_not_supplied_union_and_rejects_mixed_fields(self):
        validated = validate_private_pdf_readback(not_supplied_readback())
        self.assertEqual("not_supplied", validated["lifecycle_status"])
        self.assertEqual(
            {
                "schema_version",
                "lifecycle_status",
                "qualified",
                "ocr_used",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            },
            set(validated),
        )


@private_calibration
class PrivatePdfSchemaParityTests(unittest.TestCase):
    def test_contract_samples_match_the_json_schemas(self):
        with open("schemas/ao-lore/private-pdf-uat-manifest-v0.1.schema.json", encoding="utf-8") as handle:
            manifest_schema = json.load(handle)
        with open("schemas/ao-lore/private-pdf-uat-readback-v0.1.schema.json", encoding="utf-8") as handle:
            readback_schema = json.load(handle)

        Draft202012Validator.check_schema(manifest_schema)
        Draft202012Validator.check_schema(readback_schema)

        Draft202012Validator(manifest_schema).validate(manifest())
        Draft202012Validator(readback_schema).validate(readback())
        Draft202012Validator(readback_schema).validate(not_supplied_readback())

    def test_schema_examples_reject_unknowns_duplicates_and_closed_enums(self):
        with open(
            "schemas/ao-lore/private-pdf-uat-manifest-v0.1.schema.json",
            encoding="utf-8",
        ) as handle:
            manifest_schema = json.load(handle)
        with open(
            "schemas/ao-lore/private-pdf-uat-readback-v0.1.schema.json",
            encoding="utf-8",
        ) as handle:
            readback_schema = json.load(handle)

        bad_manifest = manifest()
        bad_manifest["documents"][0]["required_block_types"] = ["heading", "unknown"]
        duplicate_anchor_manifest = manifest()
        duplicate_anchor_manifest["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": DIGEST_B,
                "expected_text": ["Shared MIME-info Database"],
                "required_block_types": ["heading"],
            }
        )
        duplicate_types_manifest = manifest()
        duplicate_types_manifest["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": DIGEST_B,
                "expected_text": ["Another anchor"],
                "required_block_types": ["paragraph", "heading"],
            }
        )
        bad_readback = readback()
        bad_readback["results"][1]["error_code"] = "private/path.pdf"
        bad_absent_readback = not_supplied_readback()
        bad_absent_readback["tuning_decision"] = "hold"

        self.assertFalse(Draft202012Validator(manifest_schema).is_valid(bad_manifest))
        self.assertFalse(Draft202012Validator(readback_schema).is_valid(bad_readback))
        self.assertTrue(Draft202012Validator(manifest_schema).is_valid(duplicate_anchor_manifest))
        self.assertTrue(Draft202012Validator(manifest_schema).is_valid(duplicate_types_manifest))
        self.assertFalse(Draft202012Validator(readback_schema).is_valid(bad_absent_readback))
        all_rejected = all_rejected_readback()
        self.assertTrue(Draft202012Validator(readback_schema).is_valid(all_rejected))
        for metric_name in ("structural_fidelity", "text_fidelity", "source_location_fidelity"):
            bad_bool_readback = readback()
            bad_bool_readback["aggregate"]["metrics"][metric_name] = True
            self.assertFalse(Draft202012Validator(readback_schema).is_valid(bad_bool_readback))
        for summary_name in ("latency_seconds", "peak_memory_bytes"):
            for field in ("minimum", "maximum", "mean"):
                bad_bool_readback = readback()
                bad_bool_readback["aggregate"][summary_name][field] = True
                self.assertFalse(Draft202012Validator(readback_schema).is_valid(bad_bool_readback))
        bad_bool_readback = readback()
        bad_bool_readback["aggregate"]["repeatability"]["score"] = True
        self.assertFalse(Draft202012Validator(readback_schema).is_valid(bad_bool_readback))
        self.assertEqual(BLOCK_TYPES, set(manifest_schema["$defs"]["block_type"]["enum"]))


@private_calibration
class PrivatePdfUatScriptTests(unittest.TestCase):
    SCRIPT = repository_root() / "scripts/private-pdf-uat.py"

    def load_script(self):
        spec = importlib.util.spec_from_file_location("private_pdf_uat_script", self.SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def invoke(self, module, arguments, service):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(module, "_load_service", return_value=service), patch.object(
            module.sys, "stdout", stdout
        ), patch.object(module.sys, "stderr", stderr):
            code = module.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def operator_manifest():
        value = manifest()
        value["documents"] = [
            {
                "item_id": f"seed-0{index}",
                "file": f"input/seed-0{index}.pdf",
                "source_digest": DIGEST_A,
                "expected_text": [f"Reviewed public anchor {index}"],
                "required_block_types": block_types,
            }
            for index, block_types in enumerate(
                (
                    ["heading", "paragraph"],
                    ["heading", "paragraph", "list"],
                    ["heading", "table"],
                    ["heading", "paragraph", "image"],
                ),
                start=1,
            )
        ]
        return value

    @staticmethod
    def operator_readback():
        value = readback()
        value["lifecycle_status"] = "completed"
        value["counts"].update(
            total=4, created=4, unchanged=0, rejected=0, processed=4,
            successful=4, candidate_queue=4,
        )
        value["conversion_counts"].update(initial=1, resumed=3, rerun=0)
        value["results"] = [
            {
                "item_id": f"seed-0{index}",
                "source_digest": DIGEST_A,
                "status": "created",
                "candidate_id": f"candidate-seed-0{index}",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "review_status": "accepted",
            }
            for index in range(1, 5)
        ]
        value["aggregate"]["stable_failures"]["invalid_pdf"] = 0
        return value

    @staticmethod
    def service(**overrides):
        defaults = {
            "PRIVATE_PDF_UAT_CORPUS_ID": "ubuntu-pdf-seed-v1",
            "PRIVATE_PDF_UAT_ITEM_IDS": ("seed-01", "seed-02", "seed-03", "seed-04"),
            "prepare_ubuntu_pdf_seed": PrivatePdfUatScriptTests.operator_manifest,
            "run_private_pdf_uat": PrivatePdfUatScriptTests.operator_readback,
            "cleanup_private_pdf_uat": lambda corpus_id: {
                "corpus_id": corpus_id,
                "removed": True,
                "verified_documents": 4,
            },
            "validate_private_pdf_manifest": validate_private_pdf_manifest,
            "validate_private_pdf_readback": validate_private_pdf_readback,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_help_exposes_only_three_fixed_actions_and_json(self):
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--help"],
            cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("{prepare-seed,run,cleanup}", result.stdout)
        self.assertIn("--json", result.stdout)
        for forbidden in (
            "--source", "--root", "--parser", "--ocr", "--provider",
            "--network", "--concurrency", "--retry", "--review", "--promotion",
            "--threshold",
        ):
            self.assertNotIn(forbidden, result.stdout)

    def test_argument_rejection_happens_before_service_import(self):
        module = self.load_script()
        sentinel = "/private/secret.pdf\nTOKEN=secret\x1b[31m"
        for arguments in (
            [],
            ["unknown-action"],
            ["run", "--root", sentinel],
            ["run", "--json=" + sentinel],
            ["prepare-seed", "unexpected"],
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(module, "_load_service") as loader, patch.object(
                module.sys, "stdout", stdout
            ), patch.object(module.sys, "stderr", stderr):
                self.assertEqual(2, module.main(arguments))
            loader.assert_not_called()
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr.getvalue())

    def test_each_action_calls_exactly_one_service_and_emits_canonical_json_once(self):
        module = self.load_script()
        for action, expected_service in (
            ("prepare-seed", "prepare_ubuntu_pdf_seed"),
            ("run", "run_private_pdf_uat"),
            ("cleanup", "cleanup_private_pdf_uat"),
        ):
            calls = []
            service = self.service(
                prepare_ubuntu_pdf_seed=lambda: (
                    calls.append("prepare_ubuntu_pdf_seed")
                    or self.operator_manifest()
                ),
                run_private_pdf_uat=lambda: (
                    calls.append("run_private_pdf_uat")
                    or self.operator_readback()
                ),
                cleanup_private_pdf_uat=lambda corpus_id: (
                    calls.append("cleanup_private_pdf_uat")
                    or {"corpus_id": corpus_id, "removed": True, "verified_documents": 4}
                ),
            )
            output = io.StringIO()
            with patch.object(module, "_load_service", return_value=service), patch.object(
                module.sys, "stdout", output
            ), patch.object(module.sys, "stderr", io.StringIO()), patch.object(
                output, "write", wraps=output.write
            ) as writer, patch.object(output, "flush", wraps=output.flush) as flusher:
                self.assertEqual(0, module.main([action, "--json"]))
            self.assertEqual([expected_service], calls)
            writer.assert_called_once()
            flusher.assert_called_once_with()
            body = output.getvalue()
            self.assertTrue(body.endswith("\n"))
            self.assertEqual(json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")) + "\n", body)
            self.assertNotIn("sha256:", body)
            self.assertNotIn("Shared MIME", body)
            self.assertNotIn("input/", body)

    def test_human_output_is_compact_and_content_free(self):
        module = self.load_script()
        code, stdout, stderr = self.invoke(module, ["run"], self.service())
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1, len(stdout.splitlines()))
        self.assertIn("action=run", stdout)
        self.assertIn("lifecycle_status=completed", stdout)
        self.assertNotIn("sha256:", stdout)
        self.assertNotIn("Shared MIME", stdout)

    def test_malformed_unknown_authority_widened_and_nonserializable_results_reject_before_stdout(self):
        module = self.load_script()
        invalid_results = []
        unknown = manifest()
        unknown["unknown"] = True
        invalid_results.append(("prepare-seed", unknown))
        widened = self.operator_readback()
        widened["promotion_authority"] = True
        invalid_results.append(("run", widened))
        invalid_results.append(("cleanup", {"corpus_id": "ubuntu-pdf-seed-v1", "removed": object(), "verified_documents": 4}))
        for action, value in invalid_results:
            service = self.service(
                prepare_ubuntu_pdf_seed=lambda value=value: value,
                run_private_pdf_uat=lambda value=value: value,
                cleanup_private_pdf_uat=lambda corpus_id, value=value: value,
            )
            code, stdout, stderr = self.invoke(module, [action, "--json"], service)
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr)

    def test_caller_owned_mapping_and_mutable_getter_drift_never_reach_projection(self):
        module = self.load_script()

        class DriftingMapping(dict):
            def __init__(self, initial):
                super().__init__(initial)
                self.reads = 0

            def __getitem__(self, key):
                self.reads += 1
                if key == "removed" and self.reads > 1:
                    return "/private/secret.pdf\nTOKEN=secret"
                return super().__getitem__(key)

        for action, value in (
            ("prepare-seed", DriftingMapping(self.operator_manifest())),
            ("run", DriftingMapping(self.operator_readback())),
            (
                "cleanup",
                DriftingMapping(
                    {
                        "corpus_id": "ubuntu-pdf-seed-v1",
                        "removed": True,
                        "verified_documents": 4,
                    }
                ),
            ),
        ):
            service = self.service(
                prepare_ubuntu_pdf_seed=lambda value=value: value,
                run_private_pdf_uat=lambda value=value: value,
                cleanup_private_pdf_uat=lambda corpus_id, value=value: value,
            )
            code, stdout, stderr = self.invoke(module, [action, "--json"], service)
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr)
            self.assertEqual(0, value.reads)

    def test_hostile_fixed_identity_subclasses_reject_before_service_call(self):
        module = self.load_script()

        class HostileStr(str):
            def __eq__(self, other):
                raise RuntimeError("/private/identity\nTOKEN=secret")

        class HostileTuple(tuple):
            def __eq__(self, other):
                raise RuntimeError("/private/items\nTOKEN=secret")

        cases = (
            {"PRIVATE_PDF_UAT_CORPUS_ID": HostileStr("ubuntu-pdf-seed-v1")},
            {
                "PRIVATE_PDF_UAT_ITEM_IDS": HostileTuple(
                    ("seed-01", "seed-02", "seed-03", "seed-04")
                )
            },
            {
                "PRIVATE_PDF_UAT_ITEM_IDS": (
                    HostileStr("seed-01"), "seed-02", "seed-03", "seed-04"
                )
            },
        )
        for overrides in cases:
            called = []
            service = self.service(
                prepare_ubuntu_pdf_seed=lambda: called.append("service"),
                **overrides,
            )
            code, stdout, stderr = self.invoke(
                module, ["prepare-seed", "--json"], service
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr)
            self.assertEqual([], called)

    def test_ordinary_errors_are_redacted_but_process_control_propagates(self):
        module = self.load_script()
        for failure in (
            RuntimeError("/private/secret.pdf\nTOKEN=secret"),
            ValueError("\x1b[31mprivate\x00text"),
        ):
            service = self.service(run_private_pdf_uat=lambda failure=failure: (_ for _ in ()).throw(failure))
            code, stdout, stderr = self.invoke(module, ["run"], service)
            self.assertEqual((2, "", "ao-lore-private-pdf-uat: rejected\n"), (code, stdout, stderr))
        for failure in (KeyboardInterrupt(), SystemExit(7)):
            service = self.service(run_private_pdf_uat=lambda failure=failure: (_ for _ in ()).throw(failure))
            with patch.object(module, "_load_service", return_value=service), patch.object(
                module.sys, "stdout", io.StringIO()
            ), patch.object(module.sys, "stderr", io.StringIO()), self.assertRaises(type(failure)):
                module.main(["run"])

    def test_stdout_failure_uses_stable_failure_boundary(self):
        module = self.load_script()
        output = SimpleNamespace(write=lambda _body: (_ for _ in ()).throw(OSError("broken pipe /private")))
        stderr = io.StringIO()
        with patch.object(module, "_load_service", return_value=self.service()), patch.object(
            module.sys, "stdout", output
        ), patch.object(module.sys, "stderr", stderr):
            self.assertEqual(2, module.main(["run", "--json"]))
            module.sys.stdout.close()
        self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr.getvalue())

    def test_flush_failure_neutralizes_stdout_before_stable_rejection(self):
        module = self.load_script()

        class FlushFailure(io.StringIO):
            def flush(self):
                raise BrokenPipeError("/private/pipe\nTOKEN=secret")

        output = FlushFailure()
        stderr = io.StringIO()
        with patch.object(module, "_load_service", return_value=self.service()), patch.object(
            module.sys, "stdout", output
        ), patch.object(module.sys, "stderr", stderr):
            self.assertEqual(2, module.main(["run", "--json"]))
            self.assertIsNot(output, module.sys.stdout)
            self.assertFalse(module.sys.stdout.closed)
            module.sys.stdout.close()
        self.assertEqual("ao-lore-private-pdf-uat: rejected\n", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
