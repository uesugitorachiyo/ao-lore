# Operate isolated evidence workspaces

## Optional surrounding document evidence

The Python document-query API supports bounded context expansion for retrieval
adapters. Existing callers and the workspace CLI retain their baseline behavior.
For an explicitly selected, validated document generation:

```python
from ao_lore.document_evidence_query import query_workspace_documents

result = query_workspace_documents(
    generation, workspace_id, prompt,
    limit=64, adjacent_blocks=1, context_limit=128, max_context_chars=65536,
)
```

`limit` selects ranked matching blocks. Those seeds are admitted first, followed
by neighboring sibling blocks from the same document and parent. Expansion
stops at headings; heading hits themselves do not expand. At most two siblings
per direction are supported. Source block order defines adjacency, including
across page boundaries within the same section. Added context is a bounded
neighborhood, not a guarantee that every neighboring sentence is relevant.

Overlapping windows deduplicate by document and block identity. Each block
retains its original text, source span, digest, sensitivity, and freshness.
Contradictory text remains verbatim; expansion does not resolve contradictions
or grant candidate or canonical authority. Restrictions and freshness checks on
ranked seeds apply even when the output budget prevents their inclusion.

The expanded output has at most `context_limit` blocks (1–128), and its summed
text length cannot exceed `max_context_chars` (1–1,048,576 Unicode code points).
Blocks are never cut to fit; an oversized seed cannot add context on its own.
If a seed or neighbor is omitted for budget, the readback records a qualification
and returns `partial` instead of `answer`; restriction/freshness outcomes take
precedence. A `partial` result does not assert semantic incompleteness, only
that the requested context window was not fully delivered.

The defaults (`adjacent_blocks=0`, `context_limit=128`, `max_context_chars=65536`)
preserve the original query identity and output. In baseline mode, the original
block-count limit applies without a new text cap. Nondefault expansion/budget
settings are bound into the query identity. Character counts are not model
tokens: adapters must still count the actual formatted context with their
chosen tokenizer and enforce their delivery budget.

## Workspace boundaries

AO Lore uses **workspace** as the operator-facing name for one isolated
evidence-graph lifecycle. One AO Lore deployment belongs to one company and
uses one ignored `AO_LORE_HOME` runtime root. The workspace types are
reference, property, matter, or operations. This boundary organizes
company-local evidence; it is not an account or authorization system.

A fresh public checkout has an empty workspace registry and contains only
product code, contracts, documentation, and synthetic fixtures. Listing that
empty registry is read-only and does not create registry state. Real workspace
definitions and evidence stay beneath the company-owned ignored runtime root;
they are never tracked in the public repository.

## Isolation and shared references

Every workspace has its own source, document, graph, freshness, transaction,
and recovery namespace. An operation selects the registry generation and one
workspace before opening workspace state. Unknown workspaces fail before
unrelated state is opened. Refresh, replay, and recovery affect exactly the
named workspace and never cascade.

A primary workspace may declare reference imports. Imports are direct,
read-only, one-way, and non-transitive. Only workspaces of type `reference` may
be imported. Reference workspaces cannot import another workspace, and a
property, matter, or operations workspace cannot import another private
workspace. There is no default reference, wildcard import, fallback, or
implicit global search.

Queries open the primary workspace and only its declared direct references.
For each selected workspace, dispatch loads the exact document generation named
by the selected registry definition. It loads a graph only when that definition
binds one, seals the validated generations into one in-memory snapshot, then
queries and merges them deterministically. Document-only retrieval therefore
does not require graph construction. Every evidence item retains its originating
workspace together with its document store or graph, source, block, claim or
edge, generation/evidence digest, and evidence identity. Identical local IDs in
different workspaces remain distinct. For relationship evidence, the retained
origin tuple still includes its graph, source, claim or edge, and evidence
digest. Shared or relationship evidence does not override primary evidence:
authority, freshness, qualification, contradiction, and refusal gates continue
to apply.

## Fixed command surface

The production CLI accepts only a bounded registered workspace ID and the
fixed arguments shown here:

```bash
ao-lore workspace list --json
ao-lore workspace inspect --workspace <registered-id> --json
ao-lore workspace query --workspace <registered-id> --prompt "<text>" --json
ao-lore workspace refresh --workspace <registered-id> --json
ao-lore workspace replay --workspace <registered-id> --json
ao-lore workspace recover --workspace <registered-id> --json
ao-lore workspace candidate prepare --workspace <registered-id> --evidence <id> [--evidence <id> ...] --json
ao-lore workspace candidate apply --workspace <registered-id> --proposal-id <id> --authorization-id <id> --json
ao-lore workspace candidate inspect --workspace <registered-id> --proposal-id <id> --json
ao-lore workspace candidate recover --workspace <registered-id> --json
```

Omit `--json` for bounded human-readable status, count, result, and next-command
output. The query grammar remains exactly `workspace query --workspace
<registered-id> --prompt <text> --json`; there is no graph-required switch. The
surface exposes no root, URL, manifest, destination, global-search,
recursive-reference, source-head, clock, timeout, budget, policy, trust,
provider, model, network, failpoint, overwrite, force, concurrency, or
authority override.
There is deliberately no public register, create, delete, backup, or restore
command in this milestone.

The governed candidate surface is fixed-root and identifier-only. `prepare`
persists only one immutable proposal. `apply` may consume only one separately
retained exact authorization and may create only one unreviewed non-canonical
candidate. `inspect` and `recover` may reopen only retained proposal,
authorization, reservation, consumption, transaction, audit, completion,
candidate, and provenance state. The public grammar exposes no root, path, URL,
manifest, query, provider, model, network, policy, force, concurrency,
reviewer, decision, promotion, or authority override.

`list` validates the complete immutable registry chain. A fresh runtime returns
an `empty` status and zero workspaces. `inspect` verifies one graph workspace
binding and its freshness state. `query` returns v0.2 evidence
only from the selected workspace set. Document blocks and graph claims/edges
remain visibly distinct evidence kinds. `refresh` revalidates sources for one
workspace; `replay` reopens its graph and freshness history; `recover` examines
only its owned transaction state.

Workspace lifecycle status is exactly `active`, `inactive`, or `investigate`.
Only an active primary and active declared references can be queried. An
inactive workspace is retained but unavailable for operation. `investigate`
means drift, ambiguity, or unsafe retained state requires operator review; it
is never silently repaired or deleted.

Query outcomes are `answer`, `partial`, `refuse`, or `investigate`. Operation
readbacks use bounded success, inactive, or investigate statuses. The stable
reason codes are:

- `ok`
- `workspace_unknown`
- `workspace_inactive`
- `registry_invalid`
- `identity_collision`
- `reference_not_declared`
- `reference_cycle`
- `workspace_binding_drift`
- `freshness_investigation_required`
- `recovery_pending`

Ordinary command failures print only `ao-lore: workspace operation rejected`
to stderr and exit with status 2. Public readbacks never echo filesystem paths,
source content, prompts, customer labels, exception text, or private locators.

## Offline public-safe rehearsal

Generate the disposable synthetic registry and exercise its complete lifecycle
without network access:

```bash
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-workspaces.py
```

Verify the retained rehearsal without changing it:

```bash
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-workspaces.py --check
```

The rehearsal creates exactly three invented workspaces:
`shared-reference-fixture`, `primary-fixture-a`, and `secondary-fixture-b`.
Workspace A directly imports the shared reference. Workspace B remains absent
from A's reads, locks, results, and recovery. Direct and `--check` runs must be
byte-identical. The rehearsal is offline, provider-free, credential-free, and
uses only disposable ignored state; it is not a template for customer data.
Workspace rehearsal may read bounded metadata/inventory for protected tracked
roots to prove they remain unchanged; opaque external/private roots are never
walked/opened.

Run the progressive comparison independently to verify that document-only
retrieval remains useful while optional relationship evidence must earn its
place:

```bash
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-progressive-evidence.py
env -u PYTHONPATH /usr/bin/python3 scripts/rehearse-progressive-evidence.py --check
```

This fixed-root rehearsal uses only synthetic document-only, graph-only, and
combined snapshots. It denies network access, preserves unrelated workspace
inventory, and requires direct/check byte identity. Its precommitted measures
are citation precision, applicability, conflict detection, supersession
handling, qualification, refusal correctness, exact evidence identities, and a
latency budget. Enrichment passes only when a relationship-sensitive measure
improves without citation-precision or refusal regression. It never scores
graph size.

Rehearse the governed candidate workflow independently:

```bash
python3 scripts/rehearse-evidence-selection.py
python3 scripts/rehearse-evidence-selection.py --check
```

This rehearsal uses one document-only and one combined synthetic workspace,
applies fixture-only false downstream authorization, exercises every retained
apply crash boundary, and verifies direct/check byte identity while candidate
count advances by one and review and brain inventories remain unchanged.

## Backup and later knowledge flow

Workspace registry generations and workspace state are jointly bound. Any
operator-managed backup must capture the complete registry chain and every
corresponding selected workspace namespace coherently beneath `AO_LORE_HOME`.
Backing up or restoring only a graph, only a registry generation, or a mixture
of generations produces binding drift and must fail closed. AO Lore does not
yet provide a public backup or restore command; copying live state while a
writer is active is not a supported recovery procedure.

Workspace document and graph evidence remains informational and non-canonical.
A separately governed candidate path may propose exact selected evidence as a
non-canonical candidate. That transition must preserve workspace and evidence
origins and use separate review and promotion controls. Listing, querying,
refreshing, replaying, recovering, rehearsing, or backing up a workspace creates
no candidate or review event, changes no canonical knowledge, and grants no
provider, promotion, publication, deployment, release, or other authority.

## Explicit exclusions

This workspace surface adds no multi-company tenancy, tenant IDs, accounts,
authentication, roles, ACLs, or cross-company routing. Public fixtures contain
no customer data, customer names, addresses, matter labels, account IDs, or
private pilot evidence. There is no implicit global search and no candidate or
canonical authority. There is no publication, release, or deployment authority.
The implementation rehearsal performs no live acquisition or refresh, provider
call, credential use, real canonical query, candidate action, promotion, or
rollback. Passing the rehearsal or local gates changes none of those authority
boundaries.
