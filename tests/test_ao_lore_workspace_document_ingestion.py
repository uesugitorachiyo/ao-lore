import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import ao_lore.workspace_documents as workspace_documents_module
from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_contracts import (
    validate_workspace_document_ingest_readback,
)
from ao_lore.evidence_graph_contracts import AUTHORITY_ROLES
from ao_lore.ingestion import (
    IngestionDependencies,
    IngestionError,
    ingest_workspace_document,
    parse_verified_document,
)
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from ao_lore.workspace_documents import (
    WorkspaceDocumentDependencies,
    load_workspace_documents,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    publish_workspace_registry_generation,
)
from tests.test_ao_lore_ingestion import (
    FakeDistiller,
    FakeParser,
    benchmark,
    docx_benchmark,
)


ROOT = Path(__file__).resolve().parents[1]
DOCX_FIXTURE = ROOT / "tests/fixtures/ao_lore/docx/minimal-paragraph.docx"


def authority() -> dict:
    return {field: False for field in AUTHORITY_FIELDS}


def workspace_definition(*, status: str = "active", version: int = 1,
                         document_generation_digest: str | None = None) -> dict:
    value = {
        "schema_version": "ao.lore.workspace-definition.v0.2",
        "workspace_id": "workspace-a",
        "workspace_version": version,
        "workspace_type": "property",
        "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction",
        "lifecycle_status": status,
        "root_workflow_id": "workflow-workspace-a",
        "root_workflow_digest": canonical_digest("workflow-workspace-a"),
        "source_registry_id": "sources-workspace-a",
        "source_registry_digest": canonical_digest("sources-workspace-a"),
        "graph_id": None,
        "graph_digest": None,
        "freshness_policy_id": None,
        "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed",
        "freshness_summary_id": None,
        "freshness_summary_digest": None,
        "reference_workspace_ids": [],
        "document_store_id": "documents-workspace-a",
        "document_generation_digest": document_generation_digest,
        "definition_digest": "sha256:" + "0" * 64,
        **authority(),
    }
    value["definition_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "definition_digest"}
    )
    return value


def parser_dependencies(data: bytes, *, format_id: str = "pdf", decision: str = "accept"):
    calls = []
    if format_id == "pdf":
        manifest = benchmark()
        parser = FakeParser(
            calls,
            decision=decision,
            benchmark_result_digest=manifest["result_digest"],
            selection_profile_id="workspace-pdf-selection-v1",
            quality_profile_id="workspace-pdf-quality-v1",
        )
    else:
        from ao_lore.docx_ooxml import DocxLimits, docx_configuration_digest
        from ao_lore.private_docx_domain import DOCX_MIME

        manifest = docx_benchmark(decision="hold")
        parser = FakeParser(
            calls,
            decision=decision,
            media_type=DOCX_MIME,
            parser_id="native-docx-ooxml",
            parser_version="1.0.0",
            parser_configuration_digest=docx_configuration_digest(DocxLimits()),
            benchmark_result_digest=manifest["result_digest"],
            selection_profile_id="workspace-docx-selection-v1",
            quality_profile_id="workspace-docx-quality-v1",
        )
    dependencies = IngestionDependencies(
        parser=parser,
        distiller=FakeDistiller(calls),
        now=lambda: "2026-08-14T12:00:00Z",
        benchmark_manifest=manifest,
        selection_profile={"profile_id": f"workspace-{format_id}-selection-v1"},
        quality_profile={"profile_id": f"workspace-{format_id}-quality-v1"},
    )
    return dependencies, calls


class ParseVerifiedDocumentTests(unittest.TestCase):
    def test_parse_verified_document_returns_accepted_ir_without_distillation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manual.pdf"
            data = b"%PDF-1.7\nsynthetic workspace evidence"
            source.write_bytes(data)
            dependencies, calls = parser_dependencies(data)

            result = parse_verified_document(
                source, format_id="pdf", dependencies=dependencies,
            )

        self.assertEqual("accept", result["decision"])
        self.assertEqual("sha256:" + hashlib.sha256(data).hexdigest(), result["document_ir"]["document_id"])
        self.assertEqual(["parse"], [call[0] for call in calls])

    def test_parse_verified_document_rejects_quality_failure_and_unsupported_format(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manual.pdf"
            data = b"%PDF-1.7\nsynthetic workspace evidence"
            source.write_bytes(data)
            rejected, _calls = parser_dependencies(data, decision="reject")
            with self.assertRaises(IngestionError):
                parse_verified_document(source, format_id="pdf", dependencies=rejected)
            with self.assertRaises(IngestionError):
                parse_verified_document(source, format_id="txt", dependencies=rejected)


class WorkspaceDocumentIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dependencies = WorkspaceRegistryDependencies(self.root)
        self.publish_registry()
        self.inbox = self.root / "workspaces/state/workspace-a/inbox"
        self.inbox.mkdir(parents=True)
        self.pdf_data = b"%PDF-1.7\nsynthetic workspace evidence"
        (self.inbox / "manual.pdf").write_bytes(self.pdf_data)

    def tearDown(self):
        self.temp.cleanup()

    def publish_registry(self, *, status: str = "active", version: int = 1,
                         document_generation_digest: str | None = None):
        return publish_workspace_registry_generation(
            (workspace_definition(
                status=status,
                version=version,
                document_generation_digest=document_generation_digest,
            ),), self.dependencies,
        )

    def ingest(self, *, locator="inbox/manual.pdf", format_id="pdf", role="operator_procedure", sensitivity="internal", parser_decision="accept"):
        data = self.pdf_data if format_id == "pdf" else DOCX_FIXTURE.read_bytes()
        runtime, calls = parser_dependencies(data, format_id=format_id, decision=parser_decision)
        with patch("ao_lore.ingestion._workspace_parser_dependencies", return_value=runtime):
            report = ingest_workspace_document(
                self.dependencies, "workspace-a", locator, format_id, role, sensitivity,
            )
        return report, calls

    def protected_inventory(self):
        result = {}
        for name in ("candidates", "reviews", "brain", "graph"):
            path = self.root / name
            result[name] = tuple(sorted(
                (item.relative_to(path).as_posix(), item.stat().st_size)
                for item in path.rglob("*") if item.is_file()
            )) if path.exists() else ()
        return result

    def test_workspace_ingest_publishes_ir_without_graph_candidate_review_or_brain(self):
        before = self.protected_inventory()
        report, calls = self.ingest()
        generation = load_workspace_documents(
            WorkspaceDocumentDependencies(self.root),
            "workspace-a",
        ).generation

        self.assertEqual("published", report["status"])
        self.assertEqual(report["document_id"], generation["documents"][0]["document_id"])
        self.assertEqual("inbox/manual.pdf", generation["documents"][0]["document_ir"]["source"]["resource"])
        self.assertEqual("operator_procedure", generation["documents"][0]["authority_role"])
        self.assertEqual("current", generation["documents"][0]["freshness_status"])
        self.assertEqual(before, self.protected_inventory())
        self.assertEqual(["parse"], [call[0] for call in calls])
        self.assertEqual(
            report,
            validate_workspace_document_ingest_readback(
                report, generation=generation, workspace_id="workspace-a",
            ),
        )

    def test_workspace_ingest_binds_exact_source_ir_and_publication_digests(self):
        report, _calls = self.ingest(sensitivity="restricted")
        generation = load_workspace_documents(
            WorkspaceDocumentDependencies(self.root),
            "workspace-a",
        ).generation
        document = generation["documents"][0]
        self.assertEqual("sha256:" + hashlib.sha256(self.pdf_data).hexdigest(), report["source_digest"])
        self.assertEqual(canonical_digest(document["document_ir"]), report["document_ir_digest"])
        self.assertEqual(generation["generation_digest"], report["generation_digest"])
        self.assertEqual("restricted", document["sensitivity"])
        self.assertTrue(all(report[field] is False for field in AUTHORITY_FIELDS))

    def test_duplicate_publication_is_an_exact_unchanged_retry(self):
        first, _calls = self.ingest()
        second, _calls = self.ingest()
        self.assertEqual("published", first["status"])
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(first["generation_digest"], second["generation_digest"])

    def test_competing_identical_ingests_report_one_published_and_one_unchanged(self):
        barrier = threading.Barrier(2)
        original_load = workspace_documents_module.load_workspace_documents
        before_calls = 0
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def synchronized_before_snapshot(*args, **kwargs):
            nonlocal before_calls
            snapshot = original_load(*args, **kwargs)
            before_calls += 1
            if before_calls <= 2:
                barrier.wait(timeout=5)
            return snapshot

        def runtime_factory(*_args, **_kwargs):
            runtime, _calls = parser_dependencies(self.pdf_data)
            return runtime

        def run():
            try:
                results.append(
                    ingest_workspace_document(
                        self.dependencies,
                        "workspace-a",
                        "inbox/manual.pdf",
                        "pdf",
                        "operator_procedure",
                        "internal",
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        with patch(
            "ao_lore.ingestion._workspace_parser_dependencies",
            side_effect=runtime_factory,
        ), patch(
            "ao_lore.workspace_documents.load_workspace_documents",
            side_effect=synchronized_before_snapshot,
        ):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertFalse(failures)
        self.assertEqual(["published", "unchanged"], sorted(item["status"] for item in results))
        self.assertEqual(results[0]["generation_digest"], results[1]["generation_digest"])
        generations = self.root / "workspaces/state/workspace-a/documents/generations"
        self.assertEqual(1, len(list(generations.iterdir())))

    def test_only_selected_workspace_contained_inbox_sources_are_accepted(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(self.pdf_data)
        for locator in ("../manual.pdf", "/tmp/manual.pdf", "sources/manual.pdf", "inbox/../manual.pdf", "inbox/nested/manual.pdf"):
            with self.subTest(locator=locator), self.assertRaises(WorkspaceRegistryError):
                self.ingest(locator=locator)
        link = self.inbox / "linked.pdf"
        link.symlink_to(outside)
        with self.assertRaises(WorkspaceRegistryError):
            self.ingest(locator="inbox/linked.pdf")

    def test_inactive_workspace_and_closed_classifications_fail_before_parse(self):
        self.publish_registry(status="inactive", version=2)
        for kwargs in (
            {},
            {"role": "administrator"},
            {"sensitivity": "secret"},
            {"format_id": "txt"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((WorkspaceRegistryError, IngestionError)):
                self.ingest(**kwargs)

    def test_all_closed_roles_and_sensitivities_are_accepted(self):
        for index, role in enumerate(AUTHORITY_ROLES):
            locator = f"inbox/manual-{index}.pdf"
            (self.inbox / f"manual-{index}.pdf").write_bytes(self.pdf_data + str(index).encode())
            report, _calls = self.ingest(locator=locator, role=role, sensitivity=("public", "internal", "restricted")[index % 3])
            self.assertEqual("published", report["status"])

    def test_qualified_docx_adapter_publishes_without_graph(self):
        data = DOCX_FIXTURE.read_bytes()
        (self.inbox / "manual.docx").write_bytes(data)
        report, calls = self.ingest(locator="inbox/manual.docx", format_id="docx")
        self.assertEqual("published", report["status"])
        self.assertEqual(["parse"], [call[0] for call in calls])

    def test_source_and_workspace_replacement_during_operation_fail_closed(self):
        source = self.inbox / "manual.pdf"

        def replace_source(name):
            if name == "before_workspace_ingest_source_revalidation":
                source.rename(source.with_suffix(".old"))
                source.write_bytes(self.pdf_data)

        replacing = WorkspaceRegistryDependencies(self.root, replace_source)
        runtime, _calls = parser_dependencies(self.pdf_data)
        with patch("ao_lore.ingestion._workspace_parser_dependencies", return_value=runtime):
            with self.assertRaises(WorkspaceRegistryError):
                ingest_workspace_document(replacing, "workspace-a", "inbox/manual.pdf", "pdf", "operator_procedure", "internal")

        source.unlink()
        source.with_suffix(".old").rename(source)
        workspace = self.root / "workspaces/state/workspace-a"
        displaced = self.root / "workspaces/state/workspace-a-old"

        def replace_workspace(name):
            if name == "before_workspace_ingest_publication":
                workspace.rename(displaced)
                (workspace / "inbox").mkdir(parents=True)
                (workspace / "inbox/manual.pdf").write_bytes(self.pdf_data)

        replacing = WorkspaceRegistryDependencies(self.root, replace_workspace)
        runtime, _calls = parser_dependencies(self.pdf_data)
        with patch("ao_lore.ingestion._workspace_parser_dependencies", return_value=runtime):
            with self.assertRaises(WorkspaceRegistryError):
                ingest_workspace_document(replacing, "workspace-a", "inbox/manual.pdf", "pdf", "operator_procedure", "internal")

    def test_registry_advance_between_revalidation_and_publication_rejects_before_document_write(self):
        runtime, _calls = parser_dependencies(self.pdf_data)
        original_publish = workspace_documents_module.publish_workspace_documents

        def advance_registry_then_publish(*args, **kwargs):
            self.publish_registry(version=2)
            return original_publish(*args, **kwargs)

        with patch(
            "ao_lore.ingestion._workspace_parser_dependencies",
            return_value=runtime,
        ), patch(
            "ao_lore.workspace_documents.publish_workspace_documents",
            side_effect=advance_registry_then_publish,
        ):
            with self.assertRaises((IngestionError, WorkspaceRegistryError)):
                ingest_workspace_document(
                    self.dependencies,
                    "workspace-a",
                    "inbox/manual.pdf",
                    "pdf",
                    "operator_procedure",
                    "internal",
                )

        self.assertFalse((self.root / "workspaces/state/workspace-a/documents").exists())

    def test_parser_quality_rejection_does_not_publish(self):
        with self.assertRaises(IngestionError):
            self.ingest(parser_decision="reject")
        self.assertFalse((self.root / "workspaces/state/workspace-a/documents").exists())


if __name__ == "__main__":
    unittest.main()
