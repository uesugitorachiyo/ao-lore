"""Public synthetic unit, security and HTTP integration tests. No external network."""
from __future__ import annotations

import copy
import http.client
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ao_lore.source_viewer.contracts import (
    MAX_TEXT, ViewerError, canonical_json, digest, strict_json, validate_evidence,
    validate_grant, validate_key, validate_manifest, validate_resolve,
)
from ao_lore.source_viewer.demo import (
    PDF_EXCERPT, TEXT_EXCERPT, DOCX_EXCERPT, create_demo, demo_material,
    synthetic_docx, synthetic_pdf, synthetic_record,
)
from ao_lore.source_viewer.formats import docx_projection, exact_segments, render_pdf, text_projection
from ao_lore.source_viewer.server import ViewerState, fingerprint, make_server
from ao_lore.source_viewer.store import SafeRoot, SnapshotStore, provision_snapshot

KEY_FIELDS = ("workspace_id", "generation_digest", "evidence_id")
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-renderer"
HAS_PDF = importlib.util.find_spec("pypdfium2") is not None and importlib.util.find_spec("PIL") is not None


def key(record):
    return {field: record["evidence"][field] for field in KEY_FIELDS}


def fake_renderer(data, page, excerpt=""):
    if not 1 <= page <= 3:
        raise ViewerError("invalid_page")
    return {"page": page, "page_count": 3, "width": 1000, "height": 1295,
            "highlight": {"status": "exact_match_unavailable", "boxes": []}, "png": PNG}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.manifest, self.originals = demo_material()
        self.now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = lambda: self.now
        self.grant = provision_snapshot(self.home, self.manifest, self.originals,
                                       grant_id="test-grant", clock=self.clock)
        self.store = SnapshotStore(self.home, "test-grant", clock=self.clock)

    def state(self):
        state = ViewerState(self.store, renderer=fake_renderer)
        token = state.login(state.launch_code)
        return state, token, state.authenticate("Bearer " + token)

    def approval(self):
        return self.home / "source-viewer" / "approvals" / "test-grant.json"

    def source(self, index=0):
        return self.home / "source-viewer" / "objects" / self.manifest["records"][index]["evidence"]["source_digest"][7:]


class TestContracts(unittest.TestCase):
    def setUp(self):
        self.manifest, _ = demo_material()
        self.record = self.manifest["records"][0]

    def test_public_synthetic_records_validate(self):
        validate_manifest(self.manifest)
        validate_evidence(self.record["evidence"])
        validate_key(key(self.record))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ViewerError): strict_json(b'{"a":1,"a":2}')

    def test_nonfinite_json_rejected(self):
        for value in (b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}'):
            with self.subTest(value=value), self.assertRaises(ViewerError): strict_json(value)

    def test_bounded_json(self):
        with self.assertRaises(ViewerError): strict_json(b'"abcd"', 2)

    def test_extra_locator_rejected(self):
        self.record["evidence"]["local_path"] = "/private/customer.pdf"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_source_id_cannot_be_path(self):
        self.record["evidence"]["source_id"] = "../../private"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_reversed_span_rejected(self):
        self.record["evidence"]["source_span"].update(start=9, end=1)
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_bool_is_not_page_integer(self):
        self.record["evidence"]["source_span"]["page"] = True
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_finite_untyped_coordinates_preserved(self):
        self.record["evidence"]["source_span"]["coordinates"] = [50, 10, 200, 60]
        validate_manifest(self.manifest)

    def test_nonfinite_coordinates_rejected(self):
        self.record["evidence"]["source_span"]["coordinates"] = [float("inf"), 0, 1, 2]
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_undeclared_workspace_rejected(self):
        self.record["evidence"]["workspace_id"] = "another-private-workspace"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_identity_collision_rejected(self):
        self.manifest["records"].append(copy.deepcopy(self.record))
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_restricted_original_denied(self):
        self.record["original_sensitivity"] = "restricted"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_public_original_cannot_contain_internal_evidence(self):
        self.record["evidence"]["sensitivity"] = "internal"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_conflicting_whole_source_classification_denied(self):
        other = copy.deepcopy(self.record)
        other["evidence"]["evidence_id"] = digest(b"another-evidence")
        other["original_sensitivity"] = "internal"
        self.manifest["records"].append(other)
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_direct_reference_not_primary(self):
        self.manifest["reference_workspace_ids"].append("viewer-demo")
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_wrong_enum_type_rejected(self):
        for field in ("authority_role", "freshness_status", "sensitivity"):
            candidate = copy.deepcopy(self.record["evidence"])
            candidate[field] = []
            with self.subTest(field=field), self.assertRaises(ViewerError): validate_evidence(candidate)

    def test_surrogate_text_rejected(self):
        self.record["evidence"]["render_text"] = "\ud800"
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)

    def test_generation_is_part_of_key(self):
        candidate = key(self.record); candidate.pop("generation_digest")
        with self.assertRaises(ViewerError): validate_key(candidate)

    def test_record_count_limit(self):
        self.manifest["records"] *= 50
        with self.assertRaises(ViewerError): validate_manifest(self.manifest)


class TestStore(Fixture):
    def test_retained_original_digest_verified(self):
        for record in self.manifest["records"]:
            self.assertEqual(digest(self.store.source_bytes(record)), record["evidence"]["source_digest"])

    def test_source_mutation_fails_closed(self):
        self.source().write_bytes(b"changed")
        with self.assertRaisesRegex(ViewerError, "source_integrity_failed"):
            self.store.source_bytes(self.manifest["records"][0])

    def test_approval_mutation_revokes_running_process(self):
        value = strict_json(self.approval().read_bytes()); value["allow_original_download"] = True
        self.approval().write_bytes(canonical_json(value))
        with self.assertRaisesRegex(ViewerError, "approval_revoked"): self.store.authorize()

    def test_deleted_approval_denied(self):
        self.approval().unlink()
        with self.assertRaises(ViewerError): self.store.list_records()

    def test_binding_mutation_denied(self):
        path = self.home / "source-viewer" / "bindings" / (self.grant["binding_digest"][7:] + ".json")
        path.write_bytes(b"{}")
        with self.assertRaisesRegex(ViewerError, "binding_drift"): self.store.authorize()

    def test_missing_source_denied(self):
        self.source().unlink()
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_source_symlink_denied(self):
        source = self.source(); data = source.read_bytes(); source.unlink()
        other = self.home / "outside"; other.write_bytes(data); other.chmod(0o600)
        source.symlink_to(other)
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_source_directory_symlink_denied(self):
        directory = self.home / "source-viewer" / "objects"
        moved = self.home / "elsewhere"; directory.rename(moved); directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_runtime_symlink_denied(self):
        alias = self.home / "alias"; alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises((ViewerError, OSError)): SafeRoot(alias)

    def test_hardlinked_source_denied(self):
        os.link(self.source(), self.home / "hardlink")
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_fifo_rejected_without_blocking(self):
        source = self.source(); source.unlink(); os.mkfifo(source, 0o600)
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_group_writable_file_denied(self):
        self.source().chmod(0o660)
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_group_writable_directory_denied(self):
        (self.home / "source-viewer" / "objects").chmod(0o770)
        with self.assertRaises(ViewerError): self.store.source_bytes(self.manifest["records"][0])

    def test_read_does_not_mutate_retained_bytes(self):
        before = {str(p.relative_to(self.home)): digest(p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        for record in self.store.list_records(): self.store.source_bytes(record)
        after = {str(p.relative_to(self.home)): digest(p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_cross_workspace_key_denied(self):
        record = self.manifest["records"][0]["evidence"]
        with self.assertRaisesRegex(ViewerError, "evidence_unavailable"):
            self.store.get_record(("another-workspace", record["generation_digest"], record["evidence_id"]))

    def test_generation_mismatch_denied(self):
        record = self.manifest["records"][0]["evidence"]
        with self.assertRaises(ViewerError):
            self.store.get_record((record["workspace_id"], digest(b"wrong-generation"), record["evidence_id"]))

    def test_declared_reference_retains_origin(self):
        records = self.store.list_records()
        self.assertEqual(records[2]["evidence"]["workspace_id"], "demo-reference")

    def test_approval_expires(self):
        self.now += timedelta(minutes=31)
        with self.assertRaisesRegex(ViewerError, "approval_expired"): self.store.authorize()

    def test_clock_before_approval_denied(self):
        self.now -= timedelta(seconds=1)
        with self.assertRaisesRegex(ViewerError, "approval_expired"): self.store.authorize()

    def test_provision_never_overwrites_approval(self):
        before = self.approval().read_bytes()
        with self.assertRaises(ViewerError):
            provision_snapshot(self.home, self.manifest, self.originals, grant_id="test-grant", clock=self.clock)
        self.assertEqual(before, self.approval().read_bytes())

    def test_internal_original_requires_explicit_internal_approval(self):
        manifest = copy.deepcopy(self.manifest); manifest["records"][0]["original_sensitivity"] = "internal"
        with self.assertRaises(ViewerError):
            provision_snapshot(self.home, manifest, self.originals, grant_id="other", clock=self.clock)

    def test_provision_rejects_source_digest_mismatch(self):
        originals = dict(self.originals); originals[next(iter(originals))] = b"not-original"
        with self.assertRaisesRegex(ViewerError, "source_integrity_failed"):
            provision_snapshot(self.home, self.manifest, originals, grant_id="other", clock=self.clock)

    def test_approval_maximum_lifetime(self):
        bad = copy.deepcopy(self.grant); bad["expires_at"] = "2026-09-16T12:00:00Z"
        with self.assertRaises(ViewerError): validate_grant(bad)

    def test_caller_cannot_mutate_stored_record(self):
        record = self.store.list_records()[0]; record["evidence"]["render_text"] = "modified"
        self.assertEqual(self.store.list_records()[0]["evidence"]["render_text"], PDF_EXCERPT)
        with self.assertRaisesRegex(ViewerError, "binding_drift"): self.store.source_bytes(record)


class TestSessionAndResolution(Fixture):
    def test_login_is_one_use(self):
        state = ViewerState(self.store); code = state.launch_code; state.login(code)
        with self.assertRaisesRegex(ViewerError, "session_denied"): state.login(code)

    def test_expired_launch_code(self):
        now = [0.0]; state = ViewerState(self.store, timer=lambda: now[0]); now[0] = 121
        with self.assertRaises(ViewerError): state.login(state.launch_code)

    def test_wrong_launch_code(self):
        state = ViewerState(self.store)
        with self.assertRaisesRegex(ViewerError, "session_denied"): state.login("x" * 43)

    def test_session_expiry(self):
        now = [0.0]; state = ViewerState(self.store, timer=lambda: now[0])
        token = state.login(state.launch_code); now[0] = 1201
        with self.assertRaisesRegex(ViewerError, "session_expired"): state.authenticate("Bearer " + token)

    def test_unauthenticated_session_rejected(self):
        state = ViewerState(self.store)
        for header in (None, "Basic abc", "Bearer ../../../", "Bearer " + "z" * 43):
            with self.subTest(header=header), self.assertRaises(ViewerError): state.authenticate(header)

    def test_resolve_preserves_exact_evidence_and_admits_bridge_limit(self):
        state, _, session = self.state()
        result = state.resolve(session, key(self.manifest["records"][0]))
        validate_resolve(result)
        self.assertEqual(result["evidence"], self.manifest["records"][0]["evidence"])
        self.assertTrue(result["original_bytes_verified"])
        self.assertFalse(result["native_binding_revalidated"])
        self.assertNotIn(str(self.home), json.dumps(result))

    def test_opaque_handle_is_not_an_authorization(self):
        state, _, session = self.state(); result = state.resolve(session, key(self.manifest["records"][0]))
        other = fingerprint("B" * 43); state.sessions[other] = state.timer() + 500
        with self.assertRaisesRegex(ViewerError, "open_handle_unavailable"): state.page(other, result["open_id"], 2)

    def test_open_handle_expiry(self):
        now = [0.0]; state = ViewerState(self.store, renderer=fake_renderer, timer=lambda: now[0])
        token = state.login(state.launch_code); session = state.authenticate("Bearer " + token)
        result = state.resolve(session, key(self.manifest["records"][0])); now[0] = 121
        with self.assertRaisesRegex(ViewerError, "open_handle_unavailable"): state.page(session, result["open_id"], 2)

    def test_handle_count_is_bounded(self):
        state, _, session = self.state()
        for i in range(128): state.handles[str(i)] = {"until": state.timer()+100, "session": session}
        with self.assertRaisesRegex(ViewerError, "open_handle_limit"):
            state.resolve(session, key(self.manifest["records"][0]))

    def test_approval_revocation_blocks_existing_handle(self):
        state, _, session = self.state(); result = state.resolve(session, key(self.manifest["records"][0]))
        self.approval().unlink()
        with self.assertRaises(ViewerError): state.page(session, result["open_id"], 2)

    def test_original_download_disabled_by_default(self):
        state, _, session = self.state(); result = state.resolve(session, key(self.manifest["records"][0]))
        with self.assertRaisesRegex(ViewerError, "original_download_denied"): state.original(session, result["open_id"])

    def test_explicit_download_permission_is_enforced(self):
        provision_snapshot(self.home, self.manifest, self.originals, grant_id="download", clock=self.clock,
                           allow_original_download=True)
        store = SnapshotStore(self.home, "download", clock=self.clock)
        state = ViewerState(store, renderer=fake_renderer); token = state.login(state.launch_code)
        session = state.authenticate("Bearer " + token); result = state.resolve(session, key(self.manifest["records"][0]))
        data, filename = state.original(session, result["open_id"])
        self.assertEqual(filename, "source.pdf"); self.assertEqual(digest(data), result["evidence"]["source_digest"])

    def test_logout_revokes_handles_and_session(self):
        state, token, session = self.state(); result = state.resolve(session, key(self.manifest["records"][0]))
        state.logout(session)
        with self.assertRaises(ViewerError): state.authenticate("Bearer " + token)
        self.assertEqual(state.handles, {})

    def test_missing_renderer_does_not_fake_page_verification(self):
        def missing(*args): raise ViewerError("pdf_renderer_unavailable", 503)
        state = ViewerState(self.store, renderer=missing); token = state.login(state.launch_code)
        result = state.resolve(state.authenticate("Bearer " + token), key(self.manifest["records"][0]))
        self.assertEqual(result["display"]["highlight"]["status"], "not_verified")
        self.assertIsNone(result["display"]["page_count"])

    def test_bad_renderer_output_does_not_escape_schema(self):
        def bad(*args):
            output = fake_renderer(*args); output["private_path"] = "/sensitive/path"; return output
        state = ViewerState(self.store, renderer=bad); token = state.login(state.launch_code)
        with self.assertRaises(ViewerError):
            state.resolve(state.authenticate("Bearer " + token), key(self.manifest["records"][0]))

    def test_in_memory_audit_has_no_credentials_or_excerpts(self):
        state, token, session = self.state(); result = state.resolve(session, key(self.manifest["records"][0]))
        audit = json.dumps(list(state.audit))
        for prohibited in (token, result["open_id"], PDF_EXCERPT, str(self.home)):
            self.assertNotIn(prohibited, audit)


class TestTextAndDocx(unittest.TestCase):
    def test_unicode_offsets_are_codepoints(self):
        text = "😀日本語\nCited evidence\nend"; quote = "Cited evidence"; start = text.index(quote)
        result = exact_segments(text, quote, {"start": start, "end": start + len(quote)})
        self.assertEqual(result["status"], "exact_verified_span")
        self.assertEqual(result["before"] + result["match"] + result["after"], text)

    def test_mismatched_offsets_fall_back_to_unique_exact_text(self):
        result = exact_segments("prefix passage suffix", "passage", {"start": 0, "end": 7})
        self.assertEqual(result["status"], "exact_unique_text")

    def test_ambiguous_excerpt_not_guessed(self):
        self.assertEqual(exact_segments("same and same", "same", {})["status"], "ambiguous_exact_match")

    def test_verified_offset_disambiguates_identical_text(self):
        self.assertEqual(exact_segments("same and same", "same", {"start": 9, "end": 13})["status"], "exact_verified_span")

    def test_no_fuzzy_matching(self):
        self.assertEqual(exact_segments("passage", "Passage", {})["status"], "exact_match_unavailable")

    def test_empty_excerpt_is_explicit(self):
        self.assertEqual(exact_segments("content", "", {})["status"], "empty_excerpt")

    def test_html_is_literal_not_filtered_into_evidence(self):
        malicious = '<img src=x onerror="alert(1)">'  # must remain literal source text
        record = synthetic_record(malicious.encode(), "text", malicious)
        result = text_projection(malicious.encode(), record)
        self.assertEqual(result["segments"]["match"], malicious)

    def test_invalid_utf8_rejected(self):
        record = synthetic_record(b"\xff", "text", "test")
        with self.assertRaisesRegex(ViewerError, "unsupported_text_encoding"): text_projection(b"\xff", record)

    def test_binary_text_rejected(self):
        record = synthetic_record(b"test\x00", "text", "test")
        with self.assertRaises(ViewerError): text_projection(b"test\x00", record)

    def test_large_text_rejected_not_truncated(self):
        data = b"A" * (MAX_TEXT + 1); record = synthetic_record(data, "text", "A")
        with self.assertRaisesRegex(ViewerError, "resource_limit"): text_projection(data, record)

    def test_docx_main_body_projection(self):
        data = synthetic_docx(["Heading", DOCX_EXCERPT])
        self.assertEqual(docx_projection(data), "Heading\n" + DOCX_EXCERPT)

    def test_docx_is_not_pagination(self):
        data = synthetic_docx([DOCX_EXCERPT]); record = synthetic_record(data, "docx", DOCX_EXCERPT)
        result = text_projection(data, record)
        self.assertIn("not Word pagination", result["display_label"])

    def test_docx_duplicate_entries_rejected(self):
        data = io.BytesIO(synthetic_docx(["text"]))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(data, "a") as archive: archive.writestr("word/document.xml", "text")
        with self.assertRaises(ViewerError): docx_projection(data.getvalue())

    def test_docx_dtd_rejected(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>')
        with self.assertRaisesRegex(ViewerError, "invalid_document"): docx_projection(data.getvalue())

    def test_docx_zip_bomb_bound(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("word/document.xml", "a" * 200000)
        with self.assertRaisesRegex(ViewerError, "resource_limit"): docx_projection(data.getvalue())

    def test_docx_never_extracts_zip_paths(self):
        data = io.BytesIO(synthetic_docx(["safe text"]))
        with zipfile.ZipFile(data, "a") as archive: archive.writestr("../../escaped", "bad")
        self.assertEqual(docx_projection(data.getvalue()), "safe text")


@unittest.skipUnless(HAS_PDF, "optional pypdfium2/Pillow renderer not installed")
class TestPdf(unittest.TestCase):
    def test_original_pdf_page_and_exact_highlight(self):
        manifest, originals = demo_material(); record = manifest["records"][0]
        result = render_pdf(originals[record["evidence"]["source_digest"]], 2, PDF_EXCERPT)
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["highlight"]["status"], "exact_unique_text")
        self.assertTrue(result["highlight"]["boxes"])
        self.assertTrue(result["png"].startswith(b"\x89PNG"))

    def test_out_of_range_page_denied(self):
        data = synthetic_pdf([["one"]])
        with self.assertRaisesRegex(ViewerError, "invalid_page"): render_pdf(data, 2)

    def test_ambiguous_exact_pdf_match(self):
        result = render_pdf(synthetic_pdf([["same", "same"]]), 1, "same")
        self.assertEqual(result["highlight"]["status"], "ambiguous_exact_match")
        self.assertEqual(result["highlight"]["boxes"], [])

    def test_pdf_case_mismatch_is_not_highlighted(self):
        result = render_pdf(synthetic_pdf([["Exact text"]]), 1, "exact text")
        self.assertEqual(result["highlight"]["status"], "exact_match_unavailable")

    def test_blank_pdf_no_fake_highlight(self):
        result = render_pdf(synthetic_pdf([[]]), 1, "absent")
        self.assertEqual(result["highlight"]["status"], "exact_match_unavailable")

    def test_no_cited_excerpt_explicit(self):
        result = render_pdf(synthetic_pdf([["text"]]), 1)
        self.assertEqual(result["highlight"]["status"], "no_cited_excerpt_on_this_page")

    def test_rotated_and_cropped_geometry(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                data = synthetic_pdf([["heading", "Original source", "Exact passage"]],
                                     rotation=rotation, crop=(20, 20, 592, 770))
                result = render_pdf(data, 1, "Exact passage")
                self.assertEqual(result["highlight"]["status"], "exact_unique_text")
                for x, y, w, h in result["highlight"]["boxes"]:
                    self.assertTrue(0 <= x < x+w <= 1 and 0 <= y < y+h <= 1)

    def test_invalid_pdf_header_denied(self):
        with self.assertRaisesRegex(ViewerError, "invalid_document"): render_pdf(b"not a pdf", 1)

    def test_large_highlight_excerpt_not_silently_truncated(self):
        result = render_pdf(synthetic_pdf([["text"]]), 1, "x" * 16385)
        self.assertEqual(result["highlight"]["status"], "highlight_excerpt_limit")


class TestHttp(Fixture):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.store, renderer=fake_renderer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.token = None

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def call(self, method, path, body=None, headers=None):
        merged = {"Origin": self.server.origin}
        if self.token: merged["Authorization"] = "Bearer " + self.token
        if body is not None:
            merged["Content-Type"] = "application/json"
            if not isinstance(body, (str, bytes)): body = json.dumps(body)
        merged.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse(); content = response.read()
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def login(self):
        status, _, data = self.call("POST", "/api/session", {"launch_code": self.server.state.launch_code})
        self.assertEqual(status, 200)
        self.token = json.loads(data)["session_token"]

    def test_public_shell_contains_no_private_evidence_or_credentials(self):
        status, headers, data = self.call("GET", "/")
        self.assertEqual(status, 200)
        for secret in (PDF_EXCERPT, self.server.state.launch_code, str(self.home)):
            self.assertNotIn(secret.encode(), data)
        self.assertNotIn("Set-Cookie", headers)

    def test_authentication_precedes_evidence_lookup(self):
        status, _, _ = self.call("POST", "/api/resolve", key(self.manifest["records"][0]))
        self.assertEqual(status, 401)
        self.assertEqual(len(self.server.state.handles), 0)

    def test_exact_host_prevents_rebinding(self):
        self.assertEqual(self.call("GET", "/", headers={"Host": "attacker.example"})[0], 403)

    def test_exact_origin_prevents_cross_origin_login(self):
        status, _, _ = self.call("POST", "/api/session", {"launch_code": self.server.state.launch_code},
                                 headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertFalse(self.server.state.launch_used)

    def test_cross_site_fetch_rejected(self):
        self.assertEqual(self.call("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_no_cors_and_security_headers(self):
        _, headers, _ = self.call("GET", "/")
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_source_paths_and_query_tokens_rejected(self):
        for path in ("/../../etc/passwd", "/%2e%2e/etc/passwd", "/api/evidence?token=secret", "/file:///etc/passwd"):
            with self.subTest(path=path):
                status, _, content = self.call("GET", path)
                self.assertEqual(status, 403)
                self.assertNotIn(b"secret", content); self.assertNotIn(b"/etc/passwd", content)

    def test_duplicate_host_header_denied(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("GET", "/"); connection.putheader("Host", self.server.expected_host)
        connection.endheaders(); response = connection.getresponse()
        self.assertEqual(response.status, 403); response.read(); connection.close()

    def test_oversized_body_denied(self):
        status, _, _ = self.call("POST", "/api/session", "x" * 8193)
        self.assertEqual(status, 403)

    def test_duplicate_json_key_denied(self):
        status, _, _ = self.call("POST", "/api/session", '{"launch_code":"x","launch_code":"y"}')
        self.assertEqual(status, 403)

    def test_chunked_request_denied(self):
        status, _, _ = self.call("POST", "/api/session", "{}", headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 403)

    def test_unsupported_verbs_do_not_expose_files(self):
        for method in ("HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"):
            with self.subTest(method=method): self.assertEqual(self.call(method, "/")[0], 405)

    def test_authenticated_full_http_resolution_and_page(self):
        self.login()
        status, _, data = self.call("GET", "/api/evidence")
        self.assertEqual(status, 200); self.assertEqual(len(json.loads(data)["evidence"]), 3)
        status, _, data = self.call("POST", "/api/resolve", key(self.manifest["records"][0]))
        self.assertEqual(status, 200); result = json.loads(data); validate_resolve(result)
        status, headers, content = self.call("GET", "/api/open/" + result["open_id"] + "/page/2")
        self.assertEqual(status, 200); self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(content, PNG)

    def test_native_private_errors_are_redacted(self):
        self.login(); self.source().unlink()
        status, _, data = self.call("POST", "/api/resolve", key(self.manifest["records"][0]))
        self.assertEqual(status, 403)
        self.assertNotIn(str(self.home).encode(), data)
        self.assertEqual(set(json.loads(data)), {"error"})

    def test_cross_workspace_http_denial(self):
        self.login(); value = key(self.manifest["records"][0]); value["workspace_id"] = "neighbor"
        self.assertEqual(self.call("POST", "/api/resolve", value)[0], 404)

    def test_no_arbitrary_path_field_in_resolve(self):
        self.login(); value = key(self.manifest["records"][0]); value["path"] = "/etc/passwd"
        self.assertEqual(self.call("POST", "/api/resolve", value)[0], 403)

    def test_original_download_requires_separate_approval(self):
        self.login(); _, _, data = self.call("POST", "/api/resolve", key(self.manifest["records"][0]))
        handle = json.loads(data)["open_id"]
        self.assertEqual(self.call("GET", "/api/open/" + handle + "/original")[0], 403)

    def test_cookie_is_not_session_authority(self):
        self.login(); token = self.token; self.token = None
        self.assertEqual(self.call("GET", "/api/evidence", headers={"Cookie": "session=" + token})[0], 401)

    def test_logout_http_revokes_authority(self):
        self.login(); self.assertEqual(self.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.call("GET", "/api/evidence")[0], 401)


if __name__ == "__main__":
    unittest.main()
