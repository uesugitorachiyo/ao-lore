# AO Lore Agent Instructions

## Repository boundary

AO Lore is a standalone Ubuntu repository rooted at the current Git toplevel.
All source, schemas, fixtures, tests,
documentation, benchmark/evaluation/monitoring code, and generated runtime
state belong beneath this root.

AO Mission and the AO Stack may orchestrate AO Lore development as an external
control plane. Their source-owned mission records, authorizations, workgraphs,
run evidence, and readbacks remain in their own state roots. They must not own
or duplicate AO Lore product code, schemas, tests, runtime state, or product
reports. AO Lore remains independently buildable and testable: sibling
repositories are never runtime, build, test, package, or schema dependencies.
Do not contact or synchronize with the Mac host.

## Product boundaries

- `brain/` is the canonical OKF v0.2 knowledge boundary.
- `sources/` is immutable source evidence.
- `inbox/` is unprocessed input.
- `working/candidates/` is non-canonical proposed knowledge.
- Promotion is an explicit additive generation transition. Review acceptance,
  proposal preparation, inspection, recovery evidence, and passing gates are
  not promotion authority.
- Parsing emits canonical document IR and never creates or promotes knowledge.
- Distillation consumes document IR, never reopens source formats, and emits
  candidates only.
- Answerable candidate generation requires an explicit closed per-document
  knowledge policy. It copies exact bounded IR block text only; it must never
  infer, summarize, or truncate evidence into claims or citations.
- Navigation optimizes explicit evidence coverage under hard budgets.
- Synthesis consumes verified evidence only and cannot browse or invent facts.
- Canonical status, search, and answer read only the descriptor-coherent
  effective snapshot replayed from fixed `brain/generations` roots. They never
  open sources, document IR, candidates, accepted reviews, or ad hoc brain
  files. Accepted candidates are never searchable.
- Preserve the distinction between immutable source, document IR,
  non-canonical candidate, accepted candidate, canonical history, active
  canonical snapshot, retrieval hit, verified evidence, and gated answer.
- Evidence-graph membership is informational retrieval state, not candidate
  review, acceptance, or canonical authority. Preserve explicit authority roles,
  source currency, conflicts, qualifications, and exact excerpt bindings.
- Verified document retrieval is the default RAG-replacement capability and must
  not require a graph, candidate, accepted review, or canonical entry. Add graph
  relationships only when direct evidence supports them and they materially
  improve retrieval, applicability, conflict, or supersession handling.
- Candidate creation is an explicit governed selection from verified document
  evidence, verified graph evidence, or both. It is never an automatic side
  effect of ingestion, graph construction, retrieval, or answering, and it never
  grants review acceptance or promotion authority.
- Workspace governed candidate operations are fixed-root and identifier-only.
  Preparation may persist only an immutable proposal. Apply may consume only a
  separately retained exact authorization and may create only one unreviewed
  non-canonical candidate. Inspect and recover may reopen only retained
  proposal, authorization, reservation, transaction, audit, completion,
  candidate, and provenance state.
- One AO Lore deployment belongs to one company. Workspaces isolate company-local
  evidence lifecycles; they do not add tenant, account, user, role, login, ACL,
  customer-routing, or cross-company authority concepts.
- Workspace reference imports are direct, read-only, one-way, and non-transitive.
  Select the registry generation and exact primary workspace before opening
  state; never scan, fall back to, mutate, refresh, replay, or recover an
  undeclared sibling workspace.
- Every workspace query evidence identity must preserve its originating
  workspace, graph, source, claim or edge, and evidence digest. A bare local ID
  is never sufficient and shared references never override existing authority,
  freshness, qualification, or contradiction gates.
- Selected `workspace refresh` is the only operation that may use the network.
  The clean core has no default HTTP transport, so refresh is unavailable
  unless a trusted private deployment transport is installed. Any such
  transport must start at the policy-bound locator and follow only validated
  explicit redirect hosts or peer policy. The public surface exposes no public
  URL, host, root, network-policy, force, or authority controls. All other
  operations are offline.
- Legacy canonical v0.2 entries are metadata-only and never answer evidence.
  Only active v0.3 claims and citations may enter the evidence ledger.
- Never render restricted v0.3 claims or citations through public search.
  Eligible public and internal evidence must still pass derived trust and
  freshness gates before answering.
- Retrieval is deterministic, local, read-only, provider-free, network-free,
  and non-persistent. Preserve distinct contradictions; never collapse them
  into consensus or bypass the hard contradiction gate.
- Parser, distiller, navigator, and synthesizer roles have independent
  configuration, privacy, provider, cache, fallback, and budget boundaries.

## Runtime state and external authority

Use `AO_LORE_HOME` for AO Lore configurable state. It defaults to the
repository-local `.ao-lore/` directory, which must remain ignored by Git. AO
Lore runtime must not use `AO_MISSION_HOME` or `.ao-mission`; AO Mission may
separately use `AO_MISSION_HOME` for its external control-plane ledger.

Fresh public checkouts expose an empty workspace registry without creating
state. Real registry generations and workspace evidence remain beneath the
ignored company-owned runtime root. Tracked workspace fixtures must be
synthetic, public-safe, disposable, offline, and free of customer or pilot data.

Live providers, credentials, publication, release, deployment, and external
side effects require separate explicit operator authority. Never persist
tokens, provider transcripts, private chain-of-thought, or private source
content in tracked files.

Live promotion and rollback each require a separate exact, expiring, one-shot
authorization bound to one proposal, source head, brain inventory, generation,
and write set. Never infer it from acceptance or reuse one operation's
authorization for another. The public surface must retain fixed roots and must
not expose root, force, network, batch, overwrite, unattended, or authority
bypasses.

Real canonical queries also require separate operator authority. Fixture-only
reader rehearsal and passing tests must not inspect the default brain or
candidate contents and do not authorize real use, live promotion/rollback,
provider or network activity, release, publication, deployment, or any
authority advance.

## Working method

- Start from contracts and fail closed on invalid schema, containment, size,
  duplicate-key, identity, or digest input.
- Keep parser-selection prediction separate from actual parse-quality results.
- Keep benchmark and evaluation paths outside production ingestion.
- Bound parser fallbacks and record every decision.
- Preserve evidence, provenance, coverage history, and exact model-role traces
  without recording private reasoning.
- Never silently promote `working/candidates/` into `brain/`.
- Promotion development, tests, and rehearsals must use disposable public-safe
  roots unless a separate explicit live authorization names the exact action.
  Do not inspect generated candidates or mutate canonical `brain/` merely to
  prove the controls.
- Keep recovered sibling branches unchanged until the standalone repository is
  verified; cleanup is a separate operator decision.
- When AO Mission orchestrates work, preserve its exact mission, correlation,
  source-head, route, checkpoint, and artifact-digest bindings. Mission
  readback never grants product mutation or release authority.
- Canonical reader tests and rehearsals use only deterministic public-safe
  disposable roots through private test seams. Production commands keep fixed
  repository roots and expose no root, provider, network, cache, model,
  policy, trust, clock, budget, force, or authority-bypass controls.
- Evidence-freshness rehearsals use only deterministic public-safe disposable
  roots, must refuse canonical or unowned `brain/`, candidate, and source
  roots, and must never perform live refresh or any other real network access.
- Candidate-quality evidence is evaluation only. Keep detailed public text and
  rationale under ignored `.ao-lore/` runtime state, expose only bounded
  digest/count summaries, and never translate `pass|hold|reject` into review
  events or promotion authority.
- Connected evidence bundles use repository-owned ignored roots and expose no
  public root, URL, manifest, destination, network-policy, failpoint, source-head,
  budget, force, or authority override. Errors and public readbacks must remain
  bounded and path-free.
- Workspace refresh, replay, and recovery operate on exactly one validated
  workspace and never cascade to references. Preserve unknown or investigate
  state, keep registry and workspace backups coherent, and treat every
  workspace result as informational rather than candidate, canonical,
  publication, release, deployment, or authority evidence.
- Workspace query dispatch selects the registry before opening selected state,
  loads each exact declared document generation, and loads a graph only when
  the selected definition binds one. Keep the public query grammar fixed and
  validate the sealed v0.2 readback before output.
- Progressive-evidence rehearsal uses only synthetic document-only, graph-only,
  and combined snapshots, denies network access, preserves unrelated workspace
  inventory, and compares precommitted retrieval measures rather than graph
  size. Passing it creates no candidate, review, canonical, provider,
  promotion, publication, deployment, or release authority.
- Sanitized-lifecycle rehearsal is fixed-root beneath ignored
  `.ao-lore/sanitized-lifecycle-rehearsal` and accepts only optional `--check`.
  Use synthetic fixture authority and a disposable brain only. Check mode must
  recompute in a fresh owned sibling, compare canonical report bytes, remove the
  sibling, and never rewrite retained evidence. Passing grants no real review,
  promotion, GitHub publication, release, deployment, or authority advance;
  any future GitHub visibility remains private unless separately authorized.

## Verification

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s tests -p 'test_ao_lore_promotion*.py' -v
python3 -m compileall -q src tests
python3 -m ao_lore.selfcheck
git diff --check
```

Use `make check` to run the complete local non-publishing gate. It is
clean-checkout safe and treats unavailable sealed private calibration as an
explicit skip. Use `make check-extended` only when the ignored, digest-bound
private calibration manifest has been intentionally provisioned; refusal due
to an absent manifest is not a clean-gate failure and never authorizes copying
private assets into source.
Run `python3 scripts/rehearse-private-origin-readiness.py --check` for the
offline one-root/bare-remote/fresh-clone gate. It must remain temporary,
path-free, network-free, and false for GitHub creation, push, release, and
deployment authority.
Run `env -u PYTHONPATH -u AO_LORE_HOME python3
scripts/rehearse-sanitized-lifecycle.py` followed by the same command with
`--check` for the fixed offline governed-lifecycle proof. Direct and check
stdout must be byte-identical and path-free.
