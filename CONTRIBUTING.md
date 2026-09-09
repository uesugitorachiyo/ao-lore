# Contributing

Run `make check` from a clean checkout before proposing a change. The repository
must remain independently buildable and testable without sibling repositories,
network access, provider access, or ignored local assets.

Never commit private evidence, customer or pilot material, credentials, tokens,
provider transcripts, runtime state, generated candidates, reviews, canonical
brain generations, caches, reports, archives, or model files. Machine-specific paths
are also forbidden. Tests use deterministic synthetic public-safe fixtures. Intentional inert
secret or path examples require an exact digest-bound release-policy exception.

Keep source, document IR, candidate, accepted review, canonical generation,
retrieval evidence, and answer authority distinct. A passing check or accepted
change grants no promotion, publication, release, deployment, provider, or
credential authority.
