# Standalone migration provenance

## Outcome

AO Lore was recovered into this standalone repository without modifying or
deleting the misplaced source branches. The recovered Python foundation was
repackaged beneath `src/ao_lore/`; its contracts, fixtures, workflows, and
tests were moved beneath this repository's own boundaries. The evaluation and
monitoring behavior was adapted into repository-local Python packages and
contracts so AO Lore has no runtime or test dependency on sibling projects.

## Read-only recovery inputs

The following local commits were used only as recovery references:

- deterministic product foundation: `d869be3065c181b4d07ea0e8bcfb34f9bf02fa7d`;
- supplied-evidence evaluation behavior: `6ac6462`;
- read-only monitoring behavior: `a9e4987`.

These references are provenance, not product dependencies. No source import,
package, schema, generated state, or service from the original repositories is
required to build, test, or run AO Lore. AO Mission may resume the external
development-control-plane lifecycle while AO Lore remains standalone.

## Preservation boundary

The misplaced branches and generated coordination state remain unchanged for
audit and rollback. They are not deleted or rewritten by this migration.
Cleanup requires a separate operator decision after the standalone repository
has been independently reviewed.

The Mac host was not contacted and no synchronization, provider call,
credential use, release, deployment, or publication occurred.
