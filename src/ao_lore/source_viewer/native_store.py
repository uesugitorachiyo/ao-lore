"""Native AO Lore retained-source adapter for the local source viewer."""
from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_query import (
    DocumentEvidenceQueryError,
    resolve_workspace_document_evidence,
)
from ao_lore.workspace_documents import WorkspaceDocumentDependencies, load_workspace_documents
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    load_workspace_registry,
    select_workspace,
)
from ao_lore.workspace_runtime import read_workspace_inbox_source

from .contracts import (
    LEVELS, MAX_MANIFEST, MAX_SOURCE, ViewerError, canonical_json, digest, evidence_key,
    format_time, reject, require, strict_json, timestamp, valid_digest, valid_id,
    validate_evidence, validate_grant,
)
from .store import Clock, SafeRoot, utcnow


def _validate_native_manifest(value: Any) -> None:
    required = {"schema_version", "primary_workspace_id", "reference_workspace_ids",
                "registry_digest", "provenance_mode", "records"}
    require(type(value) is dict and set(value) == required)
    require(value["schema_version"] == "ao.lore.source-viewer-native-bindings.v0.1")
    require(value["provenance_mode"] == "native-retained-source")
    require(valid_id(value["primary_workspace_id"]) and valid_digest(value["registry_digest"]))
    references = value["reference_workspace_ids"]
    require(type(references) is list and len(references) <= 32
            and all(valid_id(item) for item in references)
            and len(set(references)) == len(references)
            and value["primary_workspace_id"] not in references)
    records = value["records"]
    require(type(records) is list and 1 <= len(records) <= 128)
    keys = set()
    for record in records:
        require(type(record) is dict and set(record) == {
            "evidence", "format", "original_sensitivity", "pdf_page_basis"})
        validate_evidence(record["evidence"])
        require(record["evidence"]["workspace_id"] in {value["primary_workspace_id"], *references},
                "workspace_denied")
        require(record["format"] in {"pdf", "docx"})
        require(record["original_sensitivity"] in {"public", "internal"}, "sensitivity_denied")
        require(LEVELS[record["evidence"]["sensitivity"]] <= LEVELS[record["original_sensitivity"]],
                "sensitivity_denied")
        require(record["pdf_page_basis"] == (
            "physical-one-based" if record["format"] == "pdf" else "not-applicable"))
        key = evidence_key(record["evidence"])
        require(key not in keys, "identity_collision")
        keys.add(key)


class NativeSourceStore:
    """Revalidate registry, IR, block, approval, and original bytes on every read."""

    provenance_mode = "native-retained-source"
    native_binding_revalidated = True

    def __init__(self, home: Path, grant_id: str, *, clock: Clock = utcnow):
        require(valid_id(grant_id))
        self.home = Path(os.path.abspath(home))
        self.root = SafeRoot(self.home)
        self.clock = clock
        self.grant_id = grant_id
        raw = self.root.read(("source-viewer", "native-approvals", grant_id + ".json"), 8192)
        self.grant = strict_json(raw, 8192)
        validate_grant(self.grant)
        require(self.grant["grant_id"] == grant_id)
        self._grant_digest = digest(raw)
        self._binding_hex = self.grant["binding_digest"][7:]
        self.manifest = self._load_manifest()
        self.records = {evidence_key(item["evidence"]): item for item in self.manifest["records"]}
        self.authorize()

    def _load_manifest(self) -> dict[str, Any]:
        raw = self.root.read(("source-viewer", "native-bindings", self._binding_hex + ".json"), MAX_MANIFEST)
        require(digest(raw) == self.grant["binding_digest"], "binding_drift")
        value = strict_json(raw)
        _validate_native_manifest(value)
        require(value["primary_workspace_id"] == self.grant["primary_workspace_id"], "workspace_denied")
        return value

    def _selection(self):
        try:
            selection = select_workspace(
                load_workspace_registry(WorkspaceRegistryDependencies(self.home)),
                self.manifest["primary_workspace_id"],
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ViewerError("native_binding_invalid") from exc
        require(selection.generation["registry_digest"] == self.manifest["registry_digest"],
                "approval_revoked")
        require([item["workspace_id"] for item in selection.references]
                == self.manifest["reference_workspace_ids"], "approval_revoked")
        return selection

    def authorize(self) -> None:
        raw = self.root.read(("source-viewer", "native-approvals", self.grant_id + ".json"), 8192)
        require(digest(raw) == self._grant_digest, "approval_revoked")
        now = self.clock()
        require(timestamp(self.grant["created_at"]) <= now < timestamp(self.grant["expires_at"]),
                "approval_expired")
        self._load_manifest()
        self._selection()

    def _verified(self, stored: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        self.authorize()
        evidence = stored["evidence"]
        try:
            snapshot = load_workspace_documents(
                WorkspaceDocumentDependencies(self.home), evidence["workspace_id"])
            generation = snapshot.generation
            require(type(generation) is dict
                    and generation["generation_digest"] == evidence["generation_digest"],
                    "native_binding_invalid")
            current = resolve_workspace_document_evidence(
                generation, evidence["workspace_id"], evidence["evidence_id"])
        except (DocumentEvidenceQueryError, OSError, TypeError, ValueError) as exc:
            raise ViewerError("native_binding_invalid") from exc
        require(current == evidence, "native_binding_invalid")
        documents = [item for item in generation["documents"]
                     if item["document_id"] == evidence["document_id"]]
        require(len(documents) == 1, "native_binding_invalid")
        document = documents[0]
        same_source = [item for item in generation["documents"]
                       if item["source_digest"] == evidence["source_digest"]]
        whole_level = max(LEVELS[item["sensitivity"]] for item in same_source)
        require(LEVELS[stored["original_sensitivity"]] == whole_level, "sensitivity_denied")
        resource = document["document_ir"]["source"]["resource"]
        suffix = ".pdf" if stored["format"] == "pdf" else ".docx"
        try:
            source = read_workspace_inbox_source(
                WorkspaceRegistryDependencies(self.home), evidence["workspace_id"],
                resource, expected_suffix=suffix,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ViewerError("source_integrity_failed") from exc
        source_digest = "sha256:" + hashlib.sha256(source.data).hexdigest()
        require(len(source.data) <= MAX_SOURCE, "resource_limit")
        require(source_digest == evidence["source_digest"], "source_integrity_failed")
        expected_record = canonical_digest({
            "workspace_id": evidence["workspace_id"], "source_locator": resource,
            "source_digest": source_digest, "media_type": document["media_type"],
            "authority_role": document["authority_role"], "sensitivity": document["sensitivity"],
        })
        require(document["source_record_digest"] == expected_record
                and document["source_id"] == "source-" + source_digest[7:31],
                "native_binding_invalid")
        self._selection()
        return copy.deepcopy(stored), source.data

    def list_records(self) -> list[dict[str, Any]]:
        return [self._verified(item)[0] for item in self.manifest["records"]]

    def get_record(self, key: tuple[str, str, str]) -> dict[str, Any]:
        record = self.records.get(key)
        if record is None:
            reject("evidence_unavailable", 404)
        return self._verified(record)[0]

    def source_bytes(self, record: dict[str, Any]) -> bytes:
        retained = self.records.get(evidence_key(record["evidence"])) if type(record) is dict else None
        require(retained == record, "native_binding_invalid")
        return self._verified(retained)[1]


def provision_native_approval(
    home: Path, manifest: dict[str, Any], *, grant_id: str,
    max_sensitivity: str = "public", allow_original_download: bool = False,
    lifetime_minutes: int = 30, clock: Clock = utcnow,
) -> dict[str, Any]:
    """Trusted local whole-original approval; never exposed through HTTP."""
    _validate_native_manifest(manifest)
    require(valid_id(grant_id) and type(lifetime_minutes) is int and 1 <= lifetime_minutes <= 60)
    require(max_sensitivity in {"public", "internal"})
    for record in manifest["records"]:
        require(LEVELS[record["original_sensitivity"]] <= LEVELS[max_sensitivity], "sensitivity_denied")
    body = canonical_json(manifest)
    now = clock()
    grant = {
        "schema_version": "ao.lore.source-viewer-grant.v0.1", "grant_id": grant_id,
        "primary_workspace_id": manifest["primary_workspace_id"], "binding_digest": digest(body),
        "created_at": format_time(now), "expires_at": format_time(now + timedelta(minutes=lifetime_minutes)),
        "max_sensitivity": max_sensitivity, "authorized_scope": "whole-source",
        "allow_original_download": allow_original_download,
    }
    validate_grant(grant)
    root = Path(os.path.abspath(home))
    for relative in ("source-viewer", "source-viewer/native-bindings", "source-viewer/native-approvals"):
        path = root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not path.is_symlink() and path.stat().st_uid == os.geteuid()
                and path.stat().st_mode & 0o022 == 0, "retained_state_unavailable")
    binding = root / "source-viewer/native-bindings" / (digest(body)[7:] + ".json")
    approval = root / "source-viewer/native-approvals" / (grant_id + ".json")
    try:
        if binding.exists():
            require(not binding.is_symlink() and binding.is_file()
                    and binding.read_bytes() == body, "binding_drift")
        else:
            with binding.open("xb") as stream:
                stream.write(body)
        with approval.open("xb") as stream:
            stream.write(canonical_json(grant))
    except FileExistsError:
        reject("already_exists")
    os.chmod(binding, 0o600)
    os.chmod(approval, 0o600)
    return copy.deepcopy(grant)
