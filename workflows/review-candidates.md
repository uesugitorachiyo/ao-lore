# Workflow: Review Non-Canonical Candidates

## Preconditions

1. The production parse decision is `accept` and its selection and quality
   reports are retained.
2. `build_candidate_provenance()` binds the exact source, document IR, parser,
   reports, distillation trace, candidate identity, and candidate digest.
3. `ao-lore candidate persist` returned `created` or exact-content `unchanged`.

## Inspect and review

```bash
ao-lore candidate list --json
ao-lore candidate inspect --candidate-id <candidate-id>
ao-lore candidate review --candidate-id <candidate-id> \
  --decision accept --reviewer <reviewer-id> --rationale "<concise reason>"
ao-lore candidate list --status accepted --json
```

The default list is the verified pending/unreviewed queue. Use `--status`
with `accepted`, `rejected`, or `all`; `pending` and `unreviewed` select the
same state. Ordering is `(created_at, candidate_id)`. Paginate with bounded
`--limit` and the prior JSON `next_after` value as an exclusive `--after`
cursor. The implementation verifies the entire bounded collection before
returning a page. Any unexpected file, symlink, identity or digest mismatch,
or broken review chain fails the request closed. There is no database or
mutable queue index.

Human list output is escaped and content-minimized. Prefer `--json` for stable
automation and cursor handling; it still contains metadata and digests only,
not candidate content.

Use `reject` when provenance, claims, structure, or contradictions are not
acceptable. A later review may supersede an earlier decision only by appending
a new event. Never rename, edit, replace, or delete candidate, provenance, or
review-event files.

Inspection verifies candidate/provenance bindings, every event self-digest,
contiguous sequence numbers, filenames, and the previous-event chain. Any
failure is a stop condition; retain the state for investigation.

## Authority boundary

`accepted` is a review projection only. Candidate content remains
`canonical:false` and `promotion_authority:false`. This review workflow must
not modify `brain/`, publish, release, deploy, or infer authority from a passing
benchmark, Mission readback, or review decision. If a separately authorized
promotion is intended, stop this workflow and follow
[`promote-candidate.md`](promote-candidate.md); acceptance alone is never
sufficient. OCR, DOCX ingestion, and provider execution are also outside this
workflow.
