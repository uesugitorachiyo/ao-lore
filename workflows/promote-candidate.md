# Workflow: Governed Candidate Promotion

This workflow describes the implemented local controls. It is not authority to
run them against an existing candidate or the canonical repository brain.

## Authority and safety preconditions

1. The candidate is already verified and its latest review projection is
   exactly `accepted`. Acceptance is necessary but is not promotion authority.
2. The operator has separately authorized proposal preparation for the exact
   candidate. Preparation itself does not authorize apply.
3. Any live apply has its own out-of-band, strict, expiring, one-shot
   authorization bound to the exact proposal, source head, brain inventory,
   prior and result generations, and write set.
4. Any rollback has a different one-shot authorization bound to the exact
   rollback proposal and current canonical state. Apply authority cannot be
   reused for rollback.
5. No provider, network, credential, publication, release, deployment, batch,
   overwrite, unattended, or authority-advance permission is implied.

## Prepare and inspect the proposal

Choose an output beneath the fixed
`$AO_LORE_HOME/promotions/proposals/` directory:

```bash
ao-lore promotion prepare \
  --candidate-id <candidate-id> \
  --out <proposal.json>
```

Preparation verifies the contained candidate, provenance, complete review
chain, contradictions, source head, whole-brain inventory, latest generation,
canonical identity collisions, deterministic entry, and exact additive write
set. It writes an exclusive proposal and bounded audit evidence. Do not edit
the proposal.

## Apply one explicitly authorized proposal

Authorization is supplied out of band; AO Lore validates its structure and
exact bindings but does not claim to authenticate the opaque operator identity.

```bash
ao-lore promotion apply \
  --proposal <proposal.json> \
  --authorization <apply-authorization.json> \
  --json
```

Apply revalidates every binding under the global promotion lock, reserves the
authorization before canonical publication, and appends one immutable
generation without overwriting prior knowledge. A successful readback is not
publication, release, deployment, or authority for another action.

Verify the independently derived state:

```bash
ao-lore promotion inspect --promotion-id <promotion-id> --json
```

If inspection reports recovery is required, preserve all evidence and run only:

```bash
ao-lore promotion recover --json
```

Recovery resumes an exact retained authorized transaction or returns a bounded
investigation result. It never grants or expands authority.

## Separately authorized rollback

Rollback is an append-only restoration transition, not deletion. Obtain a new
authorization for the exact rollback action, then run:

```bash
ao-lore promotion rollback \
  --promotion-id <promotion-id> \
  --authorization <rollback-authorization.json> \
  --json
ao-lore promotion inspect --promotion-id <promotion-id> --json
```

Never edit or delete the original generation, candidate, proposal,
authorization-consumption, transaction, recovery, or audit evidence.

## Implementation campaign status

The candidate-promotion-controls campaign invoked these actions only through
internal seams against disposable public-safe fixture candidates and fixture
brains. It did not inspect or promote any existing or private candidate. The
canonical repository `brain/` remained byte-for-byte unchanged. Therefore the
campaign proves the controls, not permission to perform a live promotion or
rollback.
