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

## Bounded document evidence sets

The document-query path uses a deterministic, version-bound evidence-set
selector. It first recognizes an explicit leading document heading when the
question names a subject, so a longer exact subject (for example, “Birch Depot
East”) is not silently replaced by a similarly named document. It then ranks
fact-bearing blocks using lexical overlap, numeric and effective-change cues,
and reserves an initial slot per eligible source before filling remaining
blocks. Selection has fixed document, block, character, and UTF-8 byte budgets;
it never truncates, synthesizes, or opens an original file.

Restricted documents never supply ranking features or rendered evidence. A
direct restricted request refuses, while an unrelated restricted document must
not block a named public subject. Stale selected material remains an
investigation result. Budget saturation is explicit through `partial` and
qualification codes rather than being represented as a complete answer.

Integrations that need a source passage can use the pure exact-text export
boundary. It revalidates the native generation and query readback, requires a
one-to-one supplied source mapping and SHA-256 match, and verifies Unicode
character offsets before returning a passage. It does not read paths, search
for approximate text, infer PDF/OCR coordinates, or grant whole-document
viewing permission.
