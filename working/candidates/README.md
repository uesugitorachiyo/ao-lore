# Non-canonical candidates

`ao-lore candidate persist` stores validated records as:

```text
working/candidates/<candidate-id>/
  candidate.json
  provenance.json
  reviews/000001-<event-digest>.json
```

Candidate and provenance files are immutable. Reviews are exclusive,
append-only, sequential, and digest-chained; inspection verifies the complete
chain before projecting `unreviewed`, `accepted`, or `rejected`.

`ao-lore ingest --source sources/document.pdf --json` automatically creates or
idempotently reopens one candidate. An exact retry retains provenance
`created_at`; conflicting retained bytes or bindings fail closed. Review it
through the verified queue:

```bash
ao-lore candidate list --json
ao-lore candidate inspect --candidate-id <candidate-id>
ao-lore candidate review --candidate-id <candidate-id> \
  --decision accept --reviewer <id>
ao-lore candidate list --status accepted --json
```

The default list is pending/unreviewed, ordered by
`(created_at, candidate_id)`. `--after` is exclusive and uses the preceding
page's `next_after`. The whole bounded candidate collection is verified before
any page is returned; corruption fails closed. This is a filesystem projection,
not a database or mutable index. Use escaped human output for inspection and
`--json` for stable automation.

Generated candidate contents are ignored by Git and may contain private source
derivatives. Do not commit or publish them. Every candidate remains
`canonical:false` with `promotion_authority:false`; acceptance records human
review only and never grants authority to modify `brain/`.
The current ingestion scope is one born-digital PDF with OCR disabled. DOCX,
OCR/layout recovery, promotion, providers, publication, release, and deployment
are not implemented or authorized by this directory.
