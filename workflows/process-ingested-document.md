# Workflow: Process an Ingested Document

## Purpose

Ingest one contained born-digital PDF, or an ordered bounded manifest of them,
produce quality-approved canonical document IR, and automatically persist
reviewable, non-canonical OKF candidates without changing `brain/`.

## Preconditions

1. Copy the source to a regular, non-link `.pdf` file beneath `sources/`.
2. Prepare the exact qualified Docling 2.118.1 manifest at
   `$AO_LORE_HOME/benchmarks/docling-2.118.1.json` and its local non-OCR model
   artifacts. Scanned or image-only PDFs are outside this workflow.
3. Keep the source within the configured hard byte/page/block bounds.

## Ingest

```bash
ao-lore ingest --source sources/document.pdf --json
```

The command parses exactly one PDF with OCR disabled, validates the selection
and parse-quality results, distills from document IR only, and atomically
persists the candidate and provenance. It returns digests, review status, and
safe next commands, not candidate content. An exact retry reports unchanged
and retains the original provenance `created_at`; any conflict fails closed.

For 1 through 100 documents, create a strict
`ao.lore.ingest-batch-manifest.v0.1` JSON file. Give every ordered entry a
unique `item_id`, a repository-relative `sources/...pdf` locator, and the exact
`sha256:` digest; set `continue_on_error` to `true`. Optional candidate context
must be inline and satisfy its exact versioned contract.

```bash
AO_LORE_HOME="$PWD/.ao-lore" \
HF_HOME="$PWD/.ao-lore/cache/huggingface" \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
ao-lore ingest-batch --manifest sources/batch.json --json
```

The batch runs sequentially. A safe document failure is recorded with a stable
public error code and later documents continue, producing a `partial` final
readback. Checkpoint and final state remain beneath
`$AO_LORE_HOME/batches/<batch-id>/`. Re-running the exact manifest verifies the
checkpoint and candidates, skips terminal items, and returns the same result.
Corrected or intentionally retried input requires a new manifest and batch ID.
Do not edit checkpoints or use them as approval evidence.

## Exercise the private Ubuntu operator UAT

For local operational evidence, use the fixed reviewed Ubuntu seed rather than
supplying an arbitrary document path:

```bash
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py prepare-seed --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py run --json
AO_LORE_HOME="$PWD/.ao-lore" PYTHONPATH=src \
  python3 scripts/private-pdf-uat.py cleanup --json
```

The seed is exactly four born-digital public Ubuntu package documents: the
Shared MIME-info specification, a printer-driver manual, a CUPS task form, and
a CUPS test page. Intake verifies them before retaining bytes beneath ignored
`.ao-lore/calibration/private-pdf/` state. The command has no source/root,
parser, OCR, provider/network, concurrency/retry, review/promotion, or threshold
override.

`run` automatically waits for a verified checkpoint, sends one controlled
interrupt, resumes the exact manifest, then reruns the terminal batch and
requires zero new conversions. Do not manually interrupt it as part of the
procedure. The actual conversion journal, full-lifetime socket guard,
candidate-queue verification, and before/after `brain/` digest must all agree.
After product work, `run` verifies the brain baseline, completes exact cleanup,
and verifies the brain baseline again. Only then does it assemble terminal
evidence. Cleanup/recovery detail remains below ignored
`.ao-lore/uat/private-pdf/` state. New terminal evidence is atomically retained
as exactly a readback and cross-bound cleaned-state record below
`.ao-lore/evidence/private-pdf-uat/<batch-id>/`; cleanup never targets that
root. The public aggregate is limited to metrics, exclusions, stable failure counts,
latency, peak memory, and repeatability, and the script prints only opaque
status/count summaries.

The run also seals the already-contained qualified model cache beneath its
random UAT state before starting a worker. Worker and calibration `HF_HOME`
points only to this immutable, digest-bound copy. Cache intake, copy, and
before/after verification are bounded and offline; any drift or incomplete
resumable sealed-cache cleanup leaves the run non-terminal with no new retained
evidence.

The run verifies and removes temporary repository staging and isolated UAT
product state before terminal publication. A crash or drift before the second
brain verification leaves no new terminal evidence. If the process is lost,
repeat `run` to complete digest-bound recovery before `cleanup`. Cleanup removes only the verified fixed
seed, serializes cooperative AO Lore writers, and rechecks bindings. Its final
unlink relies on a cooperative same-UID writer not swapping a verified name in
the syscall's unobservable final window; drift otherwise fails closed.

The seed decision is closed to `hold|investigate` and never edits thresholds.
A representative-domain corpus requires a later separately reviewed
runtime-only replacement. Until the exact non-skipped execution is recorded,
real-corpus UAT and private calibration remain pending. This harness verifies
unreviewed candidates but does not review or promote them and authorizes no
provider, network, publication, release, deployment, or authority advance.

## Distillation boundary

1. Give the distiller only canonical document IR and explicitly selected
   candidate comparison context.
2. Reject source paths, source bytes, format-specific parser objects, or
   unvalidated block references.
3. Produce candidate concepts, claim-to-block mappings, proposed links,
   contradiction warnings, and a reasoning-free trace.
4. Keep `canonical: false` and `promotion_authority: false` on the result.
5. Bind provenance from the accepted parse and exact distillation result.
6. Let `ao-lore ingest` persist the candidate automatically; do not write
   candidate files directly.

## Review and promotion

Distillation never promotes knowledge. Follow `workflows/review-candidates.md`
for inspection and append-only review. Promotion controls are outside this
implemented workflow and require separate authority; a quality score, model
output, evaluation report, candidate file, or accepted review is not promotion
authority.

## Failure behavior

Fail closed on containment or link violations, size/media/version or benchmark
drift, invalid IR, missing provenance, unknown blocks, malformed candidate
output, retained-content conflict, or attempted authority widening. Do not
reopen the source to repair distillation. OCR, DOCX, providers, promotion,
publication, release, and deployment are outside this workflow.

The public fixture corpus includes expected rejected documents, including
image-only PDFs, to prove the offline OCR-disabled boundary. Optional private
PDF calibration stays below `$AO_LORE_HOME/calibration/private-pdf/` and may
produce only path-free aggregate counts and metrics; it is not a prerequisite
or an authority signal. Absence is projected explicitly as `not_supplied` with
qualification, OCR, provider-call, and authority flags false.
