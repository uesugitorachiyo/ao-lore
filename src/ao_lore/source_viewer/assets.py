"""Self-contained browser assets; no templates, third-party JavaScript or network assets."""

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AO Lore · Source viewer</title><link rel="stylesheet" href="/style.css"><script src="/app.js" defer></script></head>
<body><header><div class="brand"><span class="brandmark">AO</span><div>LORE<span class="subbrand">SOURCE VIEWER / PILOT</span></div></div>
<div class="local"><span class="dot"></span>LOCAL ONLY<span class="separator">|</span>Read-only source access</div></header>
<main><section id="login" class="login"><div class="eyebrow">EXPLICIT OPERATOR ACCESS</div><h1>Original source evidence.</h1>
<p>Inspect the original document beside its exact cited passage. No source paths, cloud services or changes to your knowledge base.</p>
<form id="login-form"><label for="code">One-use code from your terminal</label><input id="code" type="password" autocomplete="off" spellcheck="false" required maxlength="64"><button type="submit">Open local workspace</button></form>
<p class="fine">The code expires after two minutes. Session credentials remain only in this tab's memory. Reloading requires restarting the viewer for a new code.</p>
</section><p id="error" role="alert" hidden></p>
<section id="workspace" hidden><div class="workspace-title"><div><div class="eyebrow">EVIDENCE WORKSPACE</div><h1 id="workspace-name"></h1></div><button id="logout" class="secondary">End session</button></div>
<div class="notice"><strong>Operator-approved snapshot</strong><span>Original bytes are digest-checked. Native AO Lore registry and IR/block revalidation are not implemented in this pilot.</span></div>
<div class="layout"><nav class="evidence-nav" aria-label="Evidence"><div class="section-label">SOURCES <span id="count"></span></div><div id="cards"></div><p class="nav-note">Each selection retains its originating workspace and document generation.</p></nav>
<section class="document-panel" aria-label="Original document"><div class="toolbar"><div><span id="format" class="format"></span><strong id="source-name">Select evidence</strong></div>
<div id="pagination" hidden><button id="previous" class="icon" aria-label="Previous page">←</button><label for="page">Page</label><input id="page" type="number" min="1" value="1" aria-label="Physical PDF page"><span id="page-total"></span><button id="next" class="icon" aria-label="Next page">→</button></div></div>
<div class="document-caption" id="document-caption">The original source will appear here.</div>
<div id="page-scroll"><div id="paper" hidden><img id="page-image" alt="Original PDF page"><div id="highlights" aria-hidden="true"></div></div><pre id="text-document" hidden></pre><p id="empty">Select a source to inspect its evidence.</p></div></section>
<aside class="citation-panel" aria-label="Citation details"><div class="section-label">CITED EVIDENCE</div><div id="verified" class="verified" hidden>✓ Original bytes verified</div><p id="highlight-status" class="status"></p><blockquote id="excerpt"></blockquote>
<dl id="metadata"></dl><details><summary>Evidence identities &amp; source span</summary><dl id="identities"></dl></details>
<p class="fine">An original-byte match verifies the retained file version, not the truth of its contents or the correctness of an answer.</p><button id="download" class="secondary" hidden>Save approved original</button></aside></div>
</section></main><footer>AO Lore source-viewer pilot<span>No provider calls · No remote assets · No knowledge promotion</span></footer></body></html>'''

JAVASCRIPT = r'''"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let token = "", active = null, imageUrl = null, version = 0, currentPage = 1;
  const labels = {
    exact_unique_text: "Exact passage highlighted", exact_verified_span: "Exact source span highlighted",
    ambiguous_exact_match: "Highlight unavailable: more than one exact match",
    exact_match_unavailable: "Highlight unavailable: excerpt did not match the page text exactly",
    highlight_geometry_unavailable: "Highlight unavailable: exact geometry could not be established",
    no_cited_excerpt_on_this_page: "No cited passage is asserted on this page",
    not_verified: "PDF rendering dependency is not available",
  };
  function errorMessage(code) { return String(code || "request_failed").replaceAll("_", " "); }
  function clearImage() { if (imageUrl) { URL.revokeObjectURL(imageUrl); imageUrl = null; } $("page-image").removeAttribute("src"); $("highlights").replaceChildren(); }
  function clearPrivate() {
    token = ""; active = null; version++; clearImage();
    for (const id of ["cards", "excerpt", "metadata", "identities", "text-document", "workspace-name", "source-name", "format", "document-caption", "highlight-status", "page-total", "count"]) $(id).replaceChildren();
    $("workspace").hidden = true; $("login").hidden = false; $("code").value = "";
  }
  function showError(error) { $("error").textContent = error.message || "Request rejected."; $("error").hidden = false; }
  async function request(path, body) {
    const headers = {};
    if (token) headers.Authorization = "Bearer " + token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers,
      body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "omit", redirect: "error"});
    if (!response.ok) {
      const data = await response.json().catch(() => ({error: "request_failed"}));
      if (response.status === 401 || response.status === 403) clearPrivate();
      throw new Error(errorMessage(data.error) + "." + (data.error === "open_handle_unavailable" ? " Select the source again." : ""));
    }
    return response;
  }
  async function json(path, body) { return (await request(path, body)).json(); }
  function term(list, name, value) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = name; dd.textContent = value; list.append(dt, dd);
  }
  function status(value) { $("highlight-status").textContent = labels[value] || errorMessage(value); }
  function boxes(display, page) {
    $("highlights").replaceChildren();
    if (page !== display.cited_page) { status("no_cited_excerpt_on_this_page"); return; }
    status(display.highlight.status);
    for (const [left, top, width, height] of display.highlight.boxes) {
      const box = document.createElement("span"); box.className = "highlight";
      // Numeric properties come from renderer-owned geometry, not source HTML/CSS.
      box.style.left = (left * 100) + "%"; box.style.top = (top * 100) + "%";
      box.style.width = (width * 100) + "%"; box.style.height = (height * 100) + "%";
      $("highlights").append(box);
    }
  }
  async function loadPage(number, stamp) {
    const selected = active;
    if (!selected || !selected.display.page_count || number < 1 || number > selected.display.page_count) return;
    $("document-caption").textContent = "Loading original PDF page…";
    const response = await request("/api/open/" + selected.open_id + "/page/" + number);
    const blob = await response.blob();
    if (stamp !== version || selected !== active) return;
    clearImage(); imageUrl = URL.createObjectURL(blob); $("page-image").src = imageUrl;
    $("paper").hidden = false; $("empty").hidden = true;
    currentPage = number; $("page").value = number;
    $("previous").disabled = number <= 1; $("next").disabled = number >= selected.display.page_count;
    const citation = selected.display.cited_page;
    $("document-caption").textContent = "Original PDF · Physical page " + number + " of " + selected.display.page_count +
      (citation ? " · Cited page " + citation : " · No citation page supplied; no page match asserted");
    boxes(selected.display, number);
  }
  async function select(card, button) {
    const stamp = ++version;
    $("error").hidden = true; clearImage(); $("paper").hidden = true; $("text-document").hidden = true;
    $("empty").hidden = false; $("empty").textContent = "Verifying original source…";
    $("excerpt").textContent = ""; $("metadata").replaceChildren(); $("identities").replaceChildren();
    $("verified").hidden = true; $("pagination").hidden = true; $("download").hidden = true;
    active = null;
    for (const item of $("cards").children) { item.classList.remove("selected"); item.removeAttribute("aria-current"); }
    button.classList.add("selected"); button.setAttribute("aria-current", "true");
    try {
      const result = await json("/api/resolve", {workspace_id: card.workspace_id,
        generation_digest: card.generation_digest, evidence_id: card.evidence_id});
      if (stamp !== version) return;
      active = result;
      const e = result.evidence, display = result.display;
      $("format").textContent = display.format.toUpperCase(); $("source-name").textContent = e.source_id;
      $("excerpt").textContent = e.render_text; $("verified").hidden = false;
      term($("metadata"), "Origin workspace", e.workspace_id);
      term($("metadata"), "Authority role", errorMessage(e.authority_role));
      term($("metadata"), "Freshness at snapshot", e.freshness_status === "current" ? "Current at snapshot" : e.freshness_status.toUpperCase() + " — historical/qualified evidence");
      term($("metadata"), "Whole-original sensitivity", result.original_sensitivity);
      term($("metadata"), "Qualifications", e.qualification_codes.length ? e.qualification_codes.join(", ") : "None recorded");
      for (const key of ["evidence_id", "generation_digest", "document_id", "document_ir_digest", "source_digest", "block_id", "block_digest"]) term($("identities"), errorMessage(key), e[key]);
      term($("identities"), "Retained source span", JSON.stringify(e.source_span));
      $("download").hidden = !result.original_download_allowed;
      status(display.highlight.status);
      if (display.format === "pdf") {
        if (display.render_status !== "available") {
          $("empty").textContent = "PDF renderer unavailable. Install requirements-source-viewer.txt in the viewer environment.";
          $("document-caption").textContent = "Original bytes verified; page/geometry not verified"; return;
        }
        $("pagination").hidden = false; $("page-total").textContent = "/ " + display.page_count;
        $("page").max = display.page_count;
        await loadPage(display.cited_page || 1, stamp);
      } else {
        const projection = await json("/api/open/" + result.open_id + "/text");
        if (stamp !== version) return;
        const target = $("text-document"), s = projection.segments;
        target.replaceChildren(document.createTextNode(s.before));
        if (s.match) { const mark = document.createElement("mark"); mark.textContent = s.match; target.append(mark); }
        target.append(document.createTextNode(s.after)); target.hidden = false; $("empty").hidden = true;
        $("document-caption").textContent = projection.display_label; status(s.status);
        target.querySelector("mark")?.scrollIntoView({block: "nearest"});
      }
    } catch (error) { if (stamp === version) { $("empty").textContent = "Source could not be opened. Select it again after resolving the reported condition."; showError(error); } else showError(error); }
  }
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("error").hidden = true;
    try {
      const result = await json("/api/session", {launch_code: $("code").value.trim()});
      token = result.session_token; $("code").value = "";
      const listing = await json("/api/evidence");
      $("workspace-name").textContent = listing.primary_workspace_id; $("count").textContent = listing.evidence.length;
      $("cards").replaceChildren();
      for (const card of listing.evidence) {
        const button = document.createElement("button"); button.className = "evidence-card";
        const kind = document.createElement("span"); kind.className = "card-format"; kind.textContent = card.format.toUpperCase();
        const title = document.createElement("strong"); title.textContent = card.source_id;
        const origin = document.createElement("small"); origin.textContent = card.workspace_id;
        const state = document.createElement("small"); state.textContent = card.sensitivity + " · " + card.freshness_status;
        button.append(kind, title, origin, state); button.addEventListener("click", () => select(card, button)); $("cards").append(button);
      }
      $("login").hidden = true; $("workspace").hidden = false;
      $("cards").firstElementChild?.click();
    } catch (error) { showError(error); }
  });
  $("previous").addEventListener("click", () => loadPage(currentPage - 1, ++version).catch(showError));
  $("next").addEventListener("click", () => loadPage(currentPage + 1, ++version).catch(showError));
  $("page").addEventListener("change", () => loadPage(Number($("page").value), ++version).catch(showError));
  $("logout").addEventListener("click", async () => { try { await json("/api/logout", {}); } catch (error) { showError(error); } finally { clearPrivate(); } });
  $("download").addEventListener("click", async () => {
    if (!active) return;
    const selected = active;
    try {
      const response = await request("/api/open/" + selected.open_id + "/original");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a"); anchor.href = url;
      anchor.download = "source." + (selected.display.format === "text" ? "txt" : selected.display.format);
      anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error); }
  });
  window.addEventListener("pagehide", clearPrivate);
})();'''

CSS = r'''
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#142337;background:#f3f5f7;font-size:14px;line-height:1.5}
*{box-sizing:border-box}body{margin:0}[hidden]{display:none!important}header{background:#122437;color:#fff;height:86px;display:flex;justify-content:space-between;align-items:center;padding:0 36px}
.brand{display:flex;align-items:center;gap:12px;font-size:20px;font-weight:750;letter-spacing:2px}.brandmark{border:1px solid #69808e;border-radius:9px;padding:9px 10px;font-size:18px}.subbrand{display:block;font-size:9px;color:#c1d2db;letter-spacing:2.1px;font-weight:500}.local{display:flex;align-items:center;gap:10px;font-size:11px;letter-spacing:.5px;color:#d4e0e7}.dot{background:#79d4b4;width:7px;height:7px;border-radius:50%}.separator{margin:0 8px;color:#738896}main{padding:27px 32px 0;max-width:1800px;margin:auto}h1{margin:3px 0 7px;font-weight:650;letter-spacing:-.6px;font-size:26px}.eyebrow,.section-label{font-size:10px;font-weight:750;letter-spacing:1.6px;color:#63758a}.workspace-title{display:flex;align-items:center;justify-content:space-between}.workspace-title h1{font-size:25px}.notice{display:flex;gap:15px;background:#e8eff4;border:1px solid #d3e0e9;border-radius:6px;padding:11px 15px;font-size:12px;margin:17px 0 20px}.notice strong{white-space:nowrap;color:#274761}.notice span{color:#526678}.layout{display:grid;grid-template-columns:224px minmax(300px,1fr) 302px;gap:18px;align-items:start}.section-label{margin-bottom:15px}.section-label span{margin-left:8px;border-radius:12px;background:#e3e9ee;padding:2px 7px}.evidence-nav{padding-top:10px}.evidence-card{display:block;width:100%;text-align:left;border:1px solid #d9e0e5;background:#fff;color:#25394d;margin-bottom:10px;border-radius:7px;padding:15px 14px;font:inherit;cursor:pointer}.evidence-card strong{display:block;overflow-wrap:anywhere;font-size:13px;margin:5px 0}.evidence-card small{display:block;color:#607386;font-size:11px}.evidence-card:hover{border-color:#88a4bb}.evidence-card.selected{background:#edf4f9;border-color:#3c789f;box-shadow:inset 3px 0 0 #2d719f}.card-format{font-size:9px;letter-spacing:1px;color:#2f7395;font-weight:750}.nav-note{color:#748495;font-size:11px;line-height:1.6;padding:5px 7px}.document-panel,.citation-panel{background:#fff;border:1px solid #dbe2e7;border-radius:8px;overflow:hidden}.toolbar{display:flex;align-items:center;justify-content:space-between;padding:16px 16px;border-bottom:1px solid #e4e9ee;gap:12px;min-height:64px}.toolbar>div:first-child{display:flex;align-items:center;gap:8px;min-width:0}.toolbar strong{font-size:12px;overflow-wrap:anywhere}.format{font-size:9px;border:1px solid #cddde7;border-radius:4px;padding:2px 5px;letter-spacing:.4px;color:#387295}#pagination{display:flex;align-items:center;gap:7px;white-space:nowrap;font-size:11px;color:#576b7d}#page{width:43px;font:inherit;border:1px solid #cdd8e1;border-radius:4px;padding:4px}button{font:inherit;border:0;cursor:pointer;background:#236787;color:#fff;border-radius:5px;padding:10px 15px;font-weight:600}button:disabled{opacity:.35;cursor:default}button:focus-visible,input:focus-visible{outline:3px solid #88bde1;outline-offset:2px}.secondary{background:#fff;border:1px solid #c8d5df;color:#33526b;font-size:12px;padding:8px 12px}.icon{background:#eef2f6;border:0;color:#46617a;padding:4px 8px;font-size:14px}.document-caption{padding:10px 16px;font-size:10px;color:#697d90;background:#fcfdfe;border-bottom:1px solid #e9eef2}#page-scroll{height:660px;overflow:auto;background:#e7ebef;padding:22px}#paper{position:relative;width:100%;box-shadow:0 2px 13px #2031491b;background:white}#page-image{display:block;width:100%;height:auto}#highlights{position:absolute;inset:0;pointer-events:none}.highlight{position:absolute;background:#ffe26473;outline:1px solid #eebc49b0;mix-blend-mode:multiply}#text-document{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.9;font:14px/1.9 ui-monospace,SFMono-Regular,Consolas,monospace;background:#fff;border:1px solid #d6dee5;border-radius:4px;padding:28px;min-height:400px;margin:0}mark{background:#ffed9c;color:inherit}#empty{font-size:13px;text-align:center;color:#65798c;padding:90px 25px}.citation-panel{padding:20px 20px 23px}.verified{display:inline-block;border:1px solid #c2dfd3;background:#edf8f2;border-radius:4px;padding:4px 7px;font-size:10px;font-weight:650;color:#286c4c}.status{font-size:11px;line-height:1.5;color:#708192;margin:10px 0 14px}blockquote{font-size:16px;line-height:1.7;margin:0 0 23px;padding:0 0 0 13px;border-left:3px solid #2d7399;color:#1d3c55;white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}dl{margin:0}dt{color:#7c8b99;font-size:10px;margin-top:13px}dd{margin:2px 0 0;font-size:12px;overflow-wrap:anywhere;color:#294359}details{margin-top:22px;border-top:1px solid #e2e8ed;padding-top:16px}summary{cursor:pointer;color:#45657f;font-size:11px;font-weight:600}#identities dd{font-family:ui-monospace,monospace;font-size:10px}.fine{font-size:11px;line-height:1.65;color:#7a8996;margin-top:21px}footer{max-width:1800px;margin:24px auto 0;padding:0 32px 22px;color:#7c8b97;font-size:10px;display:flex;justify-content:space-between;gap:20px}.login{max-width:600px;margin:55px auto 80px;padding:35px;background:#fff;border:1px solid #dce4ea;border-radius:10px}.login h1{font-size:32px;margin-top:8px}.login>p{color:#61768a;line-height:1.75}.login form{margin-top:28px}.login label{display:block;font-size:12px;color:#4a6378;margin-bottom:8px}.login input{display:block;width:100%;padding:12px;border:1px solid #bccdd9;border-radius:5px;margin-bottom:12px}.login button{width:100%}#error{padding:13px 18px;color:#8d3434;background:#fff1ef;border:1px solid #edc4bd;border-radius:5px}
@media(min-width:1550px){.layout{grid-template-columns:245px minmax(400px,1fr) 340px}#page-scroll{height:760px}}@media(max-width:1050px){.layout{grid-template-columns:190px minmax(300px,1fr)}.citation-panel{grid-column:2}.notice{display:block}.notice span{display:block;margin-top:5px}.toolbar{flex-wrap:wrap}}@media(max-width:680px){header{padding:0 18px}.local{display:none}main{padding:20px 14px 0}.layout{display:block}.evidence-nav{margin-bottom:20px}.citation-panel{margin-top:18px}#page-scroll{height:500px;padding:12px}.workspace-title h1{font-size:20px}footer{padding:0 14px 20px;display:block}.login{margin-top:20px;padding:24px}.notice{font-size:11px}}
'''
