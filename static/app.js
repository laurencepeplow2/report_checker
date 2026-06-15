/* Report Checker UI
 * Review: cycle chunks that have red/amber breaches - extract | rules | suggestion
 * (with word-level diff highlighting and manual "checked" progress boxes).
 * Document health: flag totals, broken links, overused words (AI-flagged
 * style words), story. Green flags are excluded from the UI entirely.
 *
 * Data comes from /api/* normally, or from window.__RC_DATA__ when running
 * as an exported self-contained report.
 */

const FLAG_ORDER = { r: 0, a: 1 };
const el = (id) => document.getElementById(id);

let docId = "";
let currentDoc = "";  // ?doc= value for API calls ("" = default report)
let allChunks = [];   // chunks with >=1 breach, results filtered to r/a
let view = [];
let index = 0;
let checked = new Set();  // chunk_ids marked as manually checked
let editMode = false;
let editsByChunk = {};    // chunk_id -> {at, text}: committed (locked) edits
let editorChunkId = null; // which chunk the editor is currently seeded with
let hunks = [];           // tracked-changes between original and suggestion
let editorDirty = false;  // true once the Final text box is hand-edited
let showDiff = false;     // suggestion panel: clean text vs word diff

/* ---------------- persistence for "checked" boxes ---------------- */

function storageKey() { return `rc-checked-${docId}`; }

function loadChecked() {
  try {
    checked = new Set(JSON.parse(localStorage.getItem(storageKey()) || "[]"));
  } catch { checked = new Set(); }
}

function saveChecked() {
  try { localStorage.setItem(storageKey(), JSON.stringify([...checked])); } catch {}
}

/* ---------------- data load ---------------- */

async function fetchJson(path, embeddedKey) {
  if (window.__RC_DATA__ && window.__RC_DATA__[embeddedKey]) {
    return window.__RC_DATA__[embeddedKey];
  }
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

async function load() {
  // Exports are read-only snapshots: no live edits possible.
  if (window.__RC_DATA__) el("edit-mode-btn").hidden = true;
  // Report selector: the config's report_link can hold several documents.
  // Hidden for exports (single embedded report) and single-report setups.
  if (!window.__RC_DATA__) {
    let reportList = [];
    try { reportList = await fetchJson("/api/reports", "reports"); } catch {}
    const sel = el("report-select");
    if (reportList.length > 1) {
      sel.hidden = false;
      for (const r of reportList) {
        const opt = document.createElement("option");
        opt.value = r.doc_id;
        opt.textContent = r.title + (r.mode ? ` (${r.mode})` : "");
        sel.appendChild(opt);
      }
      const saved = localStorage.getItem("rc-report");
      if (saved && reportList.some((r) => r.doc_id === saved)) sel.value = saved;
      sel.addEventListener("change", () => {
        localStorage.setItem("rc-report", sel.value);
        loadReport(sel.value);
      });
      await loadReport(sel.value);
      return;
    }
  }
  await loadReport("");
}

async function loadReport(doc) {
  currentDoc = doc || "";
  const query = doc ? `?doc=${encodeURIComponent(doc)}` : "";
  editsByChunk = {};
  if (!window.__RC_DATA__) {
    try { editsByChunk = await fetchJson(`/api/edits${query}`, "edits"); } catch {}
  }
  let runData = null;
  try {
    runData = await fetchJson(`/api/run-data${query}`, "run");
  } catch {
    document.querySelectorAll(".view").forEach((v) => (v.hidden = true));
    const empty = el("empty-state");
    empty.hidden = false;
    empty.innerHTML = "No run data found. Run <code>test_run.py</code> first.";
    return;
  }
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-review"));
  document.querySelectorAll(".nav-pill").forEach((p) =>
    p.classList.toggle("active", p.dataset.view === "review"));
  el("empty-state").hidden = true;

  docId = runData.doc_id || "";
  loadChecked();
  const docType = (runData.document_type || "")
    .replace(/^./, (c) => c.toUpperCase());
  el("doc-meta").textContent =
    `${runData.title} - ${docType} - Severity: ${runData.severity}`
    + (runData.run_date ? ` - Date: ${runData.run_date}` : "");

  // a breach counts only if it's red/amber AND not refuted by the second pass
  const isLiveBreach = (r) =>
    (r.flag === "r" || r.flag === "a") && r.verdict !== "refuted";
  allChunks = runData.chunks
    .map((c) => ({ ...c, breaches: c.results.filter(isLiveBreach) }))
    .filter((c) => c.breaches.length > 0);

  // Section toggle: report tabs in document order (rebuilt per report)
  const sectionSelect = el("section-select");
  while (sectionSelect.options.length > 1) sectionSelect.remove(1);
  sectionSelect.value = "all";
  const seen = new Set();
  for (const c of runData.chunks) {
    if (!seen.has(c.tab)) {
      seen.add(c.tab);
      const opt = document.createElement("option");
      opt.value = c.tab;
      opt.textContent = c.tab;
      sectionSelect.appendChild(opt);
    }
  }

  // Flag totals for the health view (refuted flags excluded)
  let reds = 0, ambers = 0;
  for (const c of runData.chunks) {
    for (const r of c.results) {
      if (r.verdict === "refuted") continue;
      if (r.flag === "r") reds++;
      if (r.flag === "a") ambers++;
    }
  }
  el("stat-red").textContent = String(reds);
  el("stat-amber").textContent = String(ambers);

  el("level-select").value = "all";
  applyFilter();

  try {
    renderHealth(await fetchJson(`/api/analysis${query}`, "analysis"));
  } catch { /* health analyses not run yet */ }
}

/* ---------------- review ---------------- */

function applyFilter() {
  const level = el("level-select").value;
  const section = el("section-select").value;
  view = allChunks.filter((c) =>
    (level === "all" || c.input_level === level) &&
    (section === "all" || c.tab === section)
  );
  index = 0;
  render();
}

function updateProgress() {
  const done = allChunks.filter((c) => checked.has(c.chunk_id)).length;
  el("progress-summary").textContent = `${done} / ${allChunks.length} checked`;
}

/* Render our inline formatting markup (**bold**, *italic*, <u>..</u>,
 * [text](url)) as safe HTML - everything else is escaped first. */
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function formattedHtml(text) {
  let s = escapeHtml(text);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (m, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(>])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/&lt;u&gt;([\s\S]*?)&lt;\/u&gt;/g, "<u>$1</u>");
  return s;
}

/* Word-level diff (LCS) between original and suggestion. */
function diffWords(a, b) {
  const A = a.split(/(\s+)/).filter((t) => t !== "");
  const B = b.split(/(\s+)/).filter((t) => t !== "");
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];  // {type: same|del|add, text}
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { ops.push({ type: "same", text: A[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: "del", text: A[i] }); i++; }
    else { ops.push({ type: "add", text: B[j] }); j++; }
  }
  while (i < n) { ops.push({ type: "del", text: A[i++] }); }
  while (j < m) { ops.push({ type: "add", text: B[j++] }); }
  return ops;
}

function renderDiff(container, original, suggestion) {
  container.innerHTML = "";
  for (const op of diffWords(original, suggestion)) {
    if (op.type === "same") {
      container.appendChild(document.createTextNode(op.text));
    } else if (op.type === "add") {
      const ins = document.createElement("ins");
      ins.className = "diff-add";
      ins.textContent = op.text;
      container.appendChild(ins);
    } else {
      const del = document.createElement("del");
      del.className = "diff-del";
      del.textContent = op.text;
      container.appendChild(del);
    }
  }
}

/* ---- tracked-changes accept/reject ---- */

// Group the word diff into "same" runs and "change" hunks (a contiguous
// original->proposed swap the reviewer can accept or reject).
function computeHunks(original, suggestion) {
  const ops = diffWords(original, suggestion);
  const out = [];
  let i = 0;
  while (i < ops.length) {
    if (ops[i].type === "same") {
      let t = "";
      while (i < ops.length && ops[i].type === "same") { t += ops[i].text; i++; }
      out.push({ type: "same", text: t });
    } else {
      let orig = "", prop = "";
      while (i < ops.length && ops[i].type !== "same") {
        if (ops[i].type === "del") orig += ops[i].text;
        else prop += ops[i].text;
        i++;
      }
      out.push({ type: "change", original: orig, proposed: prop, state: "accept" });
    }
  }
  return out;
}

// Text with each change resolved to its accepted/rejected side.
function resolvedText() {
  return hunks.map((h) =>
    h.type === "same" ? h.text : (h.state === "accept" ? h.proposed : h.original)
  ).join("");
}

function reseedEditorFromHunks() {
  el("suggestion-editor").innerHTML = escapeHtml(resolvedText());
}

function renderChangeReview() {
  const box = el("change-review");
  box.innerHTML = "";
  hunks.forEach((h) => {
    if (h.type === "same") {
      box.appendChild(document.createTextNode(h.text));
      return;
    }
    const span = document.createElement("span");
    span.className = "hunk " + (h.state === "accept" ? "accepted" : "rejected");
    if (h.original.trim()) {
      const old = document.createElement("span");
      old.className = "h-old";
      old.textContent = h.original.trim() + " ";
      span.appendChild(old);
    }
    if (h.proposed.trim()) {
      const neu = document.createElement("span");
      neu.className = "h-new";
      neu.textContent = h.proposed.trim();
      span.appendChild(neu);
    }
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "hunk-toggle";
    toggle.textContent = h.state === "accept" ? "✓" : "✗";
    toggle.title = h.state === "accept"
      ? "Change accepted - click to reject" : "Change rejected - click to accept";
    const flip = () => {
      h.state = h.state === "accept" ? "reject" : "accept";
      renderChangeReview();
      maybeReseed();
    };
    toggle.addEventListener("click", (e) => { e.stopPropagation(); flip(); });
    span.addEventListener("click", flip);
    span.appendChild(document.createTextNode(" "));
    span.appendChild(toggle);
    box.appendChild(span);
    box.appendChild(document.createTextNode(" "));
  });
  const hasChanges = hunks.some((h) => h.type === "change");
  el("change-review-wrap").style.display = hasChanges ? "" : "none";
}

// Re-seed the Final text box from the accepted changes, but never silently
// wipe manual formatting the reviewer has already applied.
function maybeReseed() {
  if (editorDirty && !window.confirm(
      "Rebuild the final text from the accepted changes? "
      + "Manual formatting will be lost.")) return;
  reseedEditorFromHunks();
  editorDirty = false;
}

// Wrap the first occurrence of `quote` inside the extract in a <mark>,
// whitespace-flexible and case-insensitive, preserving inline formatting.
function highlightInExtract(container, quote, flag, ruleId) {
  const q = (quote || "").trim();
  if (!q) return;
  const pattern = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
  let re;
  try { re = new RegExp(pattern, "i"); } catch { return; }
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (node.parentElement.closest("mark")) continue;
    const m = re.exec(node.nodeValue);
    if (!m) continue;
    const before = node.nodeValue.slice(0, m.index);
    const after = node.nodeValue.slice(m.index + m[0].length);
    const mark = document.createElement("mark");
    mark.className = `hl-${flag}`;
    if (ruleId) mark.dataset.rule = ruleId;   // links the highlight to its rule card
    mark.textContent = m[0];
    const frag = document.createDocumentFragment();
    if (before) frag.appendChild(document.createTextNode(before));
    frag.appendChild(mark);
    if (after) frag.appendChild(document.createTextNode(after));
    node.parentNode.replaceChild(frag, node);
    return;
  }
}

function render() {
  const content = el("chunk-content");
  const cards = el("issue-cards");
  const suggestion = el("suggestion-content");
  const location = el("chunk-location");
  content.innerHTML = "";
  cards.innerHTML = "";
  suggestion.innerHTML = "";
  location.innerHTML = "";
  el("chunk-meta").innerHTML = "";
  el("copy-btn").hidden = true;
  updateProgress();

  const box = el("reviewed-box");

  if (!view.length) {
    content.textContent = "No flagged chunks match the current filters.";
    content.classList.add("empty");
    el("editor-area").hidden = true;
    el("committed-badge").hidden = true;
    suggestion.hidden = false;
    suggestion.textContent = "Nothing to improve here.";
    suggestion.classList.add("empty");
    el("breach-count").textContent = "0";
    el("breach-count").classList.add("none");
    el("position").textContent = "0 / 0";
    box.checked = false;
    box.disabled = true;
    updateButtons();
    return;
  }
  content.classList.remove("empty");

  const chunk = view[index];

  box.disabled = false;
  box.checked = checked.has(chunk.chunk_id);
  el("extract-panel").classList.toggle("is-checked", box.checked);

  for (const text of [chunk.tab, chunk.input_level]) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = text;
    el("chunk-meta").appendChild(tag);
  }

  // Location: numbered section title (long heading-styled statements are
  // not real section titles) + approximate page + deep link to the doc
  const SECTION_NUM_RE = /^\s*(\d+(\.\d+)*|[IVXLCDM]+(\.\d+)+)[.)]?\s/i;
  const pieces = [];
  const lastHeading = chunk.heading_path?.length
    ? chunk.heading_path[chunk.heading_path.length - 1] : "";
  if (lastHeading && SECTION_NUM_RE.test(lastHeading)) pieces.push(lastHeading);
  if (chunk.approx_page) pieces.push(`≈ p.${chunk.approx_page}`);
  location.textContent = pieces.join("  ·  ");
  if (docId && chunk.tab_id) {
    const link = document.createElement("a");
    link.href = `https://docs.google.com/document/d/${docId}/edit?tab=${chunk.tab_id}`
      + (chunk.heading_id ? `#heading=${chunk.heading_id}` : "");
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open in Google Docs ↗";
    location.appendChild(document.createTextNode("  ·  "));
    location.appendChild(link);
  }

  if (chunk.image) {
    const img = document.createElement("img");
    img.src = chunk.image;
    img.alt = "Figure";
    content.appendChild(img);
    if (chunk.text) {
      const cap = document.createElement("div");
      cap.className = "caption muted";
      cap.textContent = chunk.text;
      content.appendChild(cap);
    }
  } else if (chunk.formatted_text && chunk.formatted_text !== chunk.text) {
    // show the report's bold / italic / underline / hyperlinks
    content.innerHTML = formattedHtml(chunk.formatted_text);
  } else {
    content.textContent = chunk.text;
  }

  const sorted = [...chunk.breaches].sort(
    (a, b) => (FLAG_ORDER[a.flag] ?? 9) - (FLAG_ORDER[b.flag] ?? 9)
  );
  el("breach-count").textContent = String(sorted.length);
  el("breach-count").classList.toggle("none", sorted.length === 0);

  // highlight the offending text (verifier / coded quote) inside the extract
  if (!chunk.image) {
    for (const result of sorted) {
      if (result.quote) highlightInExtract(content, result.quote, result.flag, result.rule_id);
    }
  }

  for (const result of sorted) {
    const card = document.createElement("div");
    card.className = `card flag-${result.flag}`;
    if (result.rule_id) card.dataset.rule = result.rule_id;
    if (result.verdict === "refuted") card.classList.add("refuted");
    card.innerHTML = `
      <span class="flag-chip">${result.flag}</span>
      <div class="card-body">
        <div class="category"></div>
        <div class="rule-text"></div>
        <div class="card-detail"></div>
      </div>`;
    const category = card.querySelector(".category");
    if (result.category) category.textContent = result.category;
    else category.remove();
    card.querySelector(".rule-text").textContent = result.rule;  // example excluded

    const detailEl = card.querySelector(".card-detail");
    if (result.detail) detailEl.textContent = result.detail;
    else detailEl.remove();
    cards.appendChild(card);
  }

  const committed = editsByChunk[chunk.chunk_id];
  const editorArea = el("editor-area");
  const badge = el("committed-badge");
  const diffToggle = el("diff-toggle");
  editorArea.hidden = true;
  badge.hidden = true;
  diffToggle.hidden = true;
  suggestion.hidden = false;

  if (committed) {
    // one change per paragraph: locked after a commit
    badge.hidden = false;
    badge.title = `committed ${committed.at || ""}`;
    suggestion.classList.remove("empty");
    suggestion.textContent = committed.text;
  } else if (chunk.input_level === "figure") {
    suggestion.classList.add("empty");
    suggestion.textContent = "Suggestions are not generated for figures - "
      + "use the breached rules on the left to fix the figure by hand.";
  } else if (editMode && chunk.suggestion) {
    // live-edit mode: accept/reject changes, then format + commit
    suggestion.hidden = true;
    editorArea.hidden = false;
    if (editorChunkId !== chunk.chunk_id) {
      hunks = computeHunks(chunk.text, chunk.suggestion);
      editorChunkId = chunk.chunk_id;
      editorDirty = false;
      renderChangeReview();
      reseedEditorFromHunks();
    }
  } else if (chunk.suggestion) {
    // default to the clean rewritten text; "Show changes" reveals the diff
    suggestion.classList.remove("empty");
    diffToggle.hidden = false;
    diffToggle.textContent = showDiff ? "Hide changes" : "Show changes";
    if (showDiff) renderDiff(suggestion, chunk.text, chunk.suggestion);
    else suggestion.textContent = chunk.suggestion;
    el("copy-btn").hidden = false;
  } else {
    suggestion.classList.add("empty");
    suggestion.textContent = "No suggestion generated for this chunk.";
  }

  el("position").textContent = `${index + 1} / ${view.length}`;
  updateButtons();
}

function updateButtons() {
  el("prev-btn").disabled = index <= 0;
  el("next-btn").disabled = index >= view.length - 1;
}

/* ---------------- document health ---------------- */

function renderHealth(data) {
  if (data.approx_pages_excl_annex) {
    const pages = data.approx_pages_excl_annex;
    const limit = data.page_limit || 0;
    const num = el("stat-pages");
    const flag = el("stat-pages-flag");
    num.classList.remove("good", "amber", "bad");
    flag.hidden = true;
    if (limit) {
      num.textContent = `${pages} / ${limit}`;
      el("stat-pages-label").textContent =
        `≈ pages, max for ${data.document_type || "this type"} (excl. annex)`;
      const over = pages - limit;
      if (over > 2) {
        num.classList.add("bad");
        flag.textContent = `${over} pages over the limit`;
        flag.className = "limit-flag bad";
        flag.hidden = false;
      } else if (over > 0) {
        num.classList.add("amber");
        flag.textContent = `${over} page${over > 1 ? "s" : ""} over the limit`;
        flag.className = "limit-flag amber";
        flag.hidden = false;
      } else {
        num.classList.add("good");
      }
    } else {
      num.textContent = String(pages);
      el("stat-pages-label").textContent = "≈ pages (excl. annex)";
    }
  }
  // Broken links (+ unverified ones that need a human click)
  const links = data.links || {};
  const flagged = (links.links || []).filter((l) => l.state !== "ok");
  const bignum = el("broken-count");
  bignum.textContent = String(links.broken_count ?? 0);
  bignum.classList.add(links.broken_count ? "bad" : "good");
  const unverified = links.unverified_count ?? 0;
  el("links-meta").textContent =
    `of ${links.unique_links ?? 0} unique links checked (${links.total_links ?? 0} total)` +
    (unverified ? ` - plus ${unverified} unverified` : "") + " - no AI involved";
  const list = el("broken-links");
  list.innerHTML = "";
  for (const link of flagged) {
    const row = document.createElement("div");
    row.className = "link-row";
    row.innerHTML = `<span class="status"></span><a target="_blank" rel="noopener"></a>
                     <div class="where"></div>`;
    const status = row.querySelector(".status");
    status.textContent = link.state === "unverified" ? `? ${link.status}` : link.status;
    status.classList.toggle("unverified", link.state === "unverified");
    const a = row.querySelector("a");
    a.href = link.url;
    a.textContent = link.url;
    const pageStr = (link.pages && link.pages.length)
      ? ` - ≈ p.${link.pages.join(", ")}` : "";
    row.querySelector(".where").textContent =
      `link text: "${link.text}" - in: ${link.tabs.join(", ")}${pageStr}` +
      (link.note ? ` - ${link.note}` : "");
    list.appendChild(row);
  }

  // Overused words (AI-flagged style words highlighted)
  const words = data.word_frequency || [];
  const max = words.length ? words[0].count : 1;
  const bars = el("word-bars");
  bars.innerHTML = "";
  for (const w of words.slice(0, 20)) {
    const row = document.createElement("div");
    row.className = "word-row" + (w.flagged ? " flagged" : "");
    row.innerHTML = `<span class="w"></span>
      <span class="bar-track"><span class="bar" style="width:${Math.round((w.count / max) * 100)}%"></span></span>
      <span class="n">${w.count}</span>`;
    const label = row.querySelector(".w");
    label.textContent = w.word;
    if (w.flagged) {
      label.title = w.reason || "AI flags this as a possible style issue";
      const dot = document.createElement("span");
      dot.className = "flag-dot";
      label.prepend(dot);
    }
    bars.appendChild(row);
  }

  // Sentence-length distribution (words per sentence, bucketed)
  const dist = data.sentence_lengths || [];
  const distMax = dist.reduce((m, d) => Math.max(m, d.count), 1);
  const distBox = el("sentence-dist");
  if (distBox) {
    distBox.innerHTML = "";
    for (const d of dist) {
      const row = document.createElement("div");
      row.className = "dist-row";
      row.innerHTML = `<span class="lbl"></span>
        <span class="bar-track"><span class="bar" style="width:${Math.round((d.count / distMax) * 100)}%"></span></span>
        <span class="n">${d.count}</span>`;
      row.querySelector(".lbl").textContent = d.label;
      distBox.appendChild(row);
    }
  }

  // a finding's location → "heading · ≈ p.N · Open in Google Docs ↗"
  const hdoc = data.doc_id || "";
  const locNode = (loc) => {
    const span = document.createElement("span");
    const bits = [];
    if (loc.heading) bits.push(loc.heading);
    if (loc.page) bits.push(`≈ p.${loc.page}`);
    span.appendChild(document.createTextNode(bits.join("  ·  ")));
    if (hdoc && loc.tab_id) {
      const a = document.createElement("a");
      a.href = `https://docs.google.com/document/d/${hdoc}/edit?tab=${loc.tab_id}`
        + (loc.heading_id ? `#heading=${loc.heading_id}` : "");
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Open in Google Docs ↗";
      span.appendChild(document.createTextNode(bits.length ? "  ·  " : ""));
      span.appendChild(a);
    }
    return span;
  };
  // items: [{text?, loc?}] - text is a plain prefix, loc renders the link line
  const makeCheck = (container, ok, title, items) => {
    const block = document.createElement("div");
    block.className = `health-check ${ok ? "ok" : "bad"}`;
    block.innerHTML = `<div class="check-title"><span class="check-icon">${ok ? "✓" : "✗"}</span> <span></span></div>`;
    block.querySelector(".check-title span:last-child").textContent = title;
    for (const item of items || []) {
      const row = document.createElement("div");
      row.className = "check-item";
      if (item.text) row.appendChild(document.createTextNode(item.text));
      if (item.loc) {
        if (item.text) row.appendChild(document.createTextNode("  ·  "));
        row.appendChild(locNode(item.loc));
      }
      block.appendChild(row);
    }
    container.appendChild(block);
  };

  // Figure layout checks
  const layout = data.figure_layout || {};
  const checks = el("layout-checks");
  checks.innerHTML = "";
  const multi = layout.multi_figure_subsections || [];
  makeCheck(checks, multi.length === 0,
    multi.length === 0 ? "Max one figure per sub-section"
      : `${multi.length} sub-section(s) with more than one figure`,
    multi.map((m) => ({ text: `${m.figures} figures`, loc: m })));
  const narrow = layout.narrow_figures || [];
  makeCheck(checks, narrow.length === 0,
    narrow.length === 0
      ? `Figures at full column width (column ≈ ${layout.column_width_pt || "?"}pt)`
      : `${narrow.length} figure(s) below full column width`,
    narrow.map((n) => ({ text: `${n.pct_of_column}% of column width`, loc: n })));
  const longFooters = layout.long_footers || [];
  makeCheck(checks, longFooters.length === 0,
    longFooters.length === 0 ? "Figure footers at most 2 lines"
      : `${longFooters.length} figure footer(s) over 2 lines`,
    longFooters.map((f) => ({ text: `${f.lines} lines: "${f.text}"`, loc: f })));

  // Formatting checks (footnotes/footers, justification)
  const fmt = data.formatting || {};
  const fc = el("format-checks");
  if (fc) {
    fc.innerHTML = "";
    const notes = (fmt.footnotes || 0) + (fmt.footers || 0);
    makeCheck(fc, notes === 0,
      notes === 0 ? "No footnotes or footers"
        : `${fmt.footnotes || 0} footnote(s), ${fmt.footers || 0} footer(s)`, []);
    const just = fmt.justified || [];
    makeCheck(fc, just.length === 0,
      just.length === 0 ? "Body text left-aligned (not justified)"
        : `${just.length} justified paragraph(s) - should be left-aligned`,
      just.map((j) => ({ loc: j })));
  }

  // Story flag (AI verdict on the heading sequence)
  const flagBox = el("story-flag");
  if (data.story_flag && data.story_flag.flag) {
    const f = data.story_flag.flag;
    flagBox.hidden = false;
    flagBox.className = `story-side story-verdict flag-${f}`;
    flagBox.innerHTML = `<span class="flag-chip">${f}</span><span class="verdict-text"></span>`;
    flagBox.querySelector(".verdict-text").textContent = data.story_flag.explanation || "";
  } else {
    flagBox.hidden = true;
  }

  // Story - each heading shows its per-title message flag (r/a/g)
  const story = el("story-list");
  story.innerHTML = "";
  const MSG_TITLE = { r: "no clear message", a: "partly clear message",
                      g: "clear message", none: "not assessed" };
  const MSG_ICON = { r: "✗", a: "?", g: "✓", none: "•" };
  for (const h of data.story || []) {
    const item = document.createElement("div");
    item.className = `story-item lvl-${Math.min(h.level, 3)}`;
    // every title gets a left-gutter r/a/g icon (tick = clear message,
    // ? = partly, cross = none) so the flags read as one consistent column
    const flag = (h.message_flag || "").toLowerCase();
    const cls = ["r", "a", "g"].includes(flag) ? flag : "none";
    const icon = document.createElement("span");
    icon.className = `msg-icon flag-${cls}`;
    icon.textContent = MSG_ICON[cls];
    icon.title = MSG_TITLE[cls];
    const txt = document.createElement("span");
    txt.className = "story-text";
    txt.textContent = h.text;
    item.appendChild(icon);
    item.appendChild(txt);
    story.appendChild(item);
  }
}

/* ---------------- nav + events ---------------- */

// Hovering a highlighted span in the extract emphasises the matching rule
// card(s) (and vice-versa), so it's clear which rule each highlight maps to.
function setRuleFocus(ruleId, on) {
  if (!ruleId) return;
  const esc = (window.CSS && CSS.escape) ? CSS.escape(ruleId) : ruleId;
  el("issue-cards").querySelectorAll(`.card[data-rule="${esc}"]`)
    .forEach((c) => c.classList.toggle("rule-focus", on));
  el("chunk-content").querySelectorAll(`mark[data-rule="${esc}"]`)
    .forEach((m) => m.classList.toggle("mark-focus", on));
}
for (const [host, sel] of [["chunk-content", "mark[data-rule]"],
                           ["issue-cards", ".card[data-rule]"]]) {
  el(host).addEventListener("mouseover", (e) => {
    const t = e.target.closest(sel);
    if (t) setRuleFocus(t.dataset.rule, true);
  });
  el(host).addEventListener("mouseout", (e) => {
    const t = e.target.closest(sel);
    if (t) setRuleFocus(t.dataset.rule, false);
  });
}

document.querySelectorAll(".nav-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    document.querySelectorAll(".nav-pill").forEach((p) => p.classList.remove("active"));
    pill.classList.add("active");
    el("view-review").hidden = pill.dataset.view !== "review";
    el("view-health").hidden = pill.dataset.view !== "health";
  });
});

el("prev-btn").addEventListener("click", () => { if (index > 0) { index--; render(); } });
el("next-btn").addEventListener("click", () => { if (index < view.length - 1) { index++; render(); } });
el("level-select").addEventListener("change", applyFilter);
el("section-select").addEventListener("change", applyFilter);
el("reviewed-box").addEventListener("change", (e) => {
  const chunk = view[index];
  if (!chunk) return;
  if (e.target.checked) checked.add(chunk.chunk_id);
  else checked.delete(chunk.chunk_id);
  saveChecked();
  el("extract-panel").classList.toggle("is-checked", e.target.checked);
  updateProgress();
});
/* ---------------- editor mode: live edits to the Google Doc ---------- */

const EDIT_CAUTION =
  "Caution: you can now make live changes to your document, please check "
  + "the changes in a browser on a separate screen.";

el("edit-mode-btn").addEventListener("click", () => {
  if (!editMode) {
    if (!window.confirm(EDIT_CAUTION)) return;
    editMode = true;
  } else {
    editMode = false;
  }
  editorChunkId = null;
  el("edit-mode-btn").classList.toggle("active", editMode);
  el("edit-mode-btn").innerHTML = editMode ? "&#10005; Exit edit mode" : "&#9998; Edit mode";
  render();
});

document.querySelectorAll(".editor-toolbar button").forEach((btn) => {
  btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep selection
  btn.addEventListener("click", () => {
    const cmd = btn.dataset.cmd;
    if (cmd === "link") {
      const url = window.prompt("Link URL (https://...):", "https://");
      if (url && /^https?:\/\//.test(url)) document.execCommand("createLink", false, url);
    } else {
      document.execCommand(cmd, false, null);
    }
    el("suggestion-editor").focus();
  });
});

el("revert-btn").addEventListener("click", () => {
  const chunk = view[index];
  if (!chunk) return;
  hunks = computeHunks(chunk.text, chunk.suggestion || "");
  renderChangeReview();
  reseedEditorFromHunks();   // explicit reset - discard manual edits
  editorDirty = false;
});

el("accept-all-btn").addEventListener("click", () => {
  hunks.forEach((h) => { if (h.type === "change") h.state = "accept"; });
  renderChangeReview();
  maybeReseed();
});

el("reject-all-btn").addEventListener("click", () => {
  hunks.forEach((h) => { if (h.type === "change") h.state = "reject"; });
  renderChangeReview();
  maybeReseed();
});

// any manual typing / formatting marks the Final text as hand-edited so
// later change-toggles don't silently wipe it
el("suggestion-editor").addEventListener("input", () => { editorDirty = true; });

el("diff-toggle").addEventListener("click", () => { showDiff = !showDiff; render(); });

/* Walk the contenteditable DOM into style runs for the Docs API. */
function collectRuns(node, style) {
  const runs = [];
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      const text = child.textContent.replace(/ /g, " ");
      if (text) runs.push({ ...style, text });
    } else if (child.nodeType === Node.ELEMENT_NODE) {
      const tag = child.tagName;
      if (tag === "BR") { runs.push({ ...style, text: "\n" }); continue; }
      const next = { ...style };
      if (tag === "B" || tag === "STRONG") next.bold = true;
      if (tag === "I" || tag === "EM") next.italic = true;
      if (tag === "U") next.underline = true;
      if (tag === "A" && child.href) next.link = child.href;
      if ((tag === "DIV" || tag === "P") && runs.length
          && !runs[runs.length - 1].text.endsWith("\n")) {
        runs.push({ ...style, text: "\n" });
      }
      runs.push(...collectRuns(child, next));
    }
  }
  return runs;
}

function mergeRuns(runs) {
  const merged = [];
  for (const run of runs) {
    const last = merged[merged.length - 1];
    if (last && last.bold === run.bold && last.italic === run.italic
        && last.underline === run.underline && (last.link || "") === (run.link || "")) {
      last.text += run.text;
    } else {
      merged.push({ ...run });
    }
  }
  return merged.filter((r) => r.text);
}

function changeRatio(original, edited) {
  const ops = diffWords(original, edited);
  let changed = 0, total = 0;
  for (const op of ops) {
    const words = op.text.trim() ? op.text.trim().split(/\s+/).length : 0;
    if (op.type !== "add") total += words;
    if (op.type !== "same") changed += words;
  }
  return total ? changed / total : 1;
}

el("commit-btn").addEventListener("click", async () => {
  const chunk = view[index];
  if (!chunk) return;
  const runs = mergeRuns(collectRuns(el("suggestion-editor"),
    { bold: false, italic: false, underline: false, link: "" }));
  const plain = runs.map((r) => r.text).join("").trim();
  if (!plain) { window.alert("The edited text is empty."); return; }

  if (changeRatio(chunk.text, plain) > 0.5
      && !window.confirm("This edit changes more than 50% of the original "
                         + "paragraph. Are you sure?")) {
    return;
  }

  const btn = el("commit-btn");
  btn.disabled = true;
  btn.textContent = "Committing...";
  try {
    const resp = await fetch("/api/commit-edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc: currentDoc, chunk_id: chunk.chunk_id, runs }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    editsByChunk[chunk.chunk_id] = { at: new Date().toISOString(), text: plain };
    editorChunkId = null;
    render();
  } catch (err) {
    window.alert(`Commit failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Commit edit";
  }
});

el("copy-btn").addEventListener("click", async () => {
  const chunk = view[index];
  if (chunk?.suggestion) {
    await navigator.clipboard.writeText(chunk.suggestion);
    el("copy-btn").textContent = "Copied!";
    setTimeout(() => (el("copy-btn").textContent = "Copy suggestion"), 1200);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT"
      || e.target.isContentEditable) return;
  if (e.key === "ArrowLeft") el("prev-btn").click();
  if (e.key === "ArrowRight") el("next-btn").click();
});

load();
