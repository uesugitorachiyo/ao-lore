# AO Lore Six-Month Roadmap Addendum

This addendum turns AO Lore's completed standalone foundation into a maintained,
multi-domain product plan. It does not authorize live promotion, canonical
queries, provider use, publication, release, deployment, credentials, or any
other authority advance. Each milestone needs its own reviewed design,
implementation evidence, and explicit authority for real-world mutations.

## Operating sequence

The milestones are ordered by dependency:

1. evidence freshness and source-change detection;
2. isolated evidence workspaces and a workspace registry;
3. governed graph-to-candidate conversion;
4. expanded domain-pack evaluation;
5. operator lifecycle and recovery surfaces; and
6. public-release readiness without publication.

Work should advance one milestone at a time. A later milestone must not weaken
the source, authority, evidence, review, or canonical boundaries established by
an earlier milestone.

## Month 1 — Evidence freshness

Completed. AO Lore now has deterministic revalidation of retained official
evidence, selected-workspace refresh, offline replay, crash
recovery, and operator documentation. The official refresh operator exists, but
no live refresh was executed during this pass. Local integration and gates
passed with 1,265 tests and 3 skips. Independent review resolved 3 HIGH and 4
MEDIUM findings. No push, publication, release, or other authority advance
occurred.

Success was achieved by detecting simulated and retained source changes without
overwriting history, unsafe automatic graph mutation, or silent authority
changes.

## Month 2 — Isolated evidence workspaces

Completed. The exact task head and fast-forwarded local `main` passed the
complete repository-owned gate with 1,405 tests and 3 skips. Independent review
closed with no HIGH or MEDIUM findings. Canonical-root verification also exposed
and fixed a private-PDF sandbox boundary that had mounted the whole checkout;
the worker now receives only explicit required inputs and state roots.

One AO Lore deployment belongs to one company. Its strict immutable workspace
registry binds `reference`, `property`, `matter`, and `operations` workspaces
to isolated source, graph, freshness, and recovery state. A fresh public
checkout has an empty registry and no customer state. Queries select one
primary workspace plus only its direct, read-only reference imports, preserving
the exact workspace and graph origin of every evidence item. Fixed `workspace`
commands cover list, inspect, query, refresh, replay, and recovery; deterministic
offline fixtures prove that an unrelated private workspace remains unreachable.

Workspace evidence stays informational and non-canonical, and a later candidate
proposal requires a separately governed transition. This milestone adds no
multi-company tenancy, customer data, implicit global search, live campaign activity,
publication, release, deployment, or authority advance.

## Month 3 — Governed evidence-to-knowledge workflow

Phase one is completed. Verified PDF and DOCX evidence can now be ingested into
an immutable workspace document lineage and retrieved without requiring a
knowledge graph. Document-only, legacy graph-only, and combined workspaces use
origin-qualified evidence with the existing freshness, qualification,
contradiction, restricted-evidence, refusal, and output gates. The fixed query
surface and deterministic direct/check rehearsal passed independent review and
the complete branch and fast-forwarded local `main` gates. The integrated gate
ran 1,481 tests with 3 skips, and the closure verifier ran 1,478 pytest tests
with 3 skips. No provider, candidate, review, promotion, publication, release,
deployment, or authority advance occurred.

Phase two is completed. An explicit proposal binds an ordered selection of
verified document evidence, verified graph evidence, or both to the exact
workspace registry generation, source origins, excerpts, claims and
relationships when present, freshness, qualification, conflict, and semantic
review state. Preparation is read-only. Apply requires a separately persisted,
source-head-bound authorization, consumes it exactly once, and materializes
only the selected evidence as an unreviewed, non-canonical candidate. Stale,
restricted, unreachable, contradictory, ambiguous, drifted, or competing state
fails closed. Immutable transaction records, idempotent retry, crash recovery,
concurrency coverage, inspection, fixed CLI commands, and a deterministic
disposable-root rehearsal cover the governed boundary.

Independent architecture, product, candidate-boundary, security, filesystem,
concurrency, privacy, and test reviews closed with no HIGH or MEDIUM findings.
At the terminal feature head and the fast-forwarded local `main`, `make check`
passed 1,542 tests with 3 skips; compile, self-check, and diff checks also
passed. The closure verifier passed 1,539 pytest tests with 3 skips and 1,883
subtests at both heads. No live candidate selection, review, promotion,
canonical mutation, provider use, publication, release, deployment, or
authority advance occurred.

Success was achieved: an operator can create a reviewable non-canonical
candidate from verified evidence without requiring a graph for ordinary
retrieval, creating a review event, modifying `brain/`, or receiving promotion
authority. Graph
enrichment must demonstrate a measurable retrieval or reasoning improvement;
graph size is not a success metric. See
`docs/superpowers/specs/2026-08-13-progressive-evidence-architecture-design.md`.

Phase three is completed locally. AO Lore now credits the navigable-agent-skill
research that inspired it while stating that this repository is an independent
implementation. A deterministic, public-safe, offline rehearsal composes three
synthetic documents through document retrieval, optional graph retrieval,
governed evidence selection, one immutable candidate, one accepted review, a
separately bound fixture authorization, additive promotion, and canonical
status, search, and answer. Exact retry and recovery are covered without
opening default runtime state or granting real review, promotion, canonical
query, provider, network, publication, release, or deployment authority.

The canonical lifecycle now supports a closed multi-origin v0.4 entry for the
selected document and graph origins used by this proof. Legacy v0.1 through
v0.3 contracts and readbacks remain supported without reinterpretation. The
v0.4 path preserves the ordered evidence-origin selection digest and exact
per-item provenance; it does not manufacture a synthetic source or collapse
distinct evidence identities.

## Month 4 — Generic domain-pack evaluation

Expand independently authored UAT across domain triage, workflow, notice and
recordkeeping, inspection preparation, authority comparison, source conflicts,
supersession, and domain-specific refusal. Add paraphrases that do not copy
fixtures or sources, adversarial conclusion requests, missing-evidence cases,
and qualified domain-expert review.
Record evidence completeness, retrieval precision, qualification accuracy,
refusal quality, and contradiction handling.

Success means the system meets precommitted thresholds on an independently
reviewed corpus rather than merely reproducing its own graph declarations.

## Month 5 — Operator lifecycle

Add fixed workspace status, source inspection, relationship inspection, health,
refresh-needed, backup, restore, and diagnostic surfaces. Human and JSON output
must expose authority, currency, conflicts, qualifications, and exact evidence
identities without private paths or content leakage. Document adding a workspace,
refreshing it, investigating drift, querying it, proposing a candidate, and
using the separate review and promotion workflows.

Success means an operator can complete every supported lifecycle action without
editing runtime JSON or supplying arbitrary filesystem, network, policy,
authority, concurrency, or force overrides.

## Month 6 — Clean reusable baseline and public-release readiness

The clean-baseline implementation has neutral workspace, graph, and lifecycle
proofs built from synthetic roots and records. Source review remains in
progress. External domain packs remain separately reviewed inputs. Private
repository, hosted-CI, protection, and publication status are not source-owned
facts and remain pending until separately authorized verification. These facts
grant no authority to rewrite an external system or change this source.

Final hosted-CI and branch-protection truth belongs in a bounded external
evidence packet owned by AO Mission. An external readback updates that packet,
not this durable capability roadmap. Local source evidence does not establish
hosted-CI enforcement, protection, visibility, publication, release, or
deployment.

The attribution and sanitized-lifecycle evidence remains in the external
campaign packet rather than embedded as workstation-specific source data here.
No public visibility, release, or deployment authority follows from that
readback.

Audit the current tree and Git history for secrets, private paths, private
evidence, licensing conflicts, and unsafe defaults. Prepare `SECURITY.md`,
`CONTRIBUTING.md`, support and vulnerability-reporting policies, public-data
attribution, clean-environment packaging tests, schema migration policy,
backup/recovery procedures, hosted-CI design, and dependency scanning. Conduct
an independent readiness review and retain the current private-origin,
hosted-CI, and protection results as bounded external evidence; none grants
further publication, release, deployment, or authority.

Success means no known privacy, security, licensing, packaging, documentation,
or recovery blocker remains. It does not itself publish the repository.

## Deferred decisions and external domain packs

- Real canonical queries and live promotion or rollback remain separately
  authorized operations.
- Candidate accept/reject decisions remain operator decisions.
- Evidence-graph membership remains informational and non-canonical.
- External domain packs remain outside the neutral baseline and require an
  explicit review decision before adoption, archival, or retirement.
- AO Lore remains outside the AO Stack manifest by design. The architecture
  verifier's `MANIFEST_UNKNOWN_REPOSITORY_SELECTOR` result is an external
  coverage limitation, not a request to register AO Lore as a stack component.
- The generic factory closure wrapper passes when invoked with AO Lore's
  required `PYTHONPATH=src`; the repository-owned `make check` remains the
  authoritative complete product gate.
