import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_query import resolve_workspace_document_evidence
from ao_lore.source_viewer.contracts import ViewerError
from ao_lore.source_viewer.native_store import NativeSourceStore, provision_native_approval
from ao_lore.source_viewer.server import ViewerState, fingerprint, make_server
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from ao_lore.workspace_documents import WorkspaceDocumentDependencies, publish_workspace_documents
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies, load_workspace_registry,
    publish_workspace_registry_generation,
)


def authority():
    return {field: False for field in AUTHORITY_FIELDS}


def definition(generation=None, *, version=1, references=()):
    value = {
        "schema_version": "ao.lore.workspace-definition.v0.2", "workspace_id": "workspace-a",
        "workspace_version": version, "workspace_type": "property", "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction", "lifecycle_status": "active",
        "root_workflow_id": "workflow-workspace-a", "root_workflow_digest": canonical_digest("workflow"),
        "source_registry_id": "sources-workspace-a", "source_registry_digest": canonical_digest("sources"),
        "graph_id": None, "graph_digest": None, "freshness_policy_id": None,
        "freshness_policy_digest": None, "freshness_summary_status": "not_observed",
        "freshness_summary_id": None, "freshness_summary_digest": None,
        "reference_workspace_ids": list(references), "document_store_id": "documents-workspace-a",
        "document_generation_digest": generation, "definition_digest": "sha256:" + "0" * 64,
        **authority(),
    }
    value["definition_digest"] = canonical_digest({k: v for k, v in value.items() if k != "definition_digest"})
    return value


class NativeSourceViewerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = WorkspaceRegistryDependencies(self.root)
        publish_workspace_registry_generation((definition(),), self.registry)
        inbox = self.root / "workspaces/state/workspace-a/inbox"
        inbox.mkdir(parents=True)
        self.original = b"%PDF-1.7\nNative viewer evidence\n%%EOF\n"
        (inbox / "manual.pdf").write_bytes(self.original)
        source_digest = "sha256:" + hashlib.sha256(self.original).hexdigest()
        ir = {
            "schema_version": "ao.lore.document-ir.v0.1", "document_id": source_digest,
            "source": {"resource": "inbox/manual.pdf", "digest": source_digest, "media_type": "application/pdf"},
            "blocks": [{"id": "b1", "type": "paragraph", "text": "Native viewer evidence",
                        "source_span": {"page": 1, "start": 9, "end": 31}}],
            "metadata": {"source_id": "source-" + source_digest[7:31],
                         "source_record_digest": canonical_digest({
                             "workspace_id": "workspace-a", "source_locator": "inbox/manual.pdf",
                             "source_digest": source_digest, "media_type": "application/pdf",
                             "authority_role": "operator_procedure", "sensitivity": "internal"}),
                         "authority_role": "operator_procedure", "sensitivity": "internal",
                         "version": "source-" + source_digest[7:31], "effective_date": None,
                         "freshness_status": "current", "qualification_codes": ["qualified-parser"]},
        }
        initial = load_workspace_registry(self.registry).generations[-1]
        publication = publish_workspace_documents(
            WorkspaceDocumentDependencies(self.root), "workspace-a", (ir,),
            expected_registry_digest=initial["registry_digest"],
            expected_definition_digest=definition()["definition_digest"],
        )
        self.generation = {k: v for k, v in publication.items() if k != "status"}
        self.evidence = resolve_workspace_document_evidence(
            self.generation, "workspace-a",
            resolve_workspace_document_evidence(self.generation, "workspace-a", block_id="b1")["evidence_id"],
        )
        manifest = {
            "schema_version": "ao.lore.source-viewer-native-bindings.v0.1",
            "primary_workspace_id": "workspace-a", "reference_workspace_ids": [],
            "registry_digest": load_workspace_registry(self.registry).generations[-1]["registry_digest"],
            "provenance_mode": "native-retained-source",
            "records": [{"evidence": self.evidence, "format": "pdf",
                         "original_sensitivity": "internal", "pdf_page_basis": "physical-one-based"}],
        }
        provision_native_approval(self.root, manifest, grant_id="grant-native", max_sensitivity="internal",
                                  clock=lambda: datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
        self.store = NativeSourceStore(self.root, "grant-native",
                                       clock=lambda: datetime(2026, 9, 14, 12, 1, tzinfo=timezone.utc))

    def tearDown(self):
        self.temporary.cleanup()

    def test_native_store_revalidates_and_opens_retained_original(self):
        record = self.store.get_record(("workspace-a", self.generation["generation_digest"], self.evidence["evidence_id"]))
        self.assertEqual(self.original, self.store.source_bytes(record))
        self.assertTrue(self.store.native_binding_revalidated)
        self.assertEqual("native-retained-source", self.store.provenance_mode)

    def test_native_store_fails_closed_after_source_drift(self):
        (self.root / "workspaces/state/workspace-a/inbox/manual.pdf").write_bytes(b"changed")
        with self.assertRaises(ViewerError):
            self.store.source_bytes(self.store.list_records()[0])

    def test_native_resolve_uses_versioned_truthful_readback(self):
        renderer = lambda _data, page, _excerpt: {
            "png": b"png", "page": page, "page_count": 1, "width": 10, "height": 10,
            "highlight": {"status": "exact_match_unavailable", "boxes": []},
        }
        state = ViewerState(self.store, renderer=renderer)
        token = state.login(state.launch_code)
        result = state.resolve(fingerprint(token), {
            "workspace_id": "workspace-a", "generation_digest": self.generation["generation_digest"],
            "evidence_id": self.evidence["evidence_id"],
        })
        self.assertEqual("ao.lore.source-viewer-resolve.v0.2", result["schema_version"])
        self.assertEqual("native-retained-source", result["provenance_mode"])
        self.assertTrue(result["native_binding_revalidated"])

    def test_native_store_rejects_registry_advance_after_approval(self):
        current = load_workspace_registry(self.registry).workspaces[0]
        changed = dict(current)
        changed["workspace_version"] += 1
        changed["definition_digest"] = canonical_digest({
            key: value for key, value in changed.items() if key != "definition_digest"
        })
        publish_workspace_registry_generation((changed,), self.registry)
        with self.assertRaises(ViewerError):
            self.store.list_records()

    def test_native_item_resolves_through_real_loopback_http(self):
        renderer = lambda _data, page, _excerpt: {
            "png": b"png", "page": page, "page_count": 1, "width": 10, "height": 10,
            "highlight": {"status": "exact_match_unavailable", "boxes": []},
        }
        server = make_server(self.store, renderer=renderer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, *, body=None, token=None):
                headers = {"Origin": server.origin}
                if token:
                    headers["Authorization"] = "Bearer " + token
                data = None if body is None else json.dumps(body).encode()
                if data is not None:
                    headers["Content-Type"] = "application/json"
                with urllib.request.urlopen(urllib.request.Request(
                    server.origin + path, data=data, headers=headers,
                ), timeout=5) as response:
                    return json.loads(response.read())
            token = request("/api/session", body={"launch_code": server.state.launch_code})["session_token"]
            result = request("/api/resolve", token=token, body={
                "workspace_id": "workspace-a", "generation_digest": self.generation["generation_digest"],
                "evidence_id": self.evidence["evidence_id"],
            })
            self.assertEqual("native-retained-source", result["provenance_mode"])
            self.assertTrue(result["native_binding_revalidated"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
