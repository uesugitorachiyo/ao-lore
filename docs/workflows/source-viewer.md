# Source viewer: operator and integration runbook

## Trust and startup

The viewer is an explicitly started local source-display capability. Retrieval remains read-only and network-free. The viewer transport binds only to `127.0.0.1`; it does not turn existing workspace queries into HTTP operations. The repository permits this narrow, separately approved **loopback-only viewer** and no general outbound-network permission.

Use the synthetic demo first. The snapshot bridge is approved for public/redacted evaluation only. Native mode revalidates AO Lore state directly; it is still not sufficient to feed saved query JSON into the viewer and assume the source or whole-original permission is authenticated.

The exact source gate is:

`trusted local approval -> fixed retained binding digest -> complete origin key -> approved original digest -> no-follow read -> SHA-256 verification -> optional bounded rendering -> authenticated response`

It never translates a client string into a general filesystem locator.

## Private retention layout

Under the explicitly selected `AO_LORE_HOME`:

```text
source-viewer/
  approvals/<grant-id>.json
  bindings/<binding-sha256-hex>.json
  objects/<original-sha256-hex>
  native-approvals/<grant-id>.json
  native-bindings/<binding-sha256-hex>.json
```

These paths are internal operator documentation, not fields returned in query or viewer readbacks. Runtime content must remain ignored and company-owned. Approved bytes, whole-original sensitivity, native evidence custody, primary workspace and allowed direct references are trusted provisioning inputs. The viewer does not infer reference imports or traverse them recursively.

Approvals bind one exact manifest, permit public or internal whole-original content, explicitly specify `whole-source`, and expire within at most one day by contract. The provisioning helper uses at most a 60-minute lifetime and defaults to 30 minutes. The browser session lasts at most 20 minutes; approval expiry is checked during subsequent reads even if the session would otherwise remain valid. Open handles expire after at most two minutes. Select the source again to obtain a fresh handle within the existing authorized session.

Delete the approval file to deny subsequent reads, or stop the process to end all sessions. A changed approval cannot silently broaden the running process's rights. A changed binding manifest or original file produces a refusal. Replacing an original with a newer version does not update old citations; separately ingest and bind the new version.

Revocation cannot erase already displayed/saved content, screenshots or bytes retained by a human/browser. Memory-only sessions and no-store responses are not a DRM or forensic erasure system.

## Trusted provisioning interface

`ao_lore.source_viewer.store.provision_snapshot(home, manifest, originals, grant_id=...)`

`manifest` follows `source-viewer-bindings-v0.1.schema.json`. Each record retains the exact v0.1 document-evidence record, format, whole-original sensitivity, and an explicit physical-one-based PDF page convention. `originals` maps each selected source digest to its exact bytes. The helper rehashes those bytes, rejects mismatches, writes new immutable objects/bindings, and creates an approval **last**. It cannot overwrite an approval. It copies evidence records unchanged; it does not rederive native upstream IR/block/evidence digests.

The caller must first verify native evidence custody, reference eligibility and original sensitivity in the actual AO Lore backend. Do not implement provisioning by trusting a browser upload, source path supplied by a query caller, arbitrary JSON saved from chat, or an unvalidated native query wrapper. This delivery intentionally offers no such auto-import command. No automatic customer-data workflow is demonstrated.

`SnapshotStore` retains the v0.1 snapshot readback with
`provenance_mode=operator-approved-snapshot` and
`native_binding_revalidated=false`. `NativeSourceStore` reuses AO Lore's actual
registry selector, document-generation loader, evidence-ID derivation, and
descriptor-anchored inbox reader. Its v0.2 readback uses
`provenance_mode=native-retained-source` and
`native_binding_revalidated=true` only after all bindings and original bytes
have been revalidated.

Native approval is a trusted local operation:

`ao_lore.source_viewer.native_store.provision_native_approval(home, manifest, grant_id=...)`

The manifest follows `source-viewer-native-bindings-v0.1.schema.json` and binds
the current registry head, exact primary/direct-reference set, complete native
evidence records, physical PDF page basis, and whole-original sensitivity.
Start an approved native viewer with:

```bash
PYTHONPATH=src python -m ao_lore.source_viewer serve-native --grant-id <approved-id>
```

## Format behavior

PDF input is accepted only as digest-verified retained bytes with a `%PDF-` header. The optional renderer starts an isolated Python subprocess, initializes no document scripting/form environment and returns a PNG, page count and bounded geometry. Exact case-sensitive matches are checked against the PDF text API's returned character range. The renderer's own coordinate transform handles crop/rotation. Bare upstream `source_span.coordinates` is retained for inspection but never guessed to be pixels, points or normalized coordinates.

Physical-page metadata is explicit. No page means display page 1 without asserting it is the citation page. Invalid or out-of-range pages fail; the viewer never silently clamps a citation to a different page. Printed labels such as `iv`, `A-1`, or code chapter pagination are not substituted for physical page numbers.

UTF-8 text is literal and bounded. Supplied offsets are used only after equality with the cited text is checked. Otherwise only one exact occurrence can be highlighted. Backend-generated before/match/after segments avoid JavaScript UTF-16 versus Unicode-code-point errors.

DOCX uses bounded UTF-8 main-document XML, no DTD/entities and no external relationship traversal. UTF-16 XML, macros, images, headers/footers, tracked-change presentation and Word pagination are not supported by this projection. ZIP members are read, never extracted. Paragraph text may not reproduce the exact IR extractor's whitespace; that produces an unavailable highlight rather than an inferred match.

## Transport controls

The session token stays in JavaScript memory and is sent only in an Authorization header. It is never put into a URL, cookie, localStorage, sessionStorage or logs. Cookies were deliberately avoided because different localhost TCP ports do not constitute cookie isolation. Sessions and open handles are randomly generated and independent. Host and Origin must exactly match the process's numeric loopback origin; there is no CORS. All protected routes authenticate before resource lookup.

Static HTML, CSS and JavaScript are bundled as source literals; there are no external scripts, CDNs, remote fonts, telemetry, provider calls or remote document fetches. Source text is appended using DOM text nodes/textContent. The page renderer is an image, not an active PDF iframe. Optional raw-source downloads are attachment-only and use generic names rather than private filenames.

The service uses a bounded standard-library HTTP server for a local pilot, not a general production web server. It is not defended against a malicious process with the same OS identity, a compromised browser extension, root, or hostile native-parser exploits. The subprocess has time/memory/file-size/CPU constraints and Linux no-new-privileges, but no full seccomp/filesystem sandbox. Restrict the pilot to trusted public/redacted documents until deployment security review is complete.

## Common refusal codes

| Code | Meaning and operator action |
|---|---|
| `session_required` / `session_expired` | Authenticate, or restart for a new one-use launch code after expiry/reload. |
| `approval_expired` / `approval_revoked` | Obtain a separate renewed approval; do not edit the running grant to extend it. |
| `evidence_unavailable` | The complete originating workspace/generation/evidence key is not in the approved snapshot. |
| `source_integrity_failed` | Retained original bytes do not match the citation's bound digest. Investigate; never override. |
| `binding_drift` | The approved manifest or returned record changed. Investigate the retained state. |
| `retained_state_unavailable` | Missing, symlinked, unsafe-mode, nonregular or otherwise unreadable retained state. |
| `pdf_renderer_unavailable` | Install the separately approved viewer dependencies; no page verification is claimed meanwhile. |
| `invalid_page` | The supplied physical page is invalid/out of bounds; correct the source binding upstream. |
| `original_download_denied` | Whole-source viewing approval did not include permission for raw-original download. |
| `open_handle_unavailable` | Re-select the source within the still-authorized session; handles expire quickly. |

## Native boundaries

Native integration leaves existing workspace-query schemas and canonical state
unchanged. Registry or direct-reference advancement revokes the approval.
Missing/replaced originals, source/IR/block/evidence drift, sensitivity changes,
or ambiguous identities fail closed with path-free errors. Graph-only evidence
has no original-file mapping in this version and remains unsupported.
