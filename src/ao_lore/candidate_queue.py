"""Read-only, fully verified projection of persisted AO Lore candidates."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from ._strict_io import ContractError, require_identifier
from .candidates import (
    CandidateError,
    _candidate_root,
    load_verified_candidate,
)


MAX_CANDIDATES = 10000
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STATUSES = {"unreviewed", "accepted", "rejected", "all"}


class CandidateQueueError(CandidateError):
    """Raised when a candidate queue projection cannot be verified safely."""


def _fail(message: str, exc: Exception | None = None) -> CandidateQueueError:
    error = CandidateQueueError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _validated_cursor(after: str | None) -> str | None:
    if after is None:
        return None
    try:
        cursor = require_identifier(after, "after")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if not cursor.startswith("candidate-"):
        raise CandidateQueueError("after must use the candidate- prefix")
    return cursor


def _candidate_ids(root: Path) -> list[str]:
    candidate_ids: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise _fail("candidate root entry cannot be inspected safely", exc)
                if entry.name == "README.md":
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        raise CandidateQueueError("candidate root README.md must be a regular non-link file")
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise CandidateQueueError("candidate root contains an unexpected entry")
                try:
                    candidate_id = require_identifier(entry.name, "candidate directory name")
                except ContractError as exc:
                    raise _fail(str(exc), exc)
                if not candidate_id.startswith("candidate-"):
                    raise CandidateQueueError("candidate directory must use the candidate- prefix")
                if len(candidate_ids) >= MAX_CANDIDATES:
                    raise CandidateQueueError("candidate queue exceeds its hard candidate limit")
                candidate_ids.append(candidate_id)
    except OSError as exc:
        raise _fail("candidate root cannot be enumerated safely", exc)
    return candidate_ids


def _queue_item(candidate_id: str, root: Path) -> dict[str, Any]:
    try:
        verified = load_verified_candidate(candidate_id, candidate_root=root)
    except CandidateError as exc:
        raise _fail(f"candidate {candidate_id} failed verification", exc)
    provenance = verified["provenance"]
    inspection = verified["inspection"]
    return {
        "schema_version": "ao.lore.candidate-queue-item.v0.1",
        "candidate_id": candidate_id,
        "candidate_digest": inspection["candidate_digest"],
        "provenance_digest": inspection["provenance_digest"],
        "created_at": provenance["created_at"],
        "source_digest": provenance["source_digest"],
        "parser_id": provenance["parser_id"],
        "parser_version": provenance["parser_version"],
        "review_status": inspection["review_status"],
        "verified_review_events": inspection["verified_review_events"],
        "latest_event_digest": inspection["latest_event_digest"],
        "canonical": False,
        "promotion_authority": False,
    }


def list_candidates(
    *,
    status: str = "unreviewed",
    limit: int = DEFAULT_LIMIT,
    after: str | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """List verified candidates without mutating or exposing persisted state."""

    if not isinstance(status, str) or status not in STATUSES:
        raise CandidateQueueError("status must be unreviewed, accepted, rejected, or all")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise CandidateQueueError("limit must be an integer")
    if limit < 1 or limit > MAX_LIMIT:
        raise CandidateQueueError(f"limit must be between 1 and {MAX_LIMIT}")
    cursor = _validated_cursor(after)
    root = _candidate_root(candidate_root)
    items = [_queue_item(candidate_id, root) for candidate_id in _candidate_ids(root)]
    items.sort(key=lambda item: (item["created_at"], item["candidate_id"]))

    ordered_ids = [item["candidate_id"] for item in items]
    if cursor is not None:
        try:
            start = ordered_ids.index(cursor) + 1
        except ValueError as exc:
            raise _fail("after cursor does not identify a verified candidate", exc)
    else:
        start = 0

    selected: list[dict[str, Any]] = []
    last_scanned: str | None = None
    scan_end = start
    for scan_end, item in enumerate(items[start:], start=start + 1):
        last_scanned = item["candidate_id"]
        if status == "all" or item["review_status"] == status:
            selected.append(item)
            if len(selected) == limit:
                break
    has_remaining = scan_end < len(items)
    next_after = last_scanned if has_remaining else None
    return {
        "schema_version": "ao.lore.candidate-queue-readback.v0.1",
        "requested_status": status,
        "limit": limit,
        "after": cursor,
        "returned_count": len(selected),
        "items": selected,
        "next_after": next_after,
        "canonical": False,
        "promotion_authority": False,
    }
