.PHONY: test test-extended compile selfcheck check check-extended

export PYTHONPATH := src

test:
	python3 scripts/run-clean-tests.py

test-extended:
	python3 scripts/verify-private-calibration-assets.py --check
	AO_LORE_PRIVATE_CALIBRATION=1 python3 -m unittest tests.test_ao_lore_private_ocr_uat tests.test_ao_lore_private_pdf_uat tests.test_ao_lore_private_docx_uat -v
	AO_LORE_PRIVATE_CALIBRATION=1 python3 -m unittest discover -s tests -p 'test_ao_lore_candidate_quality_*.py' -v

compile:
	python3 -m compileall -q src tests

selfcheck:
	python3 -m ao_lore.selfcheck

check: test compile selfcheck
	git diff --check

check-extended: check test-extended
