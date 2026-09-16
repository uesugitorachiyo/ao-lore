"""Deterministic bounded evidence-set selection over validated document IR."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any


RETRIEVAL_VERSION = "evidence-sets-v2"
MAX_EVIDENCE_CHARACTERS = 20_000
MAX_EVIDENCE_UTF8_BYTES = 20_000
MAX_CANDIDATE_DOCUMENTS = 64
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NAME = re.compile(r"(?:[A-Z][\w-]*)(?:\s+(?:[A-Z][\w-]*)){1,5}")
_STOP = frozenset("a an the and or of to in on at by for from with as is are was were be been this that these those what which where when why how please tell give show find list name include including across applicable applies apply specified before after since until exactly required requirement return full values value number numeric strings word only answer".split())
_DIRECTIVES = frozenset("return full values value number numeric strings word evaluation date only answer".split())
_NUMERIC = frozenset("price fee cost charge invoice quantity total sum response target limit elapsed hour hours effective amendment replace".split())


@dataclass(frozen=True)
class Selection:
    pairs: tuple[tuple[dict[str, Any], dict[str, Any]], ...]
    qualifications: tuple[str, ...]
    incomplete: bool
    restricted: bool
    freshness_review: bool


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("_", " ")


def _tokens(value: str) -> frozenset[str]:
    return frozenset(word for word in _WORD.findall(_normal(value)) if word not in _STOP)


def _blocks(values: list[dict[str, Any]]):
    for value in values:
        yield value
        yield from _blocks(value.get("children", []))


def _subject(document: dict[str, Any]) -> str | None:
    for block in document["document_ir"]["blocks"][:2]:
        text = block["text"].lstrip("# ").strip()
        if block.get("type") == "heading" or block["text"].lstrip().startswith("#"):
            title = re.split(r"\s+[—–|]\s+|\s+-\s+", text, maxsplit=1)[0]
            return " ".join(_normal(title).split()) or None
    return None


def _query_subjects(prompt: str, subjects: set[str]) -> set[str]:
    text = " ".join(_normal(prompt).split())
    matches = []
    for subject in subjects:
        for match in re.finditer(r"(?<!\w)" + re.escape(subject) + r"(?!\w)", text):
            matches.append((match.start(), match.end(), subject))
    kept = []
    for start, end, subject in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0], item[2])):
        if not any(start < old_end and end > old_start for old_start, old_end, _ in kept):
            kept.append((start, end, subject))
    return {subject for _, _, subject in kept}


def select_document_blocks(current: dict[str, Any], prompt: str, limit: int) -> Selection:
    """Return only original blocks; selection never opens files or uses providers."""
    terms = _tokens(prompt)
    phrase = " ".join(_WORD.findall(_normal(prompt)))
    if not terms:
        return Selection((), (), False, False, False)
    documents = []
    all_documents = []
    for document in current["documents"]:
        blocks = list(_blocks(document["document_ir"]["blocks"]))
        all_documents.append((document, blocks, _subject(document)))
    known_subjects = {subject for _, _, subject in all_documents if subject}
    focused = _query_subjects(prompt, known_subjects)
    restricted = False
    for document, blocks, subject in all_documents:
        if document["sensitivity"] == "restricted":
            # A named subject is a safety boundary: an unrelated restricted
            # document must not turn a public answer into a refusal merely
            # because both documents contain a common query word.
            if focused and subject in focused:
                restricted = True
            elif not focused and any(terms.intersection(_tokens(block["text"])) for block in blocks):
                restricted = True
            continue
        documents.append((document, blocks, subject))
    if restricted:
        return Selection((), (), True, True, False)
    candidates = [(document, blocks, subject) for document, blocks, subject in documents
                  if not focused or subject in focused]
    if not candidates:
        candidates = documents
    candidates = candidates[:MAX_CANDIDATE_DOCUMENTS]
    frequency = Counter(term for document, blocks, _ in candidates
                        for block in blocks for term in _tokens(block["text"]))
    count = max(1, len(candidates))
    ranked: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    freshness = False
    for document, blocks, subject in candidates:
        if document["freshness_status"] != "current":
            freshness = True
        subject_terms = _tokens(subject or "")
        content_terms = terms - subject_terms - _DIRECTIVES
        for ordinal, block in enumerate(blocks):
            block_terms = _tokens(block["text"])
            overlap = content_terms.intersection(block_terms) or terms.intersection(block_terms)
            if not overlap:
                continue
            score = sum(max(0.5, math.log1p((count - frequency[term] + .5) / (frequency[term] + .5)))
                        for term in overlap)
            text = block["text"]
            if phrase and phrase in " ".join(_WORD.findall(_normal(text))):
                score += 10.0
            heading = block.get("type") == "heading" or text.lstrip().startswith("#")
            if terms.intersection(_NUMERIC) and re.search(r"\d", text):
                score += 2.0
            if re.search(r"\b(?:effective|amend|replace|supersed)\w*\b", text, re.I):
                score += 1.5
            if heading:
                score *= .12
            ranked.append((score, ordinal, document, block))
    ranked.sort(key=lambda item: (-item[0], item[2]["document_id"], item[1], item[3]["id"]))
    per_document: set[str] = set()
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    characters = bytes_used = 0
    incomplete = len(candidates) == MAX_CANDIDATE_DOCUMENTS
    for diversity in (True, False):
        for _, _, document, block in ranked:
            identity = (document["document_id"], block["id"])
            if identity in {(doc["document_id"], value["id"]) for doc, value in selected}:
                continue
            if diversity and document["document_id"] in per_document:
                continue
            text = block["text"]
            size = len(text.encode("utf-8"))
            if len(selected) >= limit:
                break
            if characters + len(text) > MAX_EVIDENCE_CHARACTERS or bytes_used + size > MAX_EVIDENCE_UTF8_BYTES:
                incomplete = True
                continue
            selected.append((document, block))
            per_document.add(document["document_id"])
            characters += len(text)
            bytes_used += size
        if len(selected) >= limit:
            break
    qualifications = ("Evidence-set selection reached a bounded retrieval budget; evidence may be incomplete.",) if incomplete else ()
    return Selection(tuple(selected), qualifications, incomplete, False, freshness)
