"""Strict runtime-only contracts for representative-domain private PDFs."""

from __future__ import annotations

import math
import os
import re
import stat
from dataclasses import dataclass
from dataclasses import field
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from typing import Callable

from ._strict_io import ContractError, parse_strict_json, reject_symlink_ancestors


DOMAIN_CORPUS_ID = "pdf-nomagic-uk-public-sector-v1"
DOMAIN_ITEM_IDS = ("domain-01", "domain-02", "domain-03", "domain-04")
DOMAIN_PAGE_COUNTS = (1, 2, 4, 10)
DOMAIN_TRANSFORMATION_ID = "pdf-nomagic-restore-five-byte-header-v1"
DOMAIN_PROVENANCE_DIGEST = (
    "sha256:adbf22aeb0a01e677cadc3805687f21b30ac4e4c70ed7694cdb72afb8101be65"
)

_SCHEMA_VERSION = "ao.lore.private-pdf-domain-review.v0.1"
_DATASET_ID = "napierone-pdf-nomagic"
_PARSER_ID = "docling"
_PARSER_VERSION = "2.118.1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.pdf$")
_BLOCK_TYPES = frozenset(
    {"heading", "paragraph", "list", "table", "code", "image", "caption", "footnote", "link"}
)
_MAX_REVIEW_BYTES = 1024 * 1024
_MAX_JSON_NODES = 2048
_MAX_JSON_DEPTH = 16
MAX_DOMAIN_PDF_BYTES = 50 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class PrivatePdfDomainError(RuntimeError):
    """Raised at the stable private representative-domain boundary."""


@dataclass(frozen=True)
class PdfQualification:
    page_count: int
    encrypted: bool
    readable_text: bool


@dataclass(frozen=True)
class PreparedDomainDocument:
    item_id: str
    source_digest: str
    derived_digest: str
    page_count: int
    body: bytes = field(repr=False)
    expected_text: tuple[str, ...] = field(repr=False)
    required_block_types: tuple[str, ...]


@dataclass(frozen=True)
class ReviewedDomainDocument:
    item_id: str
    source_file: str
    source_digest: str
    derived_digest: str
    page_count: int
    expected_text: tuple[str, ...]
    required_block_types: tuple[str, ...]


@dataclass(frozen=True)
class ReviewedDomainCorpus:
    corpus_id: str
    provenance_digest: str
    transformation_id: str
    documents: tuple[ReviewedDomainDocument, ...]


def _invalid(cause: BaseException | None = None) -> ContractError:
    error = ContractError("private PDF domain review is invalid")
    if cause is not None:
        error.__cause__ = cause
    return error


def _detach_json(value: Any, *, depth: int = 0, count: list[int] | None = None) -> Any:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
        raise _invalid()
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, child in value.items():
            if type(key) is not str or len(key) > 128:
                raise _invalid()
            result[key] = _detach_json(child, depth=depth + 1, count=count)
        return result
    if type(value) is list:
        return [_detach_json(child, depth=depth + 1, count=count) for child in value]
    if type(value) is str:
        if len(value) > 1024:
            raise _invalid()
        return value[:]
    if type(value) is bool or value is None or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return float(value)
    raise _invalid()


def _exact(value: dict[str, Any], keys: set[str]) -> bool:
    return set(value) == keys


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _source_file(value: Any) -> bool:
    return (
        type(value) is str
        and 5 <= len(value) <= 128
        and _SOURCE_FILE_RE.fullmatch(value) is not None
        and value not in {".pdf", "..pdf"}
    )


def _closed_strings(value: Any, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= 32:
        raise _invalid()
    result: list[str] = []
    for item in value:
        if type(item) is not str or not 1 <= len(item) <= 256:
            raise _invalid()
        if allowed is not None and item not in allowed:
            raise _invalid()
        if item in result:
            raise _invalid()
        result.append(item)
    return tuple(result)


def validate_private_pdf_domain_review(value: Mapping[str, Any]) -> ReviewedDomainCorpus:
    """Detach and validate one exact no-authority representative review."""

    try:
        if type(value) is not dict:
            raise _invalid()
        review = _detach_json(value)
        if not _exact(
            review,
            {
                "schema_version",
                "corpus_id",
                "provenance",
                "representative_domain",
                "documents",
                "parser_id",
                "parser_version",
                "ocr_enabled",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            },
        ):
            raise _invalid()
        if (
            review["schema_version"] != _SCHEMA_VERSION
            or review["corpus_id"] != DOMAIN_CORPUS_ID
            or review["representative_domain"] is not True
            or review["parser_id"] != _PARSER_ID
            or review["parser_version"] != _PARSER_VERSION
            or review["ocr_enabled"] is not False
            or review["provider_calls"] is not False
            or review["promotion_authority"] is not False
            or review["claims_authority_advance"] is not False
        ):
            raise _invalid()
        provenance = review["provenance"]
        if type(provenance) is not dict or not _exact(
            provenance, {"dataset_id", "document_digest", "transformation_id"}
        ):
            raise _invalid()
        if (
            provenance["dataset_id"] != _DATASET_ID
            or provenance["document_digest"] != DOMAIN_PROVENANCE_DIGEST
            or provenance["transformation_id"] != DOMAIN_TRANSFORMATION_ID
        ):
            raise _invalid()
        documents = review["documents"]
        if type(documents) is not list or len(documents) != 4:
            raise _invalid()
        reviewed: list[ReviewedDomainDocument] = []
        source_files: set[str] = set()
        source_digests: set[str] = set()
        derived_digests: set[str] = set()
        anchors: set[str] = set()
        document_keys = {
            "item_id",
            "source_file",
            "source_digest",
            "derived_digest",
            "page_count",
            "expected_text",
            "required_block_types",
        }
        for index, document in enumerate(documents):
            if type(document) is not dict or not _exact(document, document_keys):
                raise _invalid()
            source_file = document["source_file"]
            source_digest = document["source_digest"]
            derived_digest = document["derived_digest"]
            expected_text = _closed_strings(document["expected_text"])
            required_block_types = _closed_strings(
                document["required_block_types"], allowed=_BLOCK_TYPES
            )
            if (
                document["item_id"] != DOMAIN_ITEM_IDS[index]
                or type(document["page_count"]) is not int
                or document["page_count"] != DOMAIN_PAGE_COUNTS[index]
                or not _source_file(source_file)
                or not _digest(source_digest)
                or not _digest(derived_digest)
                or source_digest == derived_digest
                or source_file in source_files
                or source_digest in source_digests
                or derived_digest in derived_digests
                or any(anchor in anchors for anchor in expected_text)
            ):
                raise _invalid()
            source_files.add(source_file)
            source_digests.add(source_digest)
            derived_digests.add(derived_digest)
            anchors.update(expected_text)
            reviewed.append(
                ReviewedDomainDocument(
                    item_id=document["item_id"],
                    source_file=source_file,
                    source_digest=source_digest,
                    derived_digest=derived_digest,
                    page_count=document["page_count"],
                    expected_text=expected_text,
                    required_block_types=required_block_types,
                )
            )
        return ReviewedDomainCorpus(
            corpus_id=DOMAIN_CORPUS_ID,
            provenance_digest=DOMAIN_PROVENANCE_DIGEST,
            transformation_id=DOMAIN_TRANSFORMATION_ID,
            documents=tuple(reviewed),
        )
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid(exc) from exc


def parse_private_pdf_domain_review(body: bytes) -> ReviewedDomainCorpus:
    """Parse duplicate-key-safe review bytes and return the detached contract."""

    if type(body) is not bytes or len(body) > _MAX_REVIEW_BYTES:
        raise _invalid()
    value = parse_strict_json(body, "private PDF domain review")
    return validate_private_pdf_domain_review(value)


def restore_pdf_nomagic_header(source: bytes) -> bytes:
    """Restore exactly the five-byte PDF signature removed by PDF-NOMAGIC."""

    if (
        type(source) is not bytes
        or len(source) < 9
        or source[:5] != b"\x00" * 5
        or source[5:7] != b"1."
        or source[7:8] not in tuple(bytes((digit,)) for digit in range(ord("0"), ord("9") + 1))
        or source[8:9] not in {b"\n", b"\r"}
    ):
        raise PrivatePdfDomainError("representative PDF source is invalid")
    return b"%PDF-" + source[5:]


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    body = b"".join(chunks)
    if not body or len(body) > maximum:
        raise OSError("source size is invalid")
    return body


def _sha256(body: bytes) -> str:
    return "sha256:" + sha256(body).hexdigest()


def load_reviewed_domain_sources(
    source_root: Path,
    review: ReviewedDomainCorpus,
    *,
    qualify: Callable[[bytes], PdfQualification],
) -> tuple[PreparedDomainDocument, ...]:
    """Load exact approved sources through one held root and return derived bytes."""

    root_descriptor: int | None = None
    try:
        if not isinstance(source_root, Path) or type(review) is not ReviewedDomainCorpus:
            raise OSError("source contract is invalid")
        if not callable(qualify):
            raise OSError("source qualifier is invalid")
        reject_symlink_ancestors(source_root, include_self=True)
        root_before = os.lstat(source_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise OSError("source root is invalid")
        root_descriptor = os.open(source_root, _DIRECTORY_FLAGS)
        root_opened = os.fstat(root_descriptor)
        if (root_before.st_dev, root_before.st_ino) != (
            root_opened.st_dev,
            root_opened.st_ino,
        ):
            raise OSError("source root changed")
        prepared: list[PreparedDomainDocument] = []
        for reviewed in review.documents:
            before = os.stat(
                reviewed.source_file, dir_fd=root_descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > MAX_DOMAIN_PDF_BYTES
            ):
                raise OSError("source binding is invalid")
            descriptor = os.open(
                reviewed.source_file,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if _stable_metadata(before) != _stable_metadata(opened):
                    raise OSError("source changed")
                source = _read_bounded(descriptor, MAX_DOMAIN_PDF_BYTES)
                if _sha256(source) != reviewed.source_digest:
                    raise OSError("source digest drifted")
                derived = restore_pdf_nomagic_header(source)
                if _sha256(derived) != reviewed.derived_digest:
                    raise OSError("derived digest drifted")
                qualification = qualify(derived)
                if (
                    type(qualification) is not PdfQualification
                    or type(qualification.page_count) is not int
                    or qualification.page_count != reviewed.page_count
                    or type(qualification.encrypted) is not bool
                    or qualification.encrypted
                    or type(qualification.readable_text) is not bool
                    or not qualification.readable_text
                ):
                    raise OSError("source qualification drifted")
                after = os.fstat(descriptor)
                public = os.stat(
                    reviewed.source_file,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _stable_metadata(opened) != _stable_metadata(after)
                    or _stable_metadata(opened) != _stable_metadata(public)
                ):
                    raise OSError("source changed")
            finally:
                os.close(descriptor)
            prepared.append(
                PreparedDomainDocument(
                    item_id=reviewed.item_id,
                    source_digest=reviewed.source_digest,
                    derived_digest=reviewed.derived_digest,
                    page_count=reviewed.page_count,
                    body=derived,
                    expected_text=reviewed.expected_text,
                    required_block_types=reviewed.required_block_types,
                )
            )
        root_after = os.fstat(root_descriptor)
        root_public = os.lstat(source_root)
        if (
            (root_opened.st_dev, root_opened.st_ino)
            != (root_after.st_dev, root_after.st_ino)
            or (root_opened.st_dev, root_opened.st_ino)
            != (root_public.st_dev, root_public.st_ino)
        ):
            raise OSError("source root changed")
        return tuple(prepared)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise PrivatePdfDomainError(
            "representative PDF source qualification failed"
        ) from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
