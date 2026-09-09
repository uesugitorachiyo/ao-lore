#!/usr/bin/env python3
"""Run the fixed-root, offline private PDF operator UAT."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any


_REJECTED = "ao-lore-private-pdf-uat: rejected\n"
_ACTIONS = ("prepare-seed", "run", "cleanup")
_MAX_RESULT_NODES = 200_000
_MAX_RESULT_DEPTH = 32


class _ArgumentRejected(Exception):
    """An intentionally content-free command-line rejection."""


class _NonEchoArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentRejected("operator arguments rejected")


def _build_parser() -> argparse.ArgumentParser:
    parser = _NonEchoArgumentParser(
        prog="private-pdf-uat.py",
        description="Operate the fixed reviewed Ubuntu private-PDF UAT seed.",
    )
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    actions = parser.add_subparsers(
        dest="action", required=True, parser_class=_NonEchoArgumentParser
    )
    for action in _ACTIONS:
        action_parser = actions.add_parser(action)
        action_parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="emit canonical JSON",
        )
    return parser


def _load_service() -> Any:
    return importlib.import_module("ao_lore.private_pdf_uat")


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
            detached[key] = _detach_json(
                item, _depth=_depth + 1, _budget=_budget
            )
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


def _fixed_identity(service: Any) -> tuple[str, tuple[str, ...]]:
    corpus_id = service.PRIVATE_PDF_UAT_CORPUS_ID
    item_ids = service.PRIVATE_PDF_UAT_ITEM_IDS
    expected_item_ids = ("seed-01", "seed-02", "seed-03", "seed-04")
    if type(corpus_id) is not str or corpus_id != "ubuntu-pdf-seed-v1":
        raise ValueError("invalid corpus identity")
    if (
        type(item_ids) is not tuple
        or len(item_ids) != len(expected_item_ids)
        or any(type(item) is not str for item in item_ids)
        or item_ids != expected_item_ids
    ):
        raise ValueError("invalid item identities")
    return "ubuntu-pdf-seed-v1", tuple(list(expected_item_ids))


def _prepare(service: Any, corpus_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
    result = _detach_json(service.prepare_ubuntu_pdf_seed())
    validated = service.validate_private_pdf_manifest(result)
    if validated["corpus_id"] != corpus_id:
        raise ValueError("invalid prepared corpus")
    if tuple(document["item_id"] for document in validated["documents"]) != tuple(
        item_ids
    ):
        raise ValueError("invalid prepared items")
    return {
        "action": "prepare-seed",
        "corpus_id": corpus_id,
        "documents": len(item_ids),
        "status": "prepared",
    }


def _run(service: Any, corpus_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
    result = _detach_json(service.run_private_pdf_uat())
    validated = service.validate_private_pdf_readback(
        result, expected_item_ids=item_ids
    )
    if validated["corpus_id"] != corpus_id:
        raise ValueError("invalid UAT corpus")
    counts = validated["counts"]
    return {
        "action": "run",
        "corpus_id": corpus_id,
        "lifecycle_status": validated["lifecycle_status"],
        "rejected": counts["rejected"],
        "successful": counts["successful"],
        "total": counts["total"],
        "tuning_decision": validated["tuning_decision"],
    }


def _cleanup(service: Any, corpus_id: str, item_ids: Sequence[str]) -> dict[str, Any]:
    result = _detach_json(service.cleanup_private_pdf_uat(corpus_id))
    if type(result) is not dict or set(result) != {
        "corpus_id",
        "removed",
        "verified_documents",
    }:
        raise ValueError("invalid cleanup result")
    if result["corpus_id"] != corpus_id or type(result["removed"]) is not bool:
        raise ValueError("invalid cleanup result")
    verified = result["verified_documents"]
    if type(verified) is not int or verified < 0 or verified > len(item_ids):
        raise ValueError("invalid cleanup result")
    if (result["removed"] and verified != len(item_ids)) or (
        not result["removed"] and verified != 0
    ):
        raise ValueError("invalid cleanup result")
    return {
        "action": "cleanup",
        "corpus_id": corpus_id,
        "removed": result["removed"],
        "verified_documents": verified,
    }


def _render_human(value: Mapping[str, Any]) -> str:
    cells: list[str] = []
    for key, cell in value.items():
        if isinstance(cell, str):
            rendered = json.dumps(cell, ensure_ascii=True)[1:-1]
        elif type(cell) is bool:
            rendered = "true" if cell else "false"
        elif type(cell) is int:
            rendered = str(cell)
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


def _reject() -> int:
    sys.stderr.write(_REJECTED)
    return 2


def _neutralize_stdout() -> None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _build_parser().parse_args(argv)
        service = _load_service()
        corpus_id, item_ids = _fixed_identity(service)
        if arguments.action == "prepare-seed":
            report = _prepare(service, corpus_id, item_ids)
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
        sys.stdout.write(output)
        sys.stdout.flush()
        return 0
    except Exception:
        _neutralize_stdout()
        return _reject()


if __name__ == "__main__":
    raise SystemExit(main())
