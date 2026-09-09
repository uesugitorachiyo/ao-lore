# Refresh Evidence Graph

This generic workspace workflow revalidates evidence in one explicitly selected workspace. It
does not select, refresh, or cascade to another workspace, and it never
changes graph generations automatically.

## Command

Refresh is a workspace operation bound to a registered workspace identity:

```bash
ao-lore workspace refresh --workspace <registered-id> --json
```

The clean baseline is offline by default. The selected workspace definition,
context, retained policy, and exact source and graph bindings are validated
before the command returns one fixed redacted unavailable result; no socket or
HTTP client is constructed. A refresh with a separately reviewed trusted
domain transport may retain observations, comparisons, and summaries beneath
that workspace's state. Lower-level injected acquisition dependencies do not
grant public network authority.

A trusted domain transport must restrict resolution to globally routable
addresses, pin the connected peer on every hop, start at exact policy-bound
locators, and follow only validated allowlisted redirect hops. Refresh uses no
credentials or providers and remains one selected workspace operation; it does
not cascade to references or other workspaces. It does not rewrite acquisition
records, publish a graph generation, or grant candidate, canonical, provider,
publication, release, or deployment authority.

## Interpret classifications

- `unchanged` means observed bytes still match the retained baseline.
- `updated` means newly observed bytes differing from the immutable retained
  baseline, with no verified successor relationship established.
- `superseded` means a declared successor relationship was verified by exact
  bound version and effective-date evidence.
- `unavailable` means the declared locator returned an allowed terminal result.
- `investigate` means drift, ambiguity, unsafe observation, or incomplete
  recovery prevents a stronger conclusion.

`updated` and `superseded` require a later reviewed graph rebuild. No freshness
classification is promotion, canonical authority, legal advice, or approval.

## Offline replay and recovery

After one complete refresh, inspect, query, replay, and recovery remain local
operations on the same selected workspace:

```bash
ao-lore workspace inspect --workspace <registered-id> --json
ao-lore workspace query --workspace <registered-id> --prompt "guidance" --json
ao-lore workspace replay --workspace <registered-id> --json
ao-lore workspace recover --workspace <registered-id> --json
```

The reader reloads only the latest complete, fully validated freshness bundle
for that workspace. It does not trust file mtimes. Ambiguous or drifted
completed bundles fail closed.

Selected affected evidence downgrades query outcomes to `investigate` and adds
the stable qualification `Source freshness requires review.` Unselected
affected sources do not poison unrelated unchanged evidence.

All disposable refresh fixtures are synthetic, public-safe, offline, and
disposable. Disposable refresh fixtures do not inspect canonical roots,
perform live refresh, or mutate retained evidence.
