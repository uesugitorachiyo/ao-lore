# Rehearse the sanitized knowledge lifecycle

This fixed rehearsal proves that AO Lore can take related synthetic documents
through verified retrieval, optional graph evidence, governed selection,
candidate review, separately authorized promotion, and canonical readback. It
uses synthetic fixture authority only. It is an offline product proof, not an
operator action on company knowledge.

Run the direct rehearsal once, then recompute it independently and compare the
canonical report bytes:

```bash
env -u PYTHONPATH -u AO_LORE_HOME python3 scripts/rehearse-sanitized-lifecycle.py
env -u PYTHONPATH -u AO_LORE_HOME python3 scripts/rehearse-sanitized-lifecycle.py --check
```

The CLI accepts no arguments other than the optional `--check`. Direct mode
creates or replays one owned run beneath the ignored fixed root
`.ao-lore/sanitized-lifecycle-rehearsal/retained` and emits its validated
canonical report. Check mode creates a fresh disposable sibling run, executes
the same lifecycle, compares canonical report bytes, removes that sibling, and
does not rewrite the retained report. A mismatch or unsafe retained state fails
closed.

All fixture evidence is invented and public-safe. The rehearsal mutates only a
disposable brain under its owned ignored run. It does not inspect or change the
repository's default `brain/`, `working/candidates/`, `sources/`, workspace
registry, customer state, or private pilot material. Network and provider use
remain denied, and the report contains no filesystem paths or credentials.

The report is evidence of deterministic local mechanics only. It does not
authorize real candidate review. It does not authorize real promotion. It does
not authorize GitHub publication, release, deployment, credential use, or any
other authority advance. Any future GitHub repository must remain private
visibility only until the operator separately approves a publication action;
passing this rehearsal does not supply that approval.

Interpret the stages narrowly:

- Document retrieval before graph construction proves that verified ingested
  evidence remains informationally readable without becoming canonical.
- Optional graph retrieval proves that explicit relationships can enrich the
  same evidence without becoming mandatory or authoritative.
- Governed selection creates a non-canonical candidate; it is not review or
  promotion.
- The accepted synthetic review is fixture evidence only and grants no live
  authority.
- The synthetic authorization is exact, expiring, one-shot, and confined to
  the disposable run.
- Canonical status, search, and answer read only the disposable promoted
  generation, never the real repository brain.
