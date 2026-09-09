# Workflow: Retrieve Canonical Knowledge

## Purpose and authority

Use the fixed local reader to report, retrieve, or answer from the effective
canonical generation snapshot. The workflow is read-only, deterministic,
provider-free, and network-free. It does not authorize a real canonical query,
promotion, rollback, release, publication, deployment, credential use, or any
authority advance; obtain separate operator authority before real use.

## Keep every stage distinct

1. A source is immutable input evidence.
2. Document IR is validated parser output derived from a source.
3. A candidate is non-canonical knowledge proposed by IR-only distillation.
4. An accepted candidate is reviewed, but acceptance is not promotion
   authority and the canonical reader never searches it.
5. A canonical entry is an immutable result of a separately authorized
   generation transition.
6. An active canonical entry is present in the effective snapshot after exact
   replay of every additive and restoration generation.
7. A retrieval hit is a deterministic match; it is not yet verified evidence
   or an answer.
8. Verified evidence is an active v0.3 claim/citation projection with exact
   lineage, freshness, trust, provenance, and policy bindings.
9. An answer is synthesized only from the verified evidence ledger after all
   deterministic coverage gates pass.

Never substitute `sources/`, document IR, `inbox/`,
`working/candidates/`, accepted reviews, inactive historical generations, or
ad hoc files under `brain/` for the effective canonical snapshot.

## Fixed commands

Check snapshot compatibility before retrieval:

```bash
ao-lore knowledge status --json
```

The answerability status is `empty`, `legacy_only`,
`mixed_version_partial`, or `fully_answerable`. Canonical v0.2 entries are
valid metadata-only history. They may produce a search result classified as
`metadata_only`, but never a claim, evidence ID, citation, or answer support.
Only eligible active canonical v0.3 claims are answerable. Restricted entries
never cross the public search boundary; public and internal entries remain
subject to the answer-time trust and freshness gates.

Retrieve deterministic lexical projections:

```bash
ao-lore knowledge search --query <text> --json
ao-lore knowledge search --query <text> --limit <1-200> --json
```

Search rebuilds an in-memory projection from the descriptor-coherent snapshot.
It uses fixed Unicode normalization, lexical overlap, and stable identity
tie-breaks. It has no embeddings, vector or graph database, model reranker,
provider, network, or persistent query index. Distinct contradictory or
qualifying claims remain separate hits and must not be collapsed into a
consensus.

Request a gated evidence-only answer:

```bash
ao-lore knowledge answer --query <text> --json
```

The only outcomes are:

- `answer`: every mandatory evidence, citation, provenance, freshness, trust,
  contradiction, coverage, and deterministic-validation gate passed;
- `partial`: usable verified evidence exists, but at least one sufficiency gate
  did not pass;
- `refuse`: no answerable evidence exists, only legacy metadata exists,
  coverage is insufficient, trust fails, or a hard contradiction exists; and
- `investigate`: canonical state is invalid, unsupported, or changed during
  the coherent read.

An `answer` or `partial` must bind exact claim IDs, evidence IDs, canonical
citations, snapshot digest, and coverage-report digest. `refuse` and
`investigate` emit no claim text or supporting IDs. The query is capped at
4,096 characters and 256 normalized tokens; answer traversal is capped at 200
nodes, eight replans, 60 seconds, and 100,000 estimated tokens; answer text is
capped at 32 KiB. Ordinary failures expose only
`ao-lore: knowledge operation rejected`.

## Campaign and operational boundary

The implementation rehearsal used deterministic public-safe disposable source,
IR, candidate, authorization, generation, and query fixtures. It exercised
mixed-version replay, search, answer, partial/refusal/investigation, rollback
exclusion, recovery, corruption, concurrent readers/writers, and exact rerun.
It did not inspect the default candidate store or canonical brain, execute a
real canonical query or live promotion, contact a provider or network, use
credentials, publish, release, deploy, or advance authority. Do not interpret
fixture completion, passing gates, an accepted review, or a search/answer
readback as permission for any of those actions.
