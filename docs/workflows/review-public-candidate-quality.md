# Review public candidate quality

This offline deterministic workflow evaluates non-canonical candidates without
deciding their review status or querying canonical knowledge.

It verifies every claim, citation, mapping, provenance binding, and public
source digest for six candidates. It selects exactly 96 claims using a closed
sampling policy, requires one semantic label per selection, and runs four
precommitted candidate-payload questions per source. Unsupported questions must
refuse with no evidence.

Verify an existing immutable campaign:

```bash
python3 scripts/rehearse-public-candidate-quality-review.py --check
```

`--check` fails if the campaign is missing or differs. It creates no lock,
staging directory, campaign, review, or brain entry. The execution form accepts
no root, force, network, provider, review, or promotion override.

## Interpret results

- `pass` means all strict quality thresholds passed. It is not acceptance.
- `hold` means bindings remain intact but nonmaterial quality thresholds missed.
- `reject` identifies corrupt, unbound, invented, misattributed, or materially
  misleading evidence. It is still not a candidate rejection event.

The current campaign produced six `hold` recommendations. An actual candidate
accept/reject remains a separate operator action. Promotion then requires an
additional exact one-shot authorization. Neither this workflow, passing tests,
AO Mission reconciliation, nor a recommendation grants review, promotion,
canonical-query, publication, release, or deployment authority.

Detailed public candidate text, annotation rationale, questions, and results
remain beneath ignored `.ao-lore/public-candidate-quality-review-20260812/`.
