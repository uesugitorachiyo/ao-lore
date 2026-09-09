#!/usr/bin/env python3
"""Generate one deterministic synthetic sanitized-lifecycle fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = HERE / "fixture-spec.json"
OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
OWNER_BYTES = b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
REJECTION = "sanitized lifecycle fixture rejected\n"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
MAX_SPEC_BYTES = 32 * 1024
MAX_FILE_BYTES = 64 * 1024


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("argument contract")


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)[:-1]).hexdigest()


def _byte_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _evidence_digest(value: object, domain: str) -> str:
    return _canonical_digest({
        "domain": f"ao.lore.evidence-graph.{domain}.v0.1",
        "value": value,
    })


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _load_spec() -> dict[str, object]:
    body = SPEC.read_bytes()
    if not 0 < len(body) <= MAX_SPEC_BYTES:
        raise ValueError("spec size")
    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
    )
    if type(value) is not dict or body != _canonical_bytes(value):
        raise ValueError("spec encoding")
    expected_keys = {
        "schema_version", "documents", "relationships",
        "selected_document_ids", "unrelated_document_id",
    }
    if set(value) != expected_keys or value["schema_version"] != "ao.lore.sanitized-lifecycle-fixture-spec.v0.1":
        raise ValueError("spec contract")
    documents = value["documents"]
    if type(documents) is not list or len(documents) != 3:
        raise ValueError("document count")
    document_keys = {
        "authority_role", "document_id", "effective_date", "sensitivity",
        "source_id", "subject_terms", "text", "version",
    }
    ids: list[str] = []
    texts: list[str] = []
    for item in documents:
        if type(item) is not dict or set(item) != document_keys:
            raise ValueError("document contract")
        if any(type(item[key]) is not str for key in document_keys - {"subject_terms"}):
            raise ValueError("document value")
        document_id = item["document_id"]
        text = item["text"]
        if not document_id or "/" in document_id or ".." in document_id:
            raise ValueError("document identity")
        if not 20 <= len(text) <= 160 or not text.isascii() or any(ord(char) < 32 for char in text):
            raise ValueError("document text")
        terms = item["subject_terms"]
        if (type(terms) is not list or not 1 <= len(terms) <= 8
                or any(type(term) is not str or not 1 <= len(term) <= 64
                       or term.casefold() not in text.casefold() for term in terms)):
            raise ValueError("subject terms")
        ids.append(document_id)
        texts.append(text)
    if ids != sorted(set(ids)) or len(set(texts)) != 3:
        raise ValueError("document ordering")
    if any(left in right for left in texts for right in texts if left != right):
        raise ValueError("document overlap")
    selected = value["selected_document_ids"]
    unrelated = value["unrelated_document_id"]
    if (type(selected) is not list or len(selected) != 2 or selected != sorted(set(selected))
            or any(type(item) is not str or item not in ids for item in selected)
            or type(unrelated) is not str or unrelated not in ids or unrelated in selected):
        raise ValueError("selection contract")
    relationships = value["relationships"]
    relationship_keys = {"relationship_id", "relationship_type", "source_document_id", "target_id"}
    if type(relationships) is not list or len(relationships) != 2:
        raise ValueError("relationship count")
    relationship_ids = []
    for item in relationships:
        if (type(item) is not dict or set(item) != relationship_keys
                or any(type(item[key]) is not str or not item[key] for key in relationship_keys)
                or item["source_document_id"] not in selected):
            raise ValueError("relationship contract")
        relationship_ids.append(item["relationship_id"])
    if relationship_ids != sorted(set(relationship_ids)):
        raise ValueError("relationship ordering")
    return value


def _documents() -> dict[str, bytes]:
    spec = _load_spec()
    files: dict[str, bytes] = {}
    bindings: list[dict[str, object]] = []
    identities: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    identity_by_document: dict[str, dict[str, object]] = {}
    claim_by_document: dict[str, dict[str, object]] = {}
    parser_digest = _canonical_digest({"parser_id": "synthetic-text", "parser_version": "1"})
    for item in spec["documents"]:
        document_id = item["document_id"]
        source_name = f"sources/{document_id}.txt"
        ir_name = f"document-ir/{document_id}.json"
        source_body = item["text"].encode("ascii")
        source_digest = _byte_digest(source_body)
        block = {
            "id": f"{document_id}-block-1",
            "source_span": {"end": len(source_body), "start": 0},
            "text": item["text"],
            "type": "paragraph",
        }
        document_ir = {
            "blocks": [block],
            "document_id": document_id,
            "metadata": {"public_safe": True, "synthetic": True},
            "parser": {
                "configuration_digest": parser_digest,
                "parser_id": "synthetic-text",
                "parser_version": "1",
            },
            "schema_version": "ao.lore.document-ir.v0.1",
            "source": {
                "digest": source_digest,
                "media_type": "text/plain",
                "resource": source_name,
            },
        }
        ir_body = _canonical_bytes(document_ir)
        ir_digest = _canonical_digest(document_ir)
        block_digest = _canonical_digest(block)
        evidence_id = _canonical_digest({
            "block_digest": block_digest,
            "block_id": block["id"],
            "document_id": document_id,
            "source_digest": source_digest,
        })
        excerpt_digest = _evidence_digest(item["text"], "excerpt")
        claim_core = {
            "citation_anchor": block["id"],
            "excerpt_digest": excerpt_digest,
            "source_digest": source_digest,
            "source_id": item["source_id"],
        }
        claim_id = "claim-" + _evidence_digest(claim_core, "claim").split(":", 1)[1][:24]
        claim = {
            "authority_role": item["authority_role"],
            "citation_anchor": block["id"],
            "claim_id": claim_id,
            "document_id": document_id,
            "excerpt": item["text"],
            "excerpt_digest": excerpt_digest,
            "operational_question_ids": ["question-sanitized-lifecycle"],
            "semantic_reason_codes": ["topic_match", "citation_supported"],
            "source_digest": source_digest,
            "source_id": item["source_id"],
            "subject_terms": item["subject_terms"],
        }
        citation = {
            "block_digest": block_digest,
            "citation_digest": "sha256:" + "0" * 64,
            "document_id": document_id,
            "evidence_id": evidence_id,
            "render_text": item["text"],
            "source_span": block["source_span"],
        }
        citation["citation_digest"] = _canonical_digest({
            key: value for key, value in citation.items() if key != "citation_digest"
        })
        identity = {
            "authority_role": item["authority_role"],
            "block_digest": block_digest,
            "block_id": block["id"],
            "citation": citation,
            "claim_id": claim_id,
            "document_id": document_id,
            "document_ir_digest": ir_digest,
            "effective_date": item["effective_date"],
            "evidence_id": evidence_id,
            "render_text": item["text"],
            "sensitivity": item["sensitivity"],
            "source_digest": source_digest,
            "source_id": item["source_id"],
            "source_span": block["source_span"],
        }
        files[source_name] = source_body
        files[ir_name] = ir_body
        bindings.append({
            "document_id": document_id,
            "document_ir_digest": ir_digest,
            "document_ir_file": ir_name,
            "source_digest": source_digest,
            "source_file": source_name,
        })
        identities.append(identity)
        claims.append(claim)
        identity_by_document[document_id] = identity
        claim_by_document[document_id] = claim
    relationships = []
    for item in spec["relationships"]:
        supporting = identity_by_document[item["source_document_id"]]
        claim = claim_by_document[item["source_document_id"]]
        relationship = {
            "citation_digest": supporting["citation"]["citation_digest"],
            "relationship_digest": "sha256:" + "0" * 64,
            "relationship_id": item["relationship_id"],
            "relationship_type": item["relationship_type"],
            "source_claim_id": claim["claim_id"],
            "source_document_id": item["source_document_id"],
            "supporting_evidence_id": supporting["evidence_id"],
            "supporting_excerpt": supporting["render_text"],
            "supporting_excerpt_digest": claim["excerpt_digest"],
            "target_id": item["target_id"],
        }
        relationship["relationship_digest"] = _canonical_digest({
            key: value for key, value in relationship.items() if key != "relationship_digest"
        })
        relationships.append(relationship)
    bundle = {
        "claims": claims,
        "documents": bindings,
        "evidence_identities": identities,
        "network_used": False,
        "provider_used": False,
        "relationships": relationships,
        "schema_version": "ao.lore.sanitized-lifecycle-fixture.v0.1",
        "selected_document_ids": spec["selected_document_ids"],
        "synthetic_public_safe": True,
        "unrelated_document_id": spec["unrelated_document_id"],
    }
    files["fixture-bundle.json"] = _canonical_bytes(bundle)
    if len(files) != 7 or any(not 0 < len(body) <= MAX_FILE_BYTES for body in files.values()):
        raise ValueError("fixture budget")
    return files


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode),
    )


def _open_absolute_directory(path: Path) -> tuple[int, os.stat_result]:
    if not path.is_absolute():
        raise ValueError("absolute path")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("path component")
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            if not _same(before, os.fstat(child)):
                os.close(child)
                raise ValueError("directory replacement")
            os.close(descriptor)
            descriptor = child
        return descriptor, os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular(parent: int, name: str, maximum: int) -> tuple[bytes, os.stat_result]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ValueError("file contract")
    descriptor = os.open(name, FILE_FLAGS, dir_fd=parent)
    try:
        held = os.fstat(descriptor)
        if not _same(before, held) or held.st_nlink != 1:
            raise ValueError("file replacement")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(8192, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise ValueError("file size")
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same(held, rebound) or rebound.st_nlink != 1:
            raise ValueError("file rebound")
        return b"".join(chunks), held
    finally:
        os.close(descriptor)


def _open_owned_parent(out: Path) -> tuple[int, os.stat_result, str]:
    supplied = os.fspath(out)
    if not supplied or ".." in Path(supplied).parts:
        raise ValueError("escaping path")
    absolute = Path(os.path.abspath(supplied))
    if absolute.name in {"", ".", ".."}:
        raise ValueError("root name")
    parent, held = _open_absolute_directory(absolute.parent)
    try:
        body, _ = _read_regular(parent, OWNER_NAME, len(OWNER_BYTES))
        if body != OWNER_BYTES:
            raise ValueError("owner marker")
        return parent, held, absolute.name
    except BaseException:
        os.close(parent)
        raise


def _verify_directory_rebound(parent: int, name: str, held: os.stat_result) -> None:
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(rebound.st_mode) or not _same(held, rebound):
        raise ValueError("directory rebound")


def _write_regular(parent: int, name: str, body: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent,
    )
    try:
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
            raise ValueError("created file")
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(rebound.st_mode) or rebound.st_nlink != 1:
        raise ValueError("created file rebound")


def _open_child(parent: int, name: str) -> tuple[int, os.stat_result]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("child directory")
    descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    held = os.fstat(descriptor)
    if not _same(before, held):
        os.close(descriptor)
        raise ValueError("child replacement")
    return descriptor, held


def _write_fixture(parent: int, parent_held: os.stat_result, root_name: str,
                   expected: dict[str, bytes]) -> None:
    try:
        os.stat(root_name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("pre-populated root")
    os.mkdir(root_name, mode=0o700, dir_fd=parent)
    root, root_held = _open_child(parent, root_name)
    try:
        for directory in ("document-ir", "sources"):
            os.mkdir(directory, mode=0o700, dir_fd=root)
        directory_fds: dict[str, tuple[int, os.stat_result]] = {
            "": (root, root_held),
            "document-ir": _open_child(root, "document-ir"),
            "sources": _open_child(root, "sources"),
        }
        try:
            for relative, body in sorted(expected.items()):
                directory, _, name = relative.rpartition("/")
                _write_regular(directory_fds[directory][0], name, body)
            for name, (descriptor, held) in directory_fds.items():
                if name:
                    _verify_directory_rebound(root, name, held)
                os.fsync(descriptor)
        finally:
            for name, (descriptor, _held) in directory_fds.items():
                if name:
                    os.close(descriptor)
        _verify_directory_rebound(parent, root_name, root_held)
        if not _same(parent_held, os.fstat(parent)):
            raise ValueError("parent replacement")
        os.fsync(root)
        os.fsync(parent)
    finally:
        os.close(root)


def _check_fixture(parent: int, parent_held: os.stat_result, root_name: str,
                   expected: dict[str, bytes]) -> bool:
    root, root_held = _open_child(parent, root_name)
    directories: dict[str, tuple[int, os.stat_result]] = {"": (root, root_held)}
    try:
        matches = set(os.listdir(root)) == {"document-ir", "fixture-bundle.json", "sources"}
        for name in ("document-ir", "sources"):
            directories[name] = _open_child(root, name)
        expected_names = {
            directory: {relative.rpartition("/")[2] for relative in expected
                        if relative.rpartition("/")[0] == directory}
            for directory in ("", "document-ir", "sources")
        }
        for directory, names in expected_names.items():
            descriptor, _held = directories[directory]
            if directory and set(os.listdir(descriptor)) != names:
                matches = False
            for name in names:
                body, _ = _read_regular(descriptor, name, MAX_FILE_BYTES)
                relative = f"{directory}/{name}" if directory else name
                if body != expected[relative]:
                    matches = False
        for name in ("document-ir", "sources"):
            _verify_directory_rebound(root, name, directories[name][1])
        _verify_directory_rebound(parent, root_name, root_held)
        if not _same(parent_held, os.fstat(parent)):
            raise ValueError("parent replacement")
        return matches
    finally:
        for name, (descriptor, _held) in directories.items():
            if name:
                os.close(descriptor)
        os.close(root)


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parent = None
    try:
        args = parser.parse_args(argv)
        expected = _documents()
        parent, parent_held, root_name = _open_owned_parent(args.out)
        if args.check:
            return 0 if _check_fixture(parent, parent_held, root_name, expected) else 1
        _write_fixture(parent, parent_held, root_name, expected)
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        sys.stderr.write(REJECTION)
        return 2
    finally:
        if parent is not None:
            os.close(parent)


if __name__ == "__main__":
    raise SystemExit(main())
