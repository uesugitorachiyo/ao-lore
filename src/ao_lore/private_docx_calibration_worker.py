"""Fixed entry point for sandboxed private DOCX calibration."""

from __future__ import annotations

from .private_docx_uat import _private_docx_calibration_worker_entry


def main() -> int:
    return _private_docx_calibration_worker_entry()


if __name__ == "__main__":
    raise SystemExit(main())
