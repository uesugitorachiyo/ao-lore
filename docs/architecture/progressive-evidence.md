# Progressive Evidence Architecture

AO Lore retrieves verified document evidence directly by default. A document
does not need an evidence graph, candidate, accepted review, or canonical entry
to be searchable through the workspace evidence-query boundary.

Graph enrichment is conditional. Add an edge only when direct evidence supports
the relationship and the edge materially improves retrieval, applicability,
conflict handling, qualification, or supersession. Graph size is not a quality
measure, and absence of a graph is not an error for document-only workspaces.
The generic graph APIs retain exact source, claim, relationship, freshness,
qualification, contradiction, and refusal bindings. Graph membership is
informational retrieval state, not canonical knowledge.

Candidate preparation is a separate, explicit governed selection from verified
document evidence, verified graph evidence, or both. Retrieval and graph
construction never create candidates as a side effect. A candidate remains
non-canonical after preparation and review acceptance; only a separately
authorized promotion can append a canonical generation.

All retrieval is deterministic, local, read-only, provider-free, network-free,
and non-persistent. Evidence identities retain workspace, graph or document,
source, claim or edge, and exact digest bindings. Contradictions remain distinct
and invoke the hard contradiction gate rather than being merged into consensus.
