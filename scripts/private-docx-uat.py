#!/usr/bin/env python3
"""Operate the fixed, offline private DOCX qualification campaign."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any


_REJECTED = "ao-lore-private-docx-uat: rejected\n"
_ACTIONS = ("prepare-seed", "run", "cleanup")
_EXPECTED_CORPUS_ID = "docx-nomagic-uk-public-sector-v1"
_EXPECTED_TRANSFORMATION_ID = "restore-ooxml-local-header-v1"
_EXPECTED_ITEM_IDS = tuple(f"docx-domain-{index:02d}" for index in range(1, 5))
_MAX_RESULT_NODES = 200_000
_MAX_RESULT_DEPTH = 32
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGING_RE = re.compile(r"^private-docx-uat-[0-9a-f]{24}$")
_PREPARED_FIELDS = {
    "batch_manifest",
    "batch_manifest_digest",
    "corpus_manifest",
    "corpus_digest",
    "expectation_digest",
    "staging_name",
    "staging_identity",
    "control_identity",
    "journal_identity",
    "sources",
    "runtime_root",
    "corpus_root",
}


class _ArgumentRejected(Exception):
    """An intentionally content-free command-line rejection."""


class _NonEchoArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentRejected("operator arguments rejected")


class _OnceFlag(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, False) is True:
            raise _ArgumentRejected("operator arguments rejected")
        setattr(namespace, self.dest, True)


def _build_parser() -> argparse.ArgumentParser:
    parser = _NonEchoArgumentParser(
        prog="private-docx-uat.py",
        description="Operate the fixed reviewed private-DOCX UAT corpus.",
    )
    parser.add_argument(
        "--json", action=_OnceFlag, nargs=0, help="emit canonical JSON"
    )
    actions = parser.add_subparsers(
        dest="action", required=True, parser_class=_NonEchoArgumentParser
    )
    for action in _ACTIONS:
        action_parser = actions.add_parser(action)
        action_parser.add_argument(
            "--json",
            action=_OnceFlag,
            nargs=0,
            default=argparse.SUPPRESS,
            help="emit canonical JSON",
        )
    return parser


def _load_service() -> Any:
    uat = importlib.import_module("ao_lore.private_docx_uat")
    domain = importlib.import_module("ao_lore.private_docx_domain")
    return SimpleNamespace(
        DOCX_PRIVATE_CORPUS_ID=domain.DOCX_PRIVATE_CORPUS_ID,
        DOCX_TRANSFORMATION_ID=domain.DOCX_TRANSFORMATION_ID,
        DOCX_UAT_ITEM_IDS=domain.DOCX_UAT_ITEM_IDS,
        PrivateDocxPreparedRun=uat.PrivateDocxPreparedRun,
        prepare_private_docx_uat_run=uat.prepare_private_docx_uat_run,
        validate_private_docx_prepared_run=lambda value: uat._validate_live_prepared_run(
            value, value.corpus_manifest, value.runtime_root
        ),
        run_private_docx_uat=uat.run_private_docx_uat,
        validate_private_docx_readback=uat.validate_private_docx_readback,
        prepare_private_docx_qualification_inputs=(
            domain.prepare_private_docx_qualification_inputs
        ),
        cleanup_private_docx_corpus=domain.cleanup_private_docx_corpus,
    )


def _detach_json(
    value: Any,
    *,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> Any:
    """Own one exact built-in JSON tree before contract validation."""

    if _budget is None:
        _budget = [_MAX_RESULT_NODES]
    if _depth > _MAX_RESULT_DEPTH or _budget[0] < 1:
        raise ValueError("invalid operator result")
    _budget[0] -= 1
    if type(value) is dict:
        detached: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("invalid operator result")
            detached[key] = _detach_json(item, _depth=_depth + 1, _budget=_budget)
        return detached
    if type(value) is list:
        return [
            _detach_json(item, _depth=_depth + 1, _budget=_budget)
            for item in value
        ]
    if type(value) in {str, bool, int} or value is None:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("invalid operator result")


def _fixed_identity(service: Any) -> tuple[str, tuple[str, ...], str]:
    corpus_id = service.DOCX_PRIVATE_CORPUS_ID
    item_ids = service.DOCX_UAT_ITEM_IDS
    transformation_id = service.DOCX_TRANSFORMATION_ID
    if type(corpus_id) is not str or corpus_id != _EXPECTED_CORPUS_ID:
        raise ValueError("invalid corpus identity")
    if (
        type(item_ids) is not tuple
        or item_ids != _EXPECTED_ITEM_IDS
        or any(type(item_id) is not str for item_id in item_ids)
    ):
        raise ValueError("invalid item identities")
    if (
        type(transformation_id) is not str
        or transformation_id != _EXPECTED_TRANSFORMATION_ID
    ):
        raise ValueError("invalid transformation identity")
    return _EXPECTED_CORPUS_ID, tuple(list(_EXPECTED_ITEM_IDS)), transformation_id


def _false_fields() -> dict[str, bool]:
    return {
        "qualified": False,
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def _ordered_item_ids(documents: Any) -> tuple[str, ...]:
    if type(documents) is not list:
        raise ValueError("invalid operator result")
    item_ids: list[str] = []
    for document in documents:
        if type(document) is not dict or type(document.get("item_id")) is not str:
            raise ValueError("invalid operator result")
        item_ids.append(document["item_id"])
    return tuple(item_ids)


def _canonical_digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _prepared_payload(result: Any) -> dict[str, Any]:
    fields = vars(result)
    if type(fields) is not dict or set(fields) != _PREPARED_FIELDS:
        raise ValueError("invalid prepared run")
    if (
        type(result.sources) is not tuple
        or len(result.sources) != len(_EXPECTED_ITEM_IDS)
        or any(type(source) is not dict for source in result.sources)
        or type(result.staging_identity) is not tuple
        or len(result.staging_identity) != 2
        or type(result.control_identity) is not tuple
        or len(result.control_identity) != 2
        or type(result.journal_identity) is not tuple
        or len(result.journal_identity) != 2
        or type(result.runtime_root) is not type(Path())
        or type(result.corpus_root) is not type(Path())
    ):
        raise ValueError("invalid prepared run")
    return _detach_json(
        {
            "batch_manifest": result.batch_manifest,
            "batch_manifest_digest": result.batch_manifest_digest,
            "corpus_manifest": result.corpus_manifest,
            "corpus_digest": result.corpus_digest,
            "expectation_digest": result.expectation_digest,
            "staging_name": result.staging_name,
            "staging_identity": list(result.staging_identity),
            "control_identity": list(result.control_identity),
            "journal_identity": list(result.journal_identity),
            "sources": list(result.sources),
            "runtime_identity": {
                "absolute": result.runtime_root.is_absolute(),
                "corpus_bound": (
                    result.corpus_root
                    == result.runtime_root / "private-docx" / "corpus"
                ),
            },
        }
    )


def _valid_identity(value: Any) -> bool:
    return (
        type(value) is list
        and len(value) == 2
        and all(type(part) is int and part >= 0 for part in value)
    )


def _prepare(
    service: Any,
    corpus_id: str,
    item_ids: Sequence[str],
    transformation_id: str,
) -> dict[str, Any]:
    result = service.prepare_private_docx_uat_run()
    if type(result) is not service.PrivateDocxPreparedRun:
        raise ValueError("invalid prepared run")
    if service.validate_private_docx_prepared_run(result) is not result:
        raise ValueError("invalid prepared run")
    prepared = _prepared_payload(result)
    corpus = prepared["corpus_manifest"]
    batch = prepared["batch_manifest"]
    sources = prepared["sources"]
    if (
        type(corpus) is not dict
        or corpus.get("corpus_id") != corpus_id
        or _ordered_item_ids(corpus.get("documents")) != tuple(item_ids)
        or type(batch) is not dict
        or batch.get("format_id") != "docx"
        or _ordered_item_ids(batch.get("documents")) != tuple(item_ids)
        or prepared["batch_manifest_digest"] != _canonical_digest(batch)
        or prepared["corpus_digest"] != _canonical_digest(corpus)
        or prepared["expectation_digest"] != corpus.get("expectation_digest")
        or any(
            type(prepared[field]) is not str
            or _DIGEST_RE.fullmatch(prepared[field]) is None
            for field in (
                "batch_manifest_digest",
                "corpus_digest",
                "expectation_digest",
            )
        )
        or type(prepared["staging_name"]) is not str
        or _STAGING_RE.fullmatch(prepared["staging_name"]) is None
        or not _valid_identity(prepared["staging_identity"])
        or not _valid_identity(prepared["control_identity"])
        or not _valid_identity(prepared["journal_identity"])
        or prepared["runtime_identity"] != {
            "absolute": True,
            "corpus_bound": True,
        }
        or type(sources) is not list
        or len(sources) != len(item_ids)
    ):
        raise ValueError("invalid prepared run")
    corpus_documents = corpus["documents"]
    for expected, source, document in zip(
        item_ids, sources, corpus_documents, strict=True
    ):
        if (
            type(source) is not dict
            or set(source) != {
                "item_id",
                "original_digest",
                "derived_digest",
                "transformation_id",
            }
            or source.get("item_id") != expected
            or source.get("original_digest") != document.get("source_digest")
            or source.get("derived_digest") != document.get("derived_digest")
            or source.get("transformation_id") != transformation_id
            or document.get("transformation_id") != transformation_id
            or any(
                type(source[field]) is not str
                or _DIGEST_RE.fullmatch(source[field]) is None
                for field in ("original_digest", "derived_digest")
            )
        ):
            raise ValueError("invalid prepared run")
    return {
        "action": "prepare-seed",
        "corpus_id": corpus_id,
        "documents": len(item_ids),
        "item_ids": list(item_ids),
        "lifecycle_status": "prepared",
        **_false_fields(),
    }


def _run(service: Any, corpus_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
    supplied = _detach_json(service.run_private_docx_uat())
    validated = _detach_json(
        service.validate_private_docx_readback(
            supplied, expected_item_ids=item_ids
        )
    )
    if validated != supplied or validated.get("corpus_id") != corpus_id:
        raise ValueError("invalid UAT result")
    counts = _detach_json(validated["counts"])
    conversions = _detach_json(validated["conversion_counts"])
    flags = _false_fields()
    if any(validated.get(key) is not value for key, value in flags.items()):
        raise ValueError("invalid UAT authority")
    return {
        "action": "run",
        "corpus_id": corpus_id,
        "item_ids": list(item_ids),
        "lifecycle_status": validated["lifecycle_status"],
        "counts": counts,
        "conversion_counts": conversions,
        "tuning_decision": validated["tuning_decision"],
        **flags,
    }


def _cleanup(service: Any, corpus_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
    result = _detach_json(service.cleanup_private_docx_corpus())
    if type(result) is not dict or set(result) != {
        "corpus_id",
        "removed",
        "verified_documents",
    }:
        raise ValueError("invalid cleanup result")
    removed = result["removed"]
    verified = result["verified_documents"]
    if (
        result["corpus_id"] != corpus_id
        or type(removed) is not bool
        or type(verified) is not int
        or verified < 0
        or verified > len(item_ids)
        or (removed and verified != len(item_ids))
        or (not removed and verified != 0)
    ):
        raise ValueError("invalid cleanup result")
    return {
        "action": "cleanup",
        "corpus_id": corpus_id,
        "item_ids": list(item_ids),
        "removed": removed,
        "verified_documents": verified,
        **_false_fields(),
    }


def _render_human(value: Mapping[str, Any]) -> str:
    cells: list[str] = []
    for key, cell in value.items():
        if type(cell) is str:
            rendered = json.dumps(cell, ensure_ascii=True)[1:-1]
        elif type(cell) is bool:
            rendered = "true" if cell else "false"
        elif type(cell) is int:
            rendered = str(cell)
        elif type(cell) in {list, dict}:
            rendered = json.dumps(
                cell,
                allow_nan=False,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raise ValueError("invalid operator output")
        cells.append(f"{key}={rendered}")
    return " ".join(cells) + "\n"


def _serialize(value: Mapping[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    return _render_human(value)


class _DiscardingStream:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _neutralize_stdout() -> None:
    sys.stdout = _DiscardingStream()


def _neutralize_stderr() -> None:
    sys.stderr = _DiscardingStream()


def _reject() -> int:
    failed = False
    try:
        written = sys.stderr.write(_REJECTED)
        if type(written) is not int or written != len(_REJECTED):
            failed = True
    except Exception:
        failed = True
    try:
        sys.stderr.flush()
    except Exception:
        failed = True
    if failed:
        _neutralize_stderr()
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Parse first, lazy-import one service facade, validate, and write once."""

    try:
        raw_arguments = list(sys.argv[1:] if argv is None else argv)
        if sum(
            1
            for argument in raw_arguments
            if type(argument) is str and argument == "--json"
        ) > 1:
            raise _ArgumentRejected("operator arguments rejected")
        arguments = _build_parser().parse_args(raw_arguments)
        service = _load_service()
        corpus_id, item_ids, transformation_id = _fixed_identity(service)
        if arguments.action == "prepare-seed":
            report = _prepare(service, corpus_id, item_ids, transformation_id)
        elif arguments.action == "run":
            report = _run(service, corpus_id, item_ids)
        elif arguments.action == "cleanup":
            report = _cleanup(service, corpus_id, item_ids)
        else:  # pragma: no cover - argparse owns this closed branch
            raise ValueError("invalid action")
        output = _serialize(report, as_json=arguments.json)
    except Exception:
        return _reject()
    try:
        written = sys.stdout.write(output)
        if type(written) is not int or written != len(output):
            raise OSError("operator output short write")
        sys.stdout.flush()
        return 0
    except Exception:
        _neutralize_stdout()
        return _reject()


if __name__ == "__main__":
    raise SystemExit(main())
