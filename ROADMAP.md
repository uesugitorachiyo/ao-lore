# AO Lore Roadmap

The completed foundation below is complemented by the forward-looking
[six-month roadmap addendum](ROADMAP-ADDENDUM.md).

## Deterministic standalone foundation

- [x] Strict contracts and architecture invariants
- [x] Eligibility, normalization, parser scoring, parse quality, coverage, and sufficiency functions
- [x] Offline parser benchmark harness and fixture corpus
- [x] One-primary production parsing with bounded fallback
- [x] Canonical document IR and IR-only candidate distillation
- [x] Coverage-first navigation, memory, and delta replanning
- [x] Independent model-role configuration and scripted offline adapters
- [x] Evidence-only synthesis
- [x] Repository-local evaluation comparison migrated and verified
- [x] Repository-local drift monitoring migrated and verified
- [x] Standalone self-check and sibling-dependency scan

## Production adapters and product surfaces

- [x] Benchmark and calibrate born-digital PDF parsing with Docling 2.118.1 and OCR disabled
- [x] Ingest one contained born-digital PDF into an automatically persisted non-canonical candidate
- [x] Implement a dedicated native OOXML DOCX adapter and deterministic benchmark contracts
- [x] Execute and retain the exact private DOCX campaign evidence
- [x] Activate the DOCX adapter only if retained representative evidence yields `hold`
- [x] Implement and benchmark OCR/layout adapters
- [x] Persist candidates beneath `working/candidates/` with append-only explicit review
- [x] Add operator-facing candidate inspection and accept/reject review controls
- [x] Add a verified, paginated pending/accepted/rejected candidate review queue
- [x] Add separately authorized candidate promotion controls
- [x] Evaluate PaddleOCR/OCR separately; keep any bounded Docling DOCX fallback separate
- [x] Expand the public-safe PDF fixture corpus and threshold calibration
- [x] Add bounded, sequential, resumable PDF batch ingestion
- [x] Record exact non-skipped four-document Ubuntu operator UAT evidence
- [x] Record path-free private PDF calibration evidence and seed tuning decision
- [x] Calibrate a separately reviewed representative-domain corpus without replacing the public Ubuntu seed

## Canonical reader adoption

- [x] Complete the canonical reader branch gates for mixed-version generation replay, active-only deterministic search, verified evidence projection, and closed answer outcomes
- [x] Fast-forward the approved feature head to local `main` and rerun the complete non-publishing gates on the integrated tree
- [ ] Authorize any real canonical query or live promotion/rollback separately; fixture rehearsal, branch gates, and integration do not grant that authority

## Public candidate quality controls

- [x] Verify all 486 exact claim/citation/source bindings across the six public candidates
- [x] Produce and semantically annotate the deterministic 96-claim sample
- [x] Run 24 precommitted candidate-only retrieval questions with exact evidence or refusal
- [x] Publish six crash-safe, non-authoritative recommendations and a path-free summary
- [x] Complete independent semantic, code, filesystem, security, and privacy review
- [ ] Take any candidate accept/reject action only under a separate operator decision

## Connected public evidence graphs

- [x] Define closed, bounded contracts for acquisition, authority, relationships, graph inspection, queries, campaign summaries, and recovery
- [x] Define reusable domain-pack acquisition and immutable offline replay with exact official locators
- [x] Build a deterministic authority-aware graph with exact-span claims, typed relationships, and zero orphans
- [x] Run independently precommitted cross-document retrieval cases with exact evidence, qualifications, investigation, or refusal
- [x] Complete independent source, semantic, retrieval, filesystem, security, privacy, and provenance review with no unresolved high or medium findings
- [ ] Treat any graph claim as candidate or canonical knowledge only through separately authorized existing review and promotion controls

## Explicitly separate authority

Reusable workspace and graph APIs are covered by deterministic neutral
rehearsals; external domain packs remain separately reviewed inputs.

Live providers, credentials, hosted CI changes, release, publication, and
deployment require separate operator approval and their own evidence.
