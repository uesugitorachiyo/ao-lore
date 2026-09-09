"""Local registry for validating the shipped JSON Schema graph."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, RefResolver


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"


def shipped_schema_registry() -> tuple[dict[str, dict], dict[str, dict]]:
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_ROOT.glob("*.schema.json"))
    }
    store = {document["$id"]: document for document in documents.values()}
    return documents, store


def shipped_validator(schema_name: str) -> Draft202012Validator:
    documents, store = shipped_schema_registry()
    schema = documents[schema_name]
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store=store),
    )


def cross_file_references() -> list[tuple[str, str, str]]:
    documents, store = shipped_schema_registry()
    references: list[tuple[str, str, str]] = []

    def visit(source_name: str, source_id: str, value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                resolved = urldefrag(urljoin(source_id, reference)).url
                if resolved not in store:
                    raise AssertionError(
                        f"unresolved shipped schema reference: {source_name}: {reference}"
                    )
                RefResolver.from_schema(
                    documents[source_name], store=store,
                ).resolve(reference)
                references.append((source_name, reference, resolved))
            for child in value.values():
                visit(source_name, source_id, child)
        elif isinstance(value, list):
            for child in value:
                visit(source_name, source_id, child)

    for name, document in documents.items():
        visit(name, document["$id"], document)
    return sorted(references)
