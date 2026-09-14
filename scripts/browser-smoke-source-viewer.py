#!/usr/bin/env python3
"""Real-browser PUBLIC SYNTHETIC smoke test. Requires Playwright and local Chromium.

Never records HTTP headers, trace archives, tokens or customer evidence. Browser tests
exercise the local viewer only; they are not a native AO Lore integration gate.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ao_lore.source_viewer.demo import PDF_EXCERPT, TEXT_EXCERPT, DOCX_EXCERPT, create_demo, synthetic_record
from ao_lore.source_viewer.contracts import digest
from ao_lore.source_viewer.store import SnapshotStore, provision_snapshot
from ao_lore.source_viewer.server import make_server


def serve(store):
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError:
        print("Playwright is unavailable; browser gate NOT RUN.", file=sys.stderr)
        return 2
    checks = []
    failures = []
    external_requests = []
    console_errors = []
    with tempfile.TemporaryDirectory(prefix="ao-lore-viewer-browser-") as temporary:
        home = Path(temporary)
        grant = create_demo(home)
        server, thread = serve(SnapshotStore(home, grant))
        try:
            with sync_playwright() as playwright:
                launch = {"headless": True}
                browser_path = next((shutil.which(name) for name in (
                    "chromium", "google-chrome-stable", "google-chrome"
                ) if shutil.which(name)), None)
                if browser_path:
                    launch["executable_path"] = browser_path
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    # Only a disposable root-run synthetic CI check needs this. This flag
                    # is never used by the product viewer or recommended for customer data.
                    launch["args"] = ["--no-sandbox"]
                browser = playwright.chromium.launch(**launch)
                context = browser.new_context(viewport={"width": 1440, "height": 1080}, device_scale_factor=1)
                page = context.new_page()
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: failures.append(str(error)))
                def observe(request):
                    url = urlparse(request.url)
                    if url.scheme in {"http", "https"} and url.netloc != server.expected_host:
                        external_requests.append(url.scheme + "://" + url.netloc)
                page.on("request", observe)
                page.goto(server.origin, wait_until="networkidle")
                expect(page.locator("#login")).to_be_visible()
                expect(page.locator("#workspace")).to_be_hidden()
                checks.append("unauthenticated shell contains no evidence")
                page.locator("#code").fill(server.state.launch_code)
                page.locator("#login-form button").click()
                expect(page.locator("#excerpt")).to_have_text(PDF_EXCERPT, timeout=30000)
                expect(page.locator("#paper")).to_be_visible(timeout=30000)
                page.wait_for_function("document.getElementById('page-image').naturalWidth > 0")
                expect(page.locator("#page")).to_have_value("2")
                expect(page.locator("#highlights .highlight")).to_have_count(1)
                expect(page.locator("#highlight-status")).to_have_text("Exact passage highlighted")
                checks += ["launch-code login", "physical PDF page 2", "exact excerpt preserved", "exact PDF text geometry visible"]
                page.screenshot(path=str(args.output_dir / "viewer-pdf.png"), full_page=True)
                page.locator("#previous").click()
                expect(page.locator("#page")).to_have_value("1", timeout=30000)
                expect(page.locator("#highlights .highlight")).to_have_count(0)
                checks += ["previous-page navigation", "no false highlight on another page"]
                page.locator("#next").click()
                expect(page.locator("#page")).to_have_value("2", timeout=30000)
                expect(page.locator("#highlights .highlight")).to_have_count(1)
                checks.append("return to citation restores exact highlight")
                page.locator("#cards button").nth(1).click()
                expect(page.locator("#excerpt")).to_have_text(TEXT_EXCERPT)
                expect(page.locator("#text-document mark")).to_have_text(TEXT_EXCERPT)
                expect(page.locator("#pagination")).to_be_hidden()
                checks += ["literal UTF-8 source projection", "verified text span highlight"]
                page.screenshot(path=str(args.output_dir / "viewer-text.png"), full_page=True)
                page.locator("#cards button").nth(2).click()
                expect(page.locator("#excerpt")).to_have_text(DOCX_EXCERPT)
                expect(page.locator("#text-document mark")).to_have_text(DOCX_EXCERPT)
                expect(page.locator("#document-caption")).to_contain_text("not Word pagination")
                expect(page.locator("#metadata")).to_contain_text("demo-reference")
                expect(page.locator("#download")).to_be_hidden()
                checks += ["DOCX paragraph text highlight", "Word layout limitation labeled", "direct-reference origin preserved", "unapproved original download hidden"]
                if context.cookies():
                    raise RuntimeError("browser session cookie created")
                if page.evaluate("localStorage.length + sessionStorage.length") != 0:
                    raise RuntimeError("browser credential storage used")
                checks += ["no cookies", "no localStorage or sessionStorage credentials"]
                page.locator("#logout").click()
                expect(page.locator("#workspace")).to_be_hidden()
                expect(page.locator("#excerpt")).to_have_text("")
                checks.append("logout clears private DOM and session")
                context.close()
                # Separate approved public fixture proves source markup is displayed as text.
                xss_home = home / "xss-fixture"
                malicious = '<script>window.XSS_EXECUTED=true</script><img src="https://invalid.example/x" onerror="window.XSS_EXECUTED=true">'
                data = malicious.encode()
                record = synthetic_record(data, "text", malicious, source_id="literal-markup-test")
                manifest = {"schema_version": "ao.lore.source-viewer-bindings.v0.1", "primary_workspace_id": "viewer-demo",
                            "reference_workspace_ids": [], "provenance_mode": "operator-approved-snapshot", "records": [record]}
                provision_snapshot(xss_home, manifest, {digest(data): data}, grant_id="xss-test")
                xss_server, xss_thread = serve(SnapshotStore(xss_home, "xss-test"))
                try:
                    xss_context = browser.new_context()
                    xss_page = xss_context.new_page()
                    xss_page.on("request", lambda request: external_requests.append("external-xss-request")
                                if urlparse(request.url).netloc not in {"", xss_server.expected_host} else None)
                    xss_page.goto(xss_server.origin)
                    xss_page.locator("#code").fill(xss_server.state.launch_code)
                    xss_page.locator("#login-form button").click()
                    expect(xss_page.locator("#text-document mark")).to_have_text(malicious)
                    if not xss_page.evaluate("window.XSS_EXECUTED === undefined"):
                        raise RuntimeError("source markup executed")
                    expect(xss_page.locator("#text-document img, #text-document script")).to_have_count(0)
                    checks.append("hostile source HTML is literal and cannot execute or fetch")
                    xss_context.close()
                finally:
                    xss_server.shutdown(); xss_server.server_close(); xss_thread.join(timeout=3)
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
    if external_requests: failures.append("unexpected external asset request")
    if console_errors: failures.append("browser console errors")
    checks.append("no external asset requests")
    result = {"status": "PASS" if not failures else "FAIL", "checks_passed": len(checks), "checks": checks,
              "external_request_count": len(external_requests), "console_error_count": len(console_errors),
              "failures": failures, "customer_data_used": False, "native_ao_lore_adapter_tested": False,
              "browser_os_sandbox_assessed": False}
    (args.output_dir / "browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        blocked = "ERR_BLOCKED_BY_ADMINISTRATOR" in str(error)
        result = {"status": "BLOCKED" if blocked else "FAIL", "test_kind": "real-browser-http-e2e",
                  "reason": "managed_browser_navigation_denied" if blocked else "browser_test_failed",
                  "checks_passed": 0, "real_browser_http_e2e": False, "customer_data_used": False,
                  "note": "No browser policy was changed or bypassed. Run this gate on the target workstation."}
        if "--output-dir" in sys.argv:
            destination = Path(sys.argv[sys.argv.index("--output-dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        raise SystemExit(2 if blocked else 1)
