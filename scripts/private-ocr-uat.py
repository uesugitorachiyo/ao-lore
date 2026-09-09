#!/usr/bin/env python3
"""Operate the fixed, offline private English-OCR campaign."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from typing import Any


_REJECTED = "ao-lore-private-ocr-uat: rejected\n"
_ACTIONS = ("prepare-runtime", "qualify", "prepare-corpus", "run", "cleanup")
_MAX_RESULT_NODES = 200_000
_MAX_RESULT_DEPTH = 32
_PUBLIC_FIELDS = {
    "action", "status", "network_accessed", "provider_calls",
    "promotion_authority", "claims_authority_advance",
}


class _ArgumentRejected(Exception):
    """An intentionally content-free command-line rejection."""


class _NonEchoArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _ArgumentRejected("operator arguments rejected")


class _OnceFlag(argparse.Action):
    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: str | None = None) -> None:
        del parser, values, option_string
        if getattr(namespace, self.dest, False) is True:
            raise _ArgumentRejected("operator arguments rejected")
        setattr(namespace, self.dest, True)


def _parser() -> argparse.ArgumentParser:
    parser = _NonEchoArgumentParser(
        prog="private-ocr-uat.py",
        description="Operate the fixed reviewed private English-OCR campaign.",
    )
    parser.add_argument("--json", action=_OnceFlag, nargs=0)
    actions = parser.add_subparsers(
        dest="action", required=True, parser_class=_NonEchoArgumentParser
    )
    for action in _ACTIONS:
        child = actions.add_parser(action)
        child.add_argument(
            "--json", action=_OnceFlag, nargs=0, default=argparse.SUPPRESS
        )
    return parser


def _load_service() -> Any:
    module = importlib.import_module("ao_lore.private_ocr_uat")
    return type("_Service", (), {"project": staticmethod(module.project_private_ocr_action)})()


def _detach(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [_MAX_RESULT_NODES]
    if depth > _MAX_RESULT_DEPTH or budget[0] < 1:
        raise ValueError("invalid operator result")
    budget[0] -= 1
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("invalid operator result")
            result[key] = _detach(item, depth=depth + 1, budget=budget)
        return result
    if type(value) is list:
        return [_detach(item, depth=depth + 1, budget=budget) for item in value]
    if type(value) in {str, bool, int} or value is None:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("invalid operator result")


def _project(action: str, value: Any) -> dict[str, Any]:
    result = _detach(value)
    if (
        type(result) is not dict
        or set(result) != _PUBLIC_FIELDS
        or result["action"] != action
        or type(result["status"]) is not str
        or result["status"] not in {"prepared", "qualified", "completed", "cleaned", "ready"}
        or any(result[field] is not False for field in (
            "network_accessed", "provider_calls", "promotion_authority",
            "claims_authority_advance",
        ))
    ):
        raise ValueError("invalid operator result")
    return result


def _serialize(value: Mapping[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    cells = []
    for key, item in value.items():
        if type(item) is str:
            rendered = json.dumps(item, ensure_ascii=True)[1:-1]
        elif type(item) is bool:
            rendered = "true" if item else "false"
        else:
            raise ValueError("invalid operator output")
        cells.append(f"{key}={rendered}")
    return " ".join(cells) + "\n"


class _DiscardingStream:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _reject() -> int:
    failed = False
    try:
        written = sys.stderr.write(_REJECTED)
        failed = type(written) is not int or written != len(_REJECTED)
    except Exception:
        failed = True
    try:
        sys.stderr.flush()
    except Exception:
        failed = True
    if failed:
        sys.stderr = _DiscardingStream()
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        if any(type(item) is not str for item in raw) or raw.count("--json") > 1:
            raise _ArgumentRejected("operator arguments rejected")
        arguments = _parser().parse_args(raw)
        service = _load_service()
        result = _project(arguments.action, service.project(arguments.action))
        output = _serialize(result, as_json=arguments.json)
    except Exception:
        return _reject()
    try:
        written = sys.stdout.write(output)
        if type(written) is not int or written != len(output):
            raise OSError("operator output short write")
        sys.stdout.flush()
        return 0
    except Exception:
        sys.stdout = _DiscardingStream()
        return _reject()


if __name__ == "__main__":
    raise SystemExit(main())
