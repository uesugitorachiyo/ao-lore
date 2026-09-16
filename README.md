# AO Lore

## Local-first, evidence-grounded document knowledge

AO Lore is a standalone, local-first system for turning documents into
verifiable evidence for retrieval and synthesis. It was inspired by
*[Don't Retrieve, Navigate: Distilling Enterprise Knowledge into Navigable
Agent Skills for QA and RAG](https://arxiv.org/abs/2604.14572)*, but is an
independent implementation.

The repository is self-contained: it has no AO Stack or sibling-repository
runtime, build, or test dependency. A fresh checkout contains no customer data
and creates no workspace state merely by being inspected.

## Why AO Lore

Ordinary RAG often treats retrieved text as interchangeable context. AO Lore
keeps an explicit evidence path instead:

```text
immutable sources -> document IR -> verified evidence -> answer
```

Verified document evidence is the default path. When useful, optional
relationship evidence can extend navigation across connected sources without
making a graph a prerequisite. Answers are bound to verified evidence; when
coverage is insufficient or contradictory, the system returns a partial answer
or refusal rather than inventing a conclusion.

This design is intended for evaluators who need to inspect provenance, test
bounded retrieval behavior, and separate document evidence from proposed or
canonical knowledge.

## What you can evaluate from a fresh checkout

- deterministic parsing into a canonical document-IR boundary;
- local, provider-free verified-document retrieval, including bounded adjacent
  document context for multi-paragraph evidence chains;
- coverage-first navigation with explicit budgets and optional evidence-graph
  enrichment;
- isolated, company-local evidence workspaces with explicit read-only
  references; and
- an explicit, governed candidate lifecycle separate from retrieval and
  canonical knowledge.

One deployment belongs to one company. The tracked foundation is offline and
local-first. It does not include a hosted service, a live model provider, a
customer corpus, or authority to promote knowledge, publish, release, or
deploy.

## Quick start

Python 3.11 or newer is recommended. The deterministic core uses only the
standard library.

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install --no-build-isolation --no-deps -e .
make check
```

Installation is optional: `make check` runs directly from `src/` using the
repository's configured `PYTHONPATH`. The gate is local and non-publishing; it
does not authorize network activity or change knowledge, release, publication,
or deployment state.

## Evaluation snapshot

AO Lore's document-retrieval core is designed to be measured independently of
optional graph navigation and knowledge promotion. The public test suite checks
deterministic parsing, retrieval, evidence boundaries, workspace isolation, and
failure-closed behavior. Private calibration assets, when absent, are explicitly
skipped rather than substituted or downloaded.

Small synthetic comparisons are useful diagnostics, not product-wide claims.
They do not establish a winner against GraphRAG, Basic Memory, or another
system; representative authorized corpora, held-out questions, and blind human
faithfulness review remain necessary for that conclusion.

### Practical RAG benchmark snapshot

The following version-pinned three-system diagnostic measured the provider-cohort
revision now merged into this repository with the requested shared reader,
`gpt-5.6-luna` at medium effort.
It used a 48-question static track and 128 lifecycle checkpoints; scheduled
errors remain in every metric denominator. The pinned synthetic corpus
fingerprint is `da912b3b31b0439f6b2bfa8f8d4df8573d785fea71f3b01ee63f8070e603dfe0`.

| Metric | AO Lore | Basic Memory | LLM-WIKI-RAG |
|---|---:|---:|---:|
| Static strict task success | **50.0%** | 47.9% | 29.2% |
| Static evidence recall | **96.9%** | 90.7% | 20.2% |
| Static complete evidence coverage | **91.7%** | 89.6% | 29.2% |
| Static retrieval p50 | **0.025 s** | 0.521 s | 0.101 s |
| Lifecycle strict task success | **55.5%** | 41.4% | 12.5% |
| Lifecycle evidence recall | **100.0%** | 88.7% | 14.3% |
| Lifecycle stale-evidence errors | 0 | 0 | 0 |

This is evidence that the current verified-document core is competitive on this
specific workload, especially across document updates; it is not a claim of a
universal winner. A separately pinned relationship-subset validation fixed the
benchmark adapter's source-ID persistence boundary: AO Lore completed all 12
scheduled attempts with 94.7% evidence recall and 75.0% complete evidence
coverage. It is a document-core relationship subset, not a certification of
optional AO graph navigation. Reader strict-success values remain sensitive to
model sampling; failures remain in denominators and are never removed.

## Architecture at a glance

AO Lore maintains distinct boundaries for immutable source material, document
IR, retrieval evidence, non-canonical candidates, accepted reviews, and
canonical generations. Graph membership is informational retrieval state, not
canonical knowledge. Candidate selection and promotion are separately governed
transitions and never an automatic outcome of ingestion or retrieval.

Read the full [architecture](docs/architecture/ao-lore.md) and the
[progressive-evidence model](docs/architecture/progressive-evidence.md) for
contracts, flows, and safety boundaries.

## Status and non-goals

This repository is an evaluator-ready local foundation. It is not a hosted
multi-tenant product, a property-management application, a legal-advice
system, or a deployment artifact. Real customer evidence belongs in a
company-owned runtime root outside tracked source, and any live transport,
provider, promotion, release, publication, or deployment requires separate
operator authority.

## Documentation

- [Architecture and contracts](docs/architecture/ao-lore.md): evidence,
  candidate, canonical, and authority boundaries.
- [Progressive evidence](docs/architecture/progressive-evidence.md): why
  verified documents work without a graph and when navigation adds value.
- [Evidence workspaces](docs/workflows/workspaces.md): company-local isolation
  and direct, read-only reference imports.
- [Local source viewer](docs/workflows/source-viewer.md): separately approved
  original-file display with native evidence revalidation.
- [Graph freshness](docs/workflows/refresh-evidence-graph.md): bounded graph
  inspection, refresh classifications, and recovery boundaries.
- [Candidate-quality review](docs/workflows/review-public-candidate-quality.md):
  the fixed, non-authoritative local review campaign.
- [Sanitized lifecycle rehearsal](workflows/rehearse-sanitized-lifecycle.md):
  fixed public-safe lifecycle verification.
- [Standalone recovery provenance](docs/recovery/standalone-migration.md):
  one-time repository migration history.
- [Contributing](CONTRIBUTING.md) and [license](LICENSE).
