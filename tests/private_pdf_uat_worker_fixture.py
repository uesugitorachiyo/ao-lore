"""Test-only subprocess backend for the private PDF UAT integration."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

from ao_lore.docling_pdf import ConvertedPdf, DoclingBackend
from ao_lore.home import repository_root, runtime_home


def _init(backend: DoclingBackend) -> None:
    backend.version = "2.118.1"


def _convert(
    _backend: DoclingBackend,
    data: bytes,
    name: str,
    *,
    max_file_size: int,
    max_num_pages: int,
) -> ConvertedPdf:
    del name, max_file_size, max_num_pages
    text = data.decode("utf-8", errors="replace")
    return ConvertedPdf(
        status="success",
        blocks=(
            {
                "type": "heading", "text": text, "page": 1,
                "coordinates": [0.0, 0.0, 1.0, 1.0], "level": 1,
                "attributes": {},
            },
            {
                "type": "paragraph", "text": text, "page": 1,
                "coordinates": [0.0, 1.0, 1.0, 2.0], "attributes": {},
            },
        ),
        page_count=1,
    )


DoclingBackend.__init__ = _init
DoclingBackend.convert = _convert

from ao_lore.private_pdf_uat_worker import main

_replacements: list[tuple[Path, Path]] = []
_sandbox_write_blocked = False


def _swap_after_validation(held) -> None:
    global _sandbox_write_blocked
    if held.contract["phase"] != "resume":
        return
    paths = [
        repository_root() / "sources/.private-pdf-uat/run-manifest.json",
        runtime_home() / held.contract["qualification_locator"],
        *(repository_root() / item["source"] for item in held.batch["documents"]),
    ]
    for index, path in enumerate(paths):
        retained = path.with_name(path.name + f".held-test-{index}")
        try:
            os.replace(path, retained)
        except OSError as exc:
            if exc.errno not in {errno.EBUSY, errno.EROFS} or index != 0:
                raise
            _sandbox_write_blocked = True
            return
        path.write_bytes(
            b"{}\n" if index < 2 else b"%PDF-1.7\nforeign replacement\n%%EOF\n"
        )
        _replacements.append((path, retained))


def _restore_after_ingest(_held) -> None:
    if _held.contract["phase"] == "resume" and not _sandbox_write_blocked:
        raise OSError("private PDF UAT fixture escaped its read-only sandbox")
    if not _replacements:
        return
    swapped = len(_replacements)
    while _replacements:
        path, retained = _replacements.pop()
        path.unlink()
        os.replace(retained, path)
    audit = runtime_home() / "fixture-worker-swap.json"
    audit.write_bytes(json.dumps(
        {"restored": True, "swapped": swapped},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n")


raise SystemExit(main(
    after_validation=_swap_after_validation,
    after_ingest=_restore_after_ingest,
))
