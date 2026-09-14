#!/usr/bin/env python3
"""Offline browser-component check with in-memory API responses, NOT an HTTP E2E test.

Useful when a managed browser forbids loopback navigation. It does not change/bypass
browser network policy, visit a blocked URL or proxy HTTP. Real HTTP remains a separate gate.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ao_lore.source_viewer.assets import HTML, CSS, JAVASCRIPT
from ao_lore.source_viewer.contracts import ViewerError, digest
from ao_lore.source_viewer.demo import PDF_EXCERPT, TEXT_EXCERPT, DOCX_EXCERPT, demo_material, synthetic_record
from ao_lore.source_viewer.server import ViewerState, OPEN_PATH
from ao_lore.source_viewer.store import SnapshotStore, provision_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright, expect
    checks, errors, network = [], [], []
    with tempfile.TemporaryDirectory(prefix="ao-lore-viewer-component-") as temporary:
        root = Path(temporary)
        manifest, originals = demo_material()
        malicious = '<script>window.XSS_EXECUTED=true</script><img src="https://invalid.example/x" onerror="window.XSS_EXECUTED=true">'
        payload = malicious.encode()
        manifest["records"].append(synthetic_record(payload, "text", malicious, source_id="literal-markup-security-test"))
        originals[digest(payload)] = payload
        provision_snapshot(root, manifest, originals, grant_id="component-test")
        state = ViewerState(SnapshotStore(root, "component-test"))
        code = state.launch_code
        def fixture_request(source, path, method, body, authorization):
            # Direct calls on the synthetic component; no HTTP request occurs here.
            try:
                content = "application/json"
                if path == "/api/session" and method == "POST":
                    result = {"session_token": state.login(json.loads(body)["launch_code"])}
                else:
                    session = state.authenticate(authorization)
                    if path == "/api/evidence": result = state.cards(session)
                    elif path == "/api/resolve" and method == "POST": result = state.resolve(session, json.loads(body))
                    elif path == "/api/logout" and method == "POST": state.logout(session); result = {"status":"session_ended"}
                    elif match := OPEN_PATH.fullmatch(path):
                        handle, action, page = match.groups()
                        if page:
                            return {"status":200,"content_type":"image/png","binary":base64.b64encode(state.page(session,handle,int(page))).decode()}
                        if action == "text": result = state.text(session,handle)
                        else: raise ViewerError("route_unavailable",404)
                    else: raise ViewerError("route_unavailable",404)
                return {"status":200,"content_type":content,"text":json.dumps(result)}
            except ViewerError as error:
                return {"status":error.status,"content_type":"application/json","text":json.dumps({"error":error.code})}
        with sync_playwright() as playwright:
            launch = {"headless":True}
            browser_path = next((shutil.which(name) for name in (
                "chromium", "google-chrome-stable", "google-chrome"
            ) if shutil.which(name)), None)
            if browser_path: launch["executable_path"] = browser_path
            if hasattr(os,"geteuid") and os.geteuid()==0: launch["args"]=["--no-sandbox"]
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(viewport={"width":1440,"height":1080},device_scale_factor=1)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: network.append(request.url) if request.url.startswith(("http:","https:")) else None)
            markup = HTML.replace('<link rel="stylesheet" href="/style.css">','').replace('<script src="/app.js" defer></script>','')
            page.set_content(markup)
            page.add_style_tag(content=CSS)
            page.expose_binding("fixtureRequest",fixture_request)
            page.evaluate('''() => { window.fetch = async (path, options={}) => {
                const result = await window.fixtureRequest(path, options.method || 'GET', options.body || null,
                    (options.headers || {}).Authorization || null);
                const body = result.binary ? Uint8Array.from(atob(result.binary), c=>c.charCodeAt(0)) : result.text;
                return new Response(body, {status:result.status,headers:{'Content-Type':result.content_type}});
            }; }''')
            page.add_script_tag(content=JAVASCRIPT)
            expect(page.locator("#workspace")).to_be_hidden(); checks.append("empty private DOM before login")
            page.locator("#code").fill(code); page.locator("#login-form button").click()
            expect(page.locator("#excerpt")).to_have_text(PDF_EXCERPT,timeout=30000)
            expect(page.locator("#paper")).to_be_visible(timeout=30000)
            page.wait_for_function("document.getElementById('page-image').naturalWidth > 0")
            expect(page.locator("#page")).to_have_value("2")
            expect(page.locator("#highlights .highlight")).to_have_count(1)
            checks += ["one-use session flow with synthetic API", "PDF physical page 2", "exact excerpt unchanged", "exact text highlight overlay"]
            page.screenshot(path=str(args.output_dir / "viewer-pdf.png"),full_page=True)
            page.locator("#previous").click(); expect(page.locator("#page")).to_have_value("1",timeout=30000)
            expect(page.locator("#highlights .highlight")).to_have_count(0)
            checks += ["previous-page navigation", "no false highlight on uncited page"]
            page.locator("#next").click(); expect(page.locator("#page")).to_have_value("2",timeout=30000)
            expect(page.locator("#highlights .highlight")).to_have_count(1); checks.append("citation highlight restored")
            page.locator("#cards button").nth(1).click()
            expect(page.locator("#text-document mark")).to_have_text(TEXT_EXCERPT)
            expect(page.locator("#pagination")).to_be_hidden(); checks += ["text highlight", "text has no fake PDF pagination"]
            page.screenshot(path=str(args.output_dir / "viewer-text.png"),full_page=True)
            page.locator("#cards button").nth(2).click()
            expect(page.locator("#text-document mark")).to_have_text(DOCX_EXCERPT)
            expect(page.locator("#document-caption")).to_contain_text("not Word pagination")
            expect(page.locator("#metadata")).to_contain_text("demo-reference")
            expect(page.locator("#download")).to_be_hidden()
            checks += ["DOCX paragraph highlight", "DOCX layout limitation visible", "reference origin retained", "download denied by default"]
            page.locator("#cards button").nth(3).click()
            expect(page.locator("#text-document mark")).to_have_text(malicious)
            if not page.evaluate("window.XSS_EXECUTED === undefined"):
                raise RuntimeError("source markup executed")
            expect(page.locator("#text-document script, #text-document img")).to_have_count(0)
            checks.append("hostile source markup stays literal and inactive")
            if context.cookies():
                raise RuntimeError("browser session cookie created")
            checks.append("no session cookies")
            page.locator("#logout").click(); expect(page.locator("#workspace")).to_be_hidden()
            expect(page.locator("#excerpt")).to_have_text(""); checks.append("logout clears evidence DOM")
            browser.close()
    result = {"status":"PASS" if not errors and not network else "FAIL", "test_kind":"offline-browser-component",
              "checks_passed":len(checks),"checks":checks,"browser_page_errors":errors,"external_request_count":len(network),
              "real_browser_http_e2e":False,"http_csp_enforcement_tested":False,"customer_data_used":False,
              "note":"about:blank component test with synthetic in-memory responses; not a bypass of managed loopback policy"}
    (args.output_dir / "browser-component-results.json").write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2)); return 0 if result['status']=='PASS' else 1


if __name__=="__main__": raise SystemExit(main())
