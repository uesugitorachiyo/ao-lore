"""Internal entry point for sandboxed private PDF calibration."""

from __future__ import annotations

from .private_pdf_uat import _private_pdf_calibration_worker_entry


def main() -> int:
    return _private_pdf_calibration_worker_entry()


if __name__ == "__main__":
    raise SystemExit(main())
