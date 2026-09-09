# AO Lore

AO Lore is a standalone, local-first engine for measured document parsing,
non-canonical knowledge distillation, evidence-coverage navigation, and
evidence-bound synthesis.

AO Lore was inspired by *[Don't Retrieve, Navigate: Distilling Enterprise
Knowledge into Navigable Agent Skills for QA and
RAG](https://arxiv.org/abs/2604.14572)*. AO Lore is an independent
implementation that extends the underlying navigation idea with progressive
evidence retrieval, governed candidates, explicit review and promotion,
canonical generations, and workspace isolation.

The repository is self-contained. It does not depend on the AO Stack or any
sibling working tree at runtime, build time, or test time. AO Mission may
orchestrate development through external AO control-plane records without
changing that product boundary. Generated AO Lore state remains below
`.ao-lore/` inside this repository.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/ao_lore/` | Parser, distiller, navigator, synthesizer, evaluation, and monitoring implementation |
| `schemas/ao-lore/` | Strict machine-readable contracts |
| `tests/` | Deterministic unit, integration, and boundary tests |
| `docs/architecture/` | Durable architecture and authority boundaries |
| `workflows/` | Operator procedures |
| `brain/` | Canonical OKF v0.2 knowledge |
| `sources/` | Immutable source evidence |
| `inbox/` | Unprocessed inputs |
| `working/candidates/` | Non-canonical proposed OKF |
| `.ao-lore/` | Ignored runtime state, caches, reports, and execution evidence |

## Current foundation

The deterministic implementation provides:

- eligibility-filtered, benchmark-calibrated parser selection;
- versioned benchmark manifests and numeric, format-aware parse-quality gates;
- native Markdown, structural HTML, and direct-text adapters with bounded
  fallback;
- an exact-version, OCR-disabled Docling PDF adapter qualified by a deterministic
  public-safe fixture benchmark;
- a canonical document-IR boundary and IR-only candidate distillation;
- atomic immutable candidate persistence and append-only accept/reject review;
- governed, separately authorized additive candidate promotion, inspection,
  crash recovery, and append-only rollback controls;
- fixed-root, read-only canonical generation replay with deterministic local
  status, lexical search, and evidence-bound answer projection;
- single-document born-digital PDF ingestion with automatic non-canonical
  candidate persistence and a verified review queue;
- deterministic public PDF calibration across accepted and expected-rejection
  fixtures, plus sequential manifest-driven ingestion with verified resume;
- coverage-first navigation with memory, delta replanning, and hard budgets;
- independent parser, distiller, navigator, and synthesizer role runtimes;
- evidence-ledger-only synthesis producing an answer, partial answer, or
  refusal;
- repository-local evaluation and monitoring over supplied, digest-bound
  evidence;
- a strict native OOXML DOCX adapter, deterministic public fixtures, and a
  qualification-bound private campaign with retained exact offline evidence.
- an offline deterministic candidate-quality campaign with exact checks,
  semantic sampling, candidate-only retrieval UAT, and non-authoritative results.
- a bounded, authority-aware evidence-graph pipeline for connected official
  public sources, with immutable acquisition, exact-span semantic checks,
  offline replay, and deterministic informational retrieval.
- company-local isolated evidence workspaces with immutable registry
  generations, direct read-only reference imports, origin-qualified retrieval,
  and fixed single-workspace lifecycle operations.

Docling is the initial PDF baseline only when version 2.118.1 is installed and
matching benchmark evidence meets the qualification minimums. It has no
unconditional priority. PDF OCR is disabled; scanned or image-only documents
fail the quality gate. Runtime activation of the DOCX adapter remains bound to
a separately retained `hold` decision. The English OCR adapter is available
only through its explicit retained-evidence activation; a Docling DOCX
fallback, live model providers, live candidate promotion, release, publication,
and deployment are not authorized by this foundation.

## English OCR runtime preparation

The English OCR development lane has a prepared, sealed offline runtime for
PaddleOCR 3.7.0, PaddleX 3.7.0, PaddlePaddle GPU 3.3.0, CUDA 12.6, and Python
3.12. Its official artifact set contains 79 locked wheels and eight locked
Paddle model archives. Preparation verifies every origin, size, digest, archive
member, package version, and model byte before publishing a no-replace runtime
tree with read-only files and directories.

The retained runtime proof binds artifact-manifest digest
`sha256:babe5affe2f987bf39105031a9fa13ee832e5df22a5c058b7d849ed0765ab9ab`,
model-inventory digest
`sha256:30150e50d10072a349c8cb11a95728859fbad957c8593a133caad1e39f3d1d03`,
and runtime-tree digest
`sha256:6002a11549b0bcb58e23edb4e98badfc58677f149e1533d6b1d11ec4ffe142bf`.
The tree contains 16,047 files totaling 11,344,260,713 bytes and reopens
idempotently without reinstalling. A bubblewrap proof with an unshared network
namespace passed Paddle's runtime check, executed a GPU tensor, and performed
one inference with a locked orientation model and its locked sample image on
`gpu:0`. The proof retains only a digest of the device UUID.

Runtime preparation alone is compatibility evidence, not OCR activation. The
separate public fixture qualification has now run all 20 fixtures twice for
each of the three sealed candidates. Candidate 1 and candidate 2 passed every
hard gate. Candidate 3 was repeatable but failed fidelity and closed outcome
gates, including 70 hallucinated lines and a non-text false accept. The retained
decision is `candidate_change`, selecting candidate 1 without changing a
threshold. Its exact qualification and selected-result digests are
`sha256:c6ec81fddb9b1db7a5588f12d3c9e64f42732ccbe07f4cb5d6239cb6eec4aa3c`
and
`sha256:13d10316270e925c1a68345e7d864de25c7640751198581a196eaac97f56387d`.

An explicit reviewed activation reopens those exact bytes and exposes only the
`paddle-ocr-english` parser for the `ocr-layout` capability. Candidate 1 has
1.0 character/word/detection/reading-order/page scores, mean polygon IoU
0.827474, zero hallucinated lines, and exact expected outcomes. Docling remains
OCR-disabled and the native DOCX profile is byte-identical.

The fixed `scripts/private-ocr-uat.py` surface exposes only
`prepare-runtime`, `qualify`, `prepare-corpus`, `run`, and `cleanup`, with an
optional `--json`. It accepts no source, root, model, device, backend,
provider, retry, concurrency, or threshold controls, and invalid arguments
load no OCR product module. Qualified single-document PNG/JPEG/PDF ingestion
and v0.3 scanned-PDF batches require the exact activated parser identity,
runtime digest, selected model digest, and qualification digest. They retain
normal candidate, checkpoint, queue, and review semantics while keeping
canonical and promotion authority false. The exact representative campaign
then completed with 1 initial, 3 resumed, and 0 rerun conversions: all four
reviewed PDFs produced candidates, none were rejected, every item calibrated
twice with identical results, and the tuning decision remained `hold`. The
brain digest was identical before work, after work, and after cleanup. Cleanup
retained exactly the validated readback and cleaned-state files, whose byte
digests are
`sha256:c84108676f20b3703dcdc7ada4265e53198027bde2a53fcf883fd33bbebb28ad`
and
`sha256:e8395e18fb970438b33640320646b9192b02bd4e6c719fe862b7da5de947344a`.
The recursive privacy scan, original-source immutability check, and runtime
residue check passed. Neither qualification nor this campaign grants network,
provider, fallback, promotion, release, publication, or deployment authority.

The source-raster boundary is separately implemented for explicitly selected
PDF OCR and reviewed PNG/JPEG inputs. It holds and hashes the source file,
rejects links and identity drift, and invokes PDFium or Pillow only inside a
minimal bubblewrap root with an unshared network namespace. Output is fixed at
300 DPI for PDFs, sRGB, lossless PNG, ordered `page-0001` identifiers, and exact
source, renderer, configuration, dimension, size, and digest bindings. The
transaction enforces 100-page, 25-million per-page pixel, 1-billion document-pixel,
decoded-byte,
aggregate-byte, source-byte, and timeout ceilings; exact incomplete or complete
pre-publication state can recover, while unknown state is preserved and
rejected. These raster bundles are private OCR inputs, not document IR or
activation evidence.

Paddle inference is a third, independent boundary. Before launch the parent
rehashes the complete sealed runtime tree and the selected model pair, validates
the raster bundle, and copies the exact manifest and page bytes into sealed
memory descriptors. Bubblewrap then provides an allowlisted root, unshared
network/PID/IPC/UTS namespaces, exact NVIDIA device and capability nodes,
read-only runtime and source mounts, tmpfs caches, a sealed contract, hard
CPU/address/data/file/descriptor/process limits, and a bounded result mount.
Seccomp denies `fork`, `vfork`, `clone3`, and process-form `clone` while
retaining native threads required by Paddle. Actual adversarial probes deny
TCP, UDP, DNS, host Unix sockets, native child creation, descendants, and
outside writes. Successful stderr is bounded and discarded; Paddle diagnostics,
paths, caches, and model output never become public evidence. The worker emits
only the strict detached observation contract and still grants no activation or
authority.

## Setup and verification

Python 3.11 or newer is recommended. The deterministic core uses only the
standard library.

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install --no-build-isolation --no-deps -e .
make check
```

This uses Ubuntu's installed build backend and does not download packages.
Installation is optional: `make check` runs directly from `src/` through the
repository's configured `PYTHONPATH`.

For born-digital PDF calibration, install the isolated optional extra and
explicitly prepare the non-OCR model cache:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[pdf]' \
  --extra-index-url https://download.pytorch.org/whl/cpu
mkdir -p .ao-lore/cache/{docling,huggingface,torch,xdg} .ao-lore/benchmarks
HF_HOME="$PWD/.ao-lore/cache/huggingface" \
XDG_CACHE_HOME="$PWD/.ao-lore/cache/xdg" \
TORCH_HOME="$PWD/.ao-lore/cache/torch" \
.venv/bin/docling-tools models download layout tableformer \
  --output-dir .ao-lore/cache/docling
AO_LORE_HOME="$PWD/.ao-lore" \
HF_HOME="$PWD/.ao-lore/cache/huggingface" \
DOCLING_ARTIFACTS_PATH="$PWD/.ao-lore/cache/docling" \
XDG_CACHE_HOME="$PWD/.ao-lore/cache/xdg" \
TORCH_HOME="$PWD/.ao-lore/cache/torch" \
.venv/bin/ao-lore benchmark pdf \
  --corpus tests/fixtures/ao_lore/pdf/fixture-spec.json \
  --out .ao-lore/benchmarks/docling-2.118.1.json
.venv/bin/ao-lore benchmark pdf --adapter limited \
  --corpus tests/fixtures/ao_lore/pdf/fixture-spec.json \
  --out .ao-lore/benchmarks/limited-pdf-0.1.0.json
```

The benchmark output is exclusive and ignored. It binds the exact corpus,
configuration, runtime, package version, metrics, exclusions, failures, and
result digest. No OCR model is prepared or invoked.
The shipped `limited` baseline extracts only declared PDF text literals and is
calibrated by the same numerical runner; it is challenger evidence, not a
hard-coded production preference.

See `docs/architecture/ao-lore.md` for formulas, role contracts, sequence
diagrams, and safety boundaries. The progressive retrieval architecture is
recorded in `docs/architecture/progressive-evidence.md`:
verified document retrieval works without a graph, relationship enrichment is
conditional, and candidate or canonical transitions remain explicit. See
`docs/recovery/standalone-migration.md` for the one-time recovery provenance
and the preserved misplaced branches.

## Review public candidate quality

Verify the fixed local campaign without invoking the canonical reader,
appending reviews, or granting promotion authority:

```bash
python3 scripts/rehearse-public-candidate-quality-review.py --check
```

The completed campaign checked 486 claims and citations, retained a
deterministic 96-claim semantic sample, and ran 24 precommitted questions. All
six recommendations are `hold`: bindings remained intact, but the strict
usefulness, duplication, or fragmentation thresholds were not all met. See
`docs/workflows/review-public-candidate-quality.md` before any separate review.

## Operate connected evidence graphs

Evidence graphs are bounded, informational retrieval inputs. Domain packs may
be supplied and reviewed independently, but the generic graph APIs preserve
exact source, claim, relationship, freshness, qualification, contradiction,
and refusal bindings. Graph membership is not canonical knowledge and never
creates legal advice, a candidate review, or promotion authority. See the graph
workflow documentation for the fixed offline inspection and query boundaries.

## Refresh evidence in a workspace

Evidence freshness is evaluated only for one explicitly selected registered
workspace. Use the fixed workspace operation:

```bash
ao-lore workspace refresh --workspace <registered-id> --json
```

The clean baseline is offline by default. The operation validates the selected
workspace, retained policy, and exact source and graph bindings, then returns
one fixed redacted rejection because no live transport is installed. It does
not rewrite acquisition records, publish a graph, or grant candidate,
canonical, provider, publication, release, or deployment authority.

Live refresh requires a separately reviewed trusted domain transport that
allows only globally routable resolved addresses and pins the connected peer
on every redirect hop. Lower-level `AcquisitionDependencies.http` remains a
trusted internal injection point and does not grant public network authority.
Any installed transport must preserve the per-source locator and redirect-host
policy, use no credentials or providers, and never cascade beyond the selected
workspace. See `docs/workflows/refresh-evidence-graph.md` for classifications,
offline replay, and recovery boundaries.

## Operate isolated evidence workspaces

One AO Lore deployment belongs to one company. Within that deployment, each
`reference`, `property`, `matter`, or `operations` workspace owns an isolated
evidence-graph lifecycle beneath the ignored `AO_LORE_HOME` root. A fresh
public checkout exposes an empty registry and creates no workspace state merely
by listing it.

Queries select one primary workspace and only its explicitly declared direct
reference workspaces. Verified document evidence is readable without a graph;
when a selected workspace binds relationship evidence, the same fixed query
adds it without making the graph mandatory. Imports are read-only, one-way,
and non-transitive, and every result preserves its exact originating workspace,
document or graph, source, and evidence identity.
There is no cross-company tenancy, customer fixture data, implicit global
search, candidate or canonical authority, or publication authority.

The fixed query grammar is:

```bash
ao-lore workspace query --workspace <registered-id> --prompt "<text>" --json
```

It exposes no graph-required, root, manifest, URL, provider, model, network,
policy, force, or authority override. The registry is selected before exact
declared document and optional graph generations are opened. Results are local,
read-only, provider-free, network-free, and validated as v0.2 workspace query
readbacks.

The fixed production surface also supports `list`, `inspect`, `refresh`,
`replay`, and `recover`. Rehearse the isolation lifecycle with invented
public-safe evidence in disposable ignored state, then verify retained bytes:

```bash
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-workspaces.py
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-workspaces.py --check
```

Rehearse the governed candidate workflow independently:

```bash
python3 scripts/rehearse-evidence-selection.py
python3 scripts/rehearse-evidence-selection.py --check
```

This fixed-root rehearsal uses one document-only and one combined synthetic
workspace, applies fixture-only false downstream authorization, exercises every
retained apply crash boundary, and verifies direct/check byte identity while
candidate count advances by one and review and brain inventories remain
unchanged.

Compare document-only, graph-only, and combined retrieval using the fixed
synthetic progressive-evidence rehearsal:

```bash
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-progressive-evidence.py
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-progressive-evidence.py --check
```

The comparison records precommitted citation precision, applicability,
conflict, supersession, qualification, refusal, exact evidence identity, and
latency-budget measures. Relationship enrichment succeeds only when it improves
a relationship-sensitive measure without citation-precision or refusal
regression. Graph size is not a success measure.

Rehearse the complete sanitized knowledge lifecycle from document retrieval
through a fixture-authorized disposable canonical readback, then independently
recompute and compare its canonical report bytes:

```bash
env -u PYTHONPATH -u AO_LORE_HOME python3 scripts/rehearse-sanitized-lifecycle.py
env -u PYTHONPATH -u AO_LORE_HOME python3 scripts/rehearse-sanitized-lifecycle.py --check
```

This fixed-root, offline proof uses invented public-safe evidence and writes
only beneath ignored `.ao-lore/sanitized-lifecycle-rehearsal`. It does not
inspect or mutate the default brain, candidates, sources, or company workspace
state. Its synthetic review and authorization grant no real candidate-review,
promotion, provider, GitHub publication, release, deployment, or other
authority. See
[`workflows/rehearse-sanitized-lifecycle.md`](workflows/rehearse-sanitized-lifecycle.md)
for exact interpretation and private-visibility boundaries.

Workspace and graph evidence remains informational and non-canonical; any later
candidate proposal requires a separately governed path. Querying or rehearsing
creates no candidate
or review event, changes no canonical knowledge, and grants no provider,
promotion, publication, deployment, or release authority. See
[`docs/workflows/workspaces.md`](docs/workflows/workspaces.md) for commands,
lifecycle states, result codes, reference rules, backup implications, and
authority boundaries.

## Ingest and review one document

Place one born-digital PDF beneath `sources/`. Production ingestion discovers
the exact qualified Docling 2.118.1 manifest at
`$AO_LORE_HOME/benchmarks/docling-2.118.1.json`, parses with OCR disabled, and
automatically persists the result beneath `working/candidates/`. It never
modifies `brain/`.

```bash
ao-lore ingest --source sources/document.pdf --json
ao-lore candidate list --json
ao-lore candidate inspect --candidate-id <candidate-id>
ao-lore candidate review --candidate-id <candidate-id> \
  --decision accept --reviewer <id>
ao-lore candidate list --status accepted --json
```

The default `candidate list` view is the verified pending/unreviewed queue.
Queue order is `(created_at, candidate_id)`. `--limit` is bounded; `--after`
accepts the last scanned candidate ID as an exclusive cursor, and JSON
`next_after` is non-null only when another page remains. Every bounded
candidate collection is verified in full before any page is returned, so an
unexpected entry, broken provenance binding, or corrupt review chain fails the
entire request closed. There is no database or mutable queue index.

Retrying the same exact document is idempotent and retains the original
provenance `created_at`; contradictory retained content is rejected. Human
output is escaped and intentionally minimal. Use `--json` when stable,
digest-bound automation output is required.

This surface supports one document at a time and born-digital PDF only. OCR,
unqualified DOCX ingestion, candidate promotion, live providers, release,
publication, and deployment are not authorized. An accepted review remains
non-canonical and grants no authority over `brain/`.

## Ingest a bounded PDF batch

Create a repository-contained JSON manifest with this exact outer shape:

```json
{
  "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
  "batch_id": "batch-guides-01",
  "documents": [
    {
      "item_id": "guide-one",
      "source": "sources/guides/guide-one.pdf",
      "source_digest": "sha256:<64 lowercase hex characters>",
      "knowledge_policy": {
        "sensitivity": "public",
        "stale_after": null
      }
    }
  ],
  "continue_on_error": true
}
```

The ordered `documents` array contains 1 through 100 unique entries. Each may
also carry an exact versioned `candidate_context`. A PDF item may opt into an
answerable v0.2 candidate with a closed `knowledge_policy`: `sensitivity` is
`public`, `internal`, or `restricted`, and `stale_after` is either `null` or a
bounded UTC timestamp. Omitting the policy preserves the legacy metadata-only
v0.1 candidate behavior. Sources are immutable,
repository-relative PDFs beneath `sources/`; absolute paths, links, duplicate
IDs or sources, digest drift, and unknown fields fail closed.

```bash
AO_LORE_HOME="$PWD/.ao-lore" \
HF_HOME="$PWD/.ao-lore/cache/huggingface" \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
ao-lore ingest-batch --manifest sources/guides/batch.json --json

ao-lore ingest-batch --manifest sources/guides/batch.json
ao-lore candidate list --json
```

Execution is sequential and always continues after a safely classified
document rejection. The final status is `partial` when any item is rejected,
including when every item is rejected. Stable error codes contain no source
locator or exception text. Durable state is stored beneath
`$AO_LORE_HOME/batches/<batch-id>/` as a digest-chained checkpoint and, when
terminal, a verified final readback. Repeating the identical manifest verifies
candidate bindings and returns the same aggregate without reparsing terminal
items. To retry a rejected or corrected source, use a new batch ID and manifest;
there is no retry or checkpoint override.

Answerable distillation copies only exact non-empty IR block text into claims
and citations. A block longer than the 512-character citation ceiling remains
visible in `claim_mappings` but is not truncated or admitted as answer
evidence. The policy and answerable payload remain non-canonical until review
and a separately authorized promotion.

Batch ingestion uses only the exact qualified Docling 2.118.1 manifest and
contained model cache. It is offline and OCR-disabled; image-only PDFs remain
visible as rejections. Results create only non-canonical candidates and safe
inspection/review commands. Review remains explicit, and even acceptance does
not promote a candidate or mutate `brain/`.

The tracked deterministic corpus includes accepted and expected-rejection PDF
fixtures. Optional private calibration inputs may remain beneath
`$AO_LORE_HOME/calibration/private-pdf/`; only a path-free aggregate of counts,
metrics, exclusions, and stable failure categories may leave that ignored
boundary. Private calibration is not required by the public gate and grants no
authority. When no private corpus is provided, the pure status projection is
`status: not_supplied` with `qualified`, OCR, provider-call, and authority
flags all false; absence is never represented as a passing calibration.

## Private Ubuntu PDF operator UAT

The narrow operator harness uses exactly four reviewed, born-digital public
documents installed by Ubuntu packages: the Shared MIME-info specification, a
printer-driver manual, a CUPS task-information form, and a CUPS test page. It
does not search home directories or projects and accepts no source, root,
parser, OCR, provider, network, concurrency, retry, review, promotion, or
threshold override.

```bash
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py prepare-seed --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py run --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py cleanup --json
```

`prepare-seed` verifies and copies the four inputs into the ignored
`.ao-lore/calibration/private-pdf/` boundary. `run` waits for a verified batch
checkpoint, issues one controlled interruption, resumes the exact manifest,
and reruns the completed batch to require zero new conversions. It verifies
the candidate queue, checks `brain/`, completes verified cleanup, and checks
`brain/` again before assembling any terminal evidence. Cleanup/recovery state
remains under ignored `.ao-lore/uat/private-pdf/`; a successful new run
atomically publishes exactly `private-pdf-uat-readback.json` and
`cleaned-state.json` beneath the distinct ignored
`.ao-lore/evidence/private-pdf-uat/<batch-id>/` root. Actual conversions are
bound to a durable conversion journal. Every ingest worker phase is launched
through the fixed Ubuntu `/usr/bin/bwrap` boundary with a new network
namespace, an empty root populated only by fixed read-only system, Python, and
repository mounts, a private `/tmp`, and only the exact run, batch, and
candidate directories writable. Host runtime/control paths and filesystem
Unix sockets are absent. The sealed model cache is mounted read-only.
Interpreter-declared external `site-packages` roots are separate read-only,
socket-free, identity-bound mounts; ambient user-site discovery is disabled
and only those contract-listed dependency paths are added explicitly.
Bubblewrap availability is probed before the contract is written;
missing or failed isolation starts no worker. The sandbox clears the inherited
environment and supplies only fixed contained cache/home paths, offline flags,
and the repository import path. A Python socket guard remains as a second,
observable defense. This is an automated
controlled-interruption test, not an instruction to send an arbitrary terminal
signal.

Before any worker starts, `run` validates and hashes
the contained qualified Hugging Face cache and creates a random run-owned
sealed copy. Only contained regular-file links are dereferenced; directory or
external links, hard links, special files, overflow, and mutation fail closed.
The copy has no links, uses `0555` directories and `0444` files, and is bound
by root identity, exact tree manifest, digest, file count, and byte count.
Workers set `HF_HOME` to that copy and never consume the original cache after
sealing. Resumable cleanup removes the exact sealed tree before terminal
evidence is retained.

Calibration is not executed synchronously in the operator process. A dedicated
internal worker runs exact Docling 2.118.1 in a second verified bubblewrap
process with the same read-only sealed-cache and unshared-network guarantees.
Its strict run contract binds the manifest and document order, source byte
digests and identities, qualification and configuration digests, cache tree,
output identity, and complete sandbox specification. The corpus is limited to
four inputs, 50 MiB each and 200 MiB total. Each conversion has a worker alarm,
the parent imposes a hard aggregate deadline, and timeout or output/contract
drift terminates the whole process group and yields no trusted readback. The
worker publishes only a bounded atomic aggregate result; the parent verifies
and removes both transient calibration files before continuing.

The exported calibration aggregate contains six content-free groups: metrics,
exclusions, stable failure counts, latency, peak memory, and repeatability.
Script output is narrower still: opaque corpus status and counts only. It
contains no filenames, paths, annotations, extracted text, exception details,
credentials, or provider material. The seed decision is only `hold` or
`investigate`; it never changes qualification thresholds. An exact offline
Docling 2.118.1 run is recorded with canonical retained-readback digest
`sha256:73810919283d1523d0197d66030b57f4f1ceafbd96bb0103317d342bbd4f2026`
and aggregate digest
`sha256:dafeecc62751f2ea165948f9c169a9d31b50580eb7fbeadd55309b2165c4836e`.
It processed four documents successfully with no rejection, interrupted once,
resumed three conversions, performed zero rerun conversions, and selected
`hold`; `brain/` remained unchanged. This path-free runtime evidence grants no
OCR, provider, network, promotion, release, publication, deployment, or authority.
A separately reviewed representative-domain campaign now reuses the same
internal state machine without widening this public command surface. Its
retained readback binds campaign-origin digest
`sha256:aeb6bd753c370f5afbe7556d57a92c986a9b87d7dac83fd056d3bb5468ebc54d`
and aggregate digest
`sha256:4008ffc713e72c7a1c29ad628c84d5b1c8455d6818a7aced96ceb52e0590611d`.
The exact offline run processed four documents successfully, interrupted after
one conversion, resumed three, added zero conversions on rerun, and produced
two identical calibration attempts per document. Fidelity was text `0.5`,
structural `1.0`, and source-location `1.0`, with no exclusions or stable
failures, so the closed decision was `hold`. Source inventory and `brain/`
remained unchanged; transient corpus, staging, candidate, and batch state was
removed. This evidence grants no OCR, provider, network, promotion, release,
publication, deployment, or authority.

`run` verifies and removes its temporary repository staging before terminal
evidence publication. The cleaned-state record cross-binds the readback digest,
batch and corpus identities, and all three equal brain snapshots. Cleanup does
not target the evidence root. After a process loss, rerun the same action so
its digest-bound cleanup-only recovery records can finish before the candidate
and `brain/` baselines are captured; prepared artifacts are revalidated before
any worker launch. Then use `cleanup` to remove only the
verified fixed seed. Cleanup serializes cooperative AO Lore writers and
rechecks every binding; Linux's final unlink syscall still relies on those
cooperative writers not replacing a verified name in that unobservable final
window. An unreviewed candidate remains non-canonical: this harness performs
queue verification, not review or promotion, and grants no provider, network,
release, publication, deployment, or authority.

## Private DOCX operator campaign

The native DOCX path validates the OOXML ZIP package and its relationships
before extracting deterministic document IR. The approved private source set
uses the single declared `restore-ooxml-local-header-v1` transformation: it
restores only the four-byte OOXML local-header signature and binds both the
original and derived identities. It does not invoke OCR or silently fall back
to Docling. The representative policy has 88 accepted documents, 11 expected
active-content rejections, and one expected invalid-package rejection for an
unowned opaque package part; accepted-item
fidelity and complete expected-outcome accuracy are evaluated separately.
Intended-safe auxiliary OOXML roles are admitted only through exact content
types, XML roots, owning internal relationships, and bounded inventory rules;
they are never promoted into extracted document IR. Non-hyperlink external
relationships, orphan parts, executable payloads, and unknown roles remain
invalid.
Representative compatibility is closed to one normalized leading slash on a
package-root target, one exact empty leading `mso-contentType` custom-XML
processing instruction, four role-bound comment content-type aliases, and a
25,165,824-character per-part XML ceiling plus a distinct
29,360,128-character aggregate ceiling. Custom XML item properties may omit
`schemaRefs`; a present container is either exactly empty or contains unique,
nonempty URI-only leaves.
External images accept only
bounded `cid:` relationships bound through `r:link`; their targets are neither
fetched nor retained. Note/numbering relationship parts, glossary
styles-with-effects, EMF thumbnails, and at most three zero bytes after a
JPEG's unique EOI marker use separate exact validators.

The standard-library-only operator accepts exactly three actions and optional
`--json` before or after each action:

```bash
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src python3 scripts/private-docx-uat.py prepare-seed --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src python3 scripts/private-docx-uat.py run --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src python3 scripts/private-docx-uat.py cleanup --json
```

There are no source, root, corpus, parser, threshold, candidate, OCR, provider,
network, retry, concurrency, review, or promotion overrides. `prepare-seed`
loads the already reviewed, fixed four-item derived corpus and prepares its
durable UAT state. `run` performs the controlled one-conversion interruption,
three-conversion resume, zero-conversion rerun, two-attempt calibration, brain
and candidate reconciliation, and verified cleanup. `cleanup` removes only an
exact verified derived corpus if one remains. All phases fail closed on
identity, digest, journal, checkpoint, queue, or recovery drift.

Representative qualification inputs are prepared only through
`prepare_private_docx_qualification_inputs`. That one product call validates
the reviewed 100-item descriptors and expectation, restores only the four-byte
headers into fixed fixture names, and atomically publishes the closed manifest,
expectation, and corpus beneath `private-docx/qualification-inputs`. A durable
fixed intent makes exact partial writes recoverable under the private-DOCX
cooperative-writer lock. The retained `qualification.json` and four-item UAT
`corpus` remain separate; their shared representative expectation digest is
validated before UAT preparation or cleanup. Unknown, partial, linked, or
drifted state is preserved and rejected, and original source bytes are never
deleted or rewritten.
Exact qualification execution enters through
`run_private_docx_qualification`: it invokes that preparation transaction once,
passes a detached input manifest to the adapter factory, reloads and
cross-checks the published expectation and every derived fixture, and only
then runs the benchmark. Execution contains no second/manual input writer and
adds no operator override.

Accepted qualification fixtures derive their text, structural-event, and
count expectations through the bounded independent DOCX expectation oracle,
after package security validation. The oracle does not consume DocumentIR or
native-parser semantic helpers. It independently preserves body order,
hyperlink/note/image segment boundaries, table merges and nesting, and visible
wordprocessing-shape text. Hyperlink identifiers, anchors, and external targets
are validation-only and never enter DocumentIR; a picture with both a valid
embedded payload and a valid external link uses the embedded payload and never
fetches or echoes the link. The reviewed outcome partition remains exactly 88
accepted, 11 active-content rejections, and one invalid-package rejection.

The closed hyperlink grammar also admits bounded validation-only tooltip,
target-frame, uppercase RSID, and reviewed run-presentation metadata. It emits
only visible text: tabs become separators, rendered page breaks are inert, and
balanced nonnested `PAGEREF` fields discard their bounded instruction and
bookmark identifier while retaining the visible result. Picture blips may
carry only the unqualified presentation hint `cstate="print"`, which is also
discarded. The parser and oracle enforce these grammars through independent
state machines; none of the discarded metadata, locators, instructions, or
external targets enters DocumentIR or retained evidence.
An otherwise empty run may be inert only when it contains a present, nonempty,
closed-valid presentation block. It cannot emit text or evidence, change field
state, satisfy field-result visibility, or make an all-formatting hyperlink
valid.

Ingest and calibration workers run beneath the fixed `/usr/bin/bwrap` boundary
with an unshared network namespace, an empty allowlisted root, read-only source
and code mounts, exact writable run state, a private temporary directory,
cleared environment, resource ceilings, and clone/fork denial. Durable intents,
held identities, deterministic quarantines, and fsynced publication make
preparation, worker state, retained evidence, and cleanup resumable after a
crash without accepting foreign state.

Operator output is bounded and path/content/digest-free. It exposes only the
fixed corpus and item identities, lifecycle/count information, the closed
`hold`/`candidate_change`/`investigate` decision, cleanup status, and false
authority fields. The exact offline qualification ran all 100 documents twice:
88 were accepted, 11 produced the expected active-content rejection, and one
produced the expected invalid-package rejection. All four fidelity/outcome
scores and repeatability were 1.0, with no unexpected failures or exclusions,
yielding `hold`. Its result digest is
`sha256:e36b132b49cc08b5815757912c0f77d68b2465095db40559608d878930d594fe`.

The fixed four-item lifecycle then completed with one initial conversion,
three resumed conversions, and zero rerun conversions. Every item calibrated
twice, `brain/` was unchanged before work, after work, and after cleanup, and
cleanup retained exactly the validated readback and cleaned-state files. The
readback and cleaned-state byte digests are respectively
`sha256:a434614f0073de2654f7fb94dd606c380917e038f370617081e2a6b3697ec5bb`
and
`sha256:bdf096d31fe041a341076bb84f5356503269ea5d14305276635dd4c8f9258821`.
The recursive privacy scan and original-source immutability check passed. This
retained `hold` satisfies the representative-evidence activation condition for
the native adapter but grants no promotion or release authority. The separately
qualified English PaddleOCR path does not alter this DOCX result. A Docling DOCX
fallback, any live candidate promotion, release, publication, and deployment
remain separately governed and require their own reviewed evidence and authority.

## Governed candidate promotion

AO Lore implements a dormant fixed-action local promotion surface:

```bash
ao-lore promotion prepare --candidate-id <candidate-id> --out <proposal.json>
ao-lore promotion apply --proposal <proposal.json> --authorization <authorization.json> --json
ao-lore promotion inspect --promotion-id <promotion-id> --json
ao-lore promotion rollback --promotion-id <promotion-id> --authorization <authorization.json> --json
ao-lore promotion recover --json
```

Preparation requires an exactly accepted, digest-bound candidate and produces
a bounded proposal beneath `$AO_LORE_HOME/promotions/proposals/`. Acceptance is
not promotion authority. Apply validates a separately supplied, exact,
expiring, one-shot authorization; publishes one additive immutable generation;
and records immutable consumption, transaction, recovery, and chained audit
evidence. Rollback is a distinct append-only generation and requires its own
separate one-shot authorization. Inspect is read-only, and recover resumes only
an exact retained transaction without inventing authority.

The implementation campaign exercised these controls only against disposable
public-safe fixture candidates and fixture brains. It did not inspect or
promote an existing or private candidate, and the repository canonical
`brain/` remained byte-for-byte unchanged. Implemented controls, accepted
reviews, passing tests, proposals, readbacks, and recovery state grant no
authority to perform a live promotion or rollback. Each such action requires
separate explicit authorization for that single bounded action. Nothing here
grants provider, network, credential, batch, overwrite, unattended,
publication, release, deployment, or authority-advance permission. See
[`workflows/promote-candidate.md`](workflows/promote-candidate.md).

## Canonical knowledge retrieval

AO Lore exposes one fixed, local canonical-reader surface:

```bash
ao-lore knowledge status --json
ao-lore knowledge search --query <text> [--limit <1-200>] --json
ao-lore knowledge answer --query <text> --json
```

The reader opens only the repository-owned `brain/` and
`brain/generations/` roots. It validates every committed generation and
replays additive and restoration transitions to derive one effective active
snapshot. It never reads `sources/`, document IR, `inbox/`, or
`working/candidates/`; in particular, an accepted candidate is still only a
reviewed proposal and is never searched. A candidate becomes canonical only
through a separately authorized promotion generation, and a canonical entry
is active only when it remains in the replayed effective snapshot.

These boundaries keep the lifecycle stages distinct:

| Stage | Meaning | Reader treatment |
| --- | --- | --- |
| Source | Immutable input evidence | Never opened |
| Document IR | Parser output derived from one source | Never opened |
| Candidate | Non-canonical distillation proposal | Never opened |
| Accepted candidate | Reviewed candidate, without promotion authority | Never opened |
| Canonical entry | Immutable entry committed by a generation | Validated during replay |
| Active canonical entry | Canonical entry present in the effective replayed snapshot | Eligible for projection |
| Retrieval hit | Deterministic query match against that snapshot | Metadata or claim projection, not an answer |
| Verified evidence | Active v0.3 claim/citation with exact lineage and policy bindings | Eligible for coverage gates |
| Answer | Evidence-only synthesis after every required gate | `answer`, `partial`, `refuse`, or `investigate` |

Canonical v0.2 entries remain valid replay state but contain metadata only.
Search may label a matching legacy concept `metadata_only`; it never emits a
claim, evidence ID, or citation for that result, and answer never treats it as
support. Canonical v0.3 entries carry exact claims, citations, and immutable
knowledge policy. Restricted v0.3 entries never cross the public search
boundary; eligible public and internal entries remain subject to the later
trust and freshness gates. Search uses deterministic Unicode normalization and
lexical ordering, with no embeddings, vector store, model, provider, network,
cache, or persisted query index. Distinct conflicting or qualifying claims
remain separate; retrieval never merges them into consensus, and a hard
contradiction causes answer refusal.

`status` reports `empty`, `legacy_only`, `mixed_version_partial`, or
`fully_answerable`. `answer` returns the closed outcomes `answer`, `partial`,
`refuse`, or `investigate`, with exact claim, evidence, citation, snapshot, and
coverage bindings when evidence is emitted. Query text is bounded to 4,096
characters and 256 normalized tokens; search returns at most 200 hits; answer
navigation is capped at 200 nodes, eight replans, 60 seconds, and 100,000
estimated tokens; answer text is capped at 32 KiB. Ordinary CLI failures are
reduced to `ao-lore: knowledge operation rejected`.

The implementation campaign exercised promotion, replay, status, search,
answer, refusal/partial outcomes, rollback exclusion, recovery, corruption,
and race cases only against deterministic public-safe disposable fixtures. It
did not open the repository's default candidates or default canonical brain,
perform a real canonical query, execute a live promotion, contact a provider
or network, use credentials, publish, release, deploy, or advance authority.
Real canonical use and every live promotion or rollback remain separately
authorized operator actions. See
[`workflows/answer-from-brain.md`](workflows/answer-from-brain.md).

## Evidence commands

Runtime reports must be written below `.ao-lore/` (or the explicitly set,
repository-contained `AO_LORE_HOME`). Create the desired output directory,
then run:

```bash
mkdir -p .ao-lore/evaluation .ao-lore/monitoring
ao-lore evaluation compare \
  --manifest .ao-lore/evaluation/manifest.json \
  --out .ao-lore/evaluation/comparison.json
ao-lore monitoring evaluate \
  --baseline .ao-lore/monitoring/baseline.json \
  --observation .ao-lore/monitoring/observation.json \
  --out .ao-lore/monitoring/verdict.json

ao-lore candidate persist --result RESULT.json --provenance PROVENANCE.json
ao-lore candidate list --json
ao-lore candidate inspect --candidate-id candidate-0123456789abcdef
ao-lore candidate review --candidate-id candidate-0123456789abcdef \
  --decision accept --reviewer operator-id --rationale "evidence checked"
```

These evidence and candidate commands consume supplied evidence only.
Candidate records remain non-canonical, and an accepted review does not grant
promotion authority. Only a separately authorized promotion action may append
a canonical generation; no command launches providers, publishes, or releases.

## Verification gates

`make check` is the authoritative clean-checkout, offline, non-publishing gate.
It uses only tracked public-safe fixtures, creates required test runtime
ephemerally, and reports unavailable sealed private calibration as explicit
skips.

`make check-extended` is optional local evidence. It first verifies an ignored,
digest-bound `.ao-lore/private-calibration-assets.json` inventory and refuses
with `private calibration assets are not installed` when that inventory is
absent. Passing either gate grants no provider, publication, release,
deployment, candidate-review, promotion, or other authority.

Before any separately authorized private-origin creation, run both forms of the
offline readiness rehearsal:

```bash
python3 scripts/rehearse-private-origin-readiness.py
python3 scripts/rehearse-private-origin-readiness.py --check
```

Each run builds a temporary one-root sanitized repository, transfers only its
`main` commit through a local bare repository, removes the clone's local remote,
validates the canonical pinned dependency bytes, exact installed metadata, and
dependency consistency offline—not package provenance—then executes product, safety, secret, static-analysis, packaging, and
Git-object gates through a disposable supported-Python environment. Hosted CI
and a fresh GitHub clone perform the actual hash-locked install when network
authority is active. Local rehearsal performs no network or GitHub operation
and retains no repository.
The optional `--check` spelling is retained for compatibility and executes this
same read-only rehearsal.
