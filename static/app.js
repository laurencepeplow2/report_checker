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
let allChunks = [];   // chunks with >=1 breach, results filtered to r/a
let view = [];
let index = 0;
let checked = new Set();  // chunk_ids marked as manually checked

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
  let runData = null;
  try {
    runData = await fetchJson("/api/run-data", "run");
  } catch {
    document.querySelectorAll(".view").forEach((v) => (v.hidden = true));
    const empty = el("empty-state");
    empty.hidden = false;
    empty.innerHTML = "No run data found. Run <code>test_run.py</code> first.";
    return;
  }

  docId = runData.doc_id || "";
  loadChecked();
  el("doc-meta").textContent =
    `${runData.title} - ${runData.document_type} - severity: ${runData.severity} - ${runData.model}`;

  const total = runData.chunks.length;
  allChunks = runData.chunks
    .map((c) => ({ ...c, breaches: c.results.filter((r) => r.flag === "r" || r.flag === "a") }))
    .filter((c) => c.breaches.length > 0);
  el("issue-summary").textContent =
    `${allChunks.length} of ${total} checked chunks have issues`;

  // Flag totals for the health view
  let reds = 0, ambers = 0;
  for (const c of runData.chunks) {
    for (const r of c.results) {
      if (r.flag === "r") reds++;
      if (r.flag === "a") ambers++;
    }
  }
  el("stat-red").textContent = String(reds);
  el("stat-amber").textContent = String(ambers);
  el("stat-total").textContent = String(reds + ambers);

  applyFilter();

  try {
    renderHealth(await fetchJson("/api/analysis", "analysis"));
  } catch { /* health analyses not run yet */ }
}

/* ---------------- review ---------------- */

function applyFilter() {
  const level = el("level-select").value;
  view = level === "all" ? allChunks : allChunks.filter((c) => c.input_level === level);
  index = 0;
  render();
}

function updateProgress() {
  const done = allChunks.filter((c) => checked.has(c.chunk_id)).length;
  el("progress-summary").textContent = `- ${done} / ${allChunks.length} checked`;
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
    content.textContent = "No flagged chunks at this input level.";
    content.classList.add("empty");
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

  for (const text of [chunk.tab, chunk.section, chunk.input_level]) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = text;
    el("chunk-meta").appendChild(tag);
  }

  // Location: section title path + approximate page + deep link to the doc
  const pieces = [];
  if (chunk.heading_path && chunk.heading_path.length) {
    pieces.push(chunk.heading_path[chunk.heading_path.length - 1]);
  }
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
  } else {
    content.textContent = chunk.text;
  }

  const sorted = [...chunk.breaches].sort(
    (a, b) => (FLAG_ORDER[a.flag] ?? 9) - (FLAG_ORDER[b.flag] ?? 9)
  );
  el("breach-count").textContent = String(sorted.length);
  el("breach-count").classList.toggle("none", sorted.length === 0);

  for (const result of sorted) {
    const card = document.createElement("div");
    card.className = `card flag-${result.flag}`;
    card.innerHTML = `
      <span class="flag-chip">${result.flag}</span>
      <div>
        <div class="category"></div>
        <div class="rule-text"></div>
      </div>`;
    card.querySelector(".category").textContent = result.category;
    card.querySelector(".rule-text").textContent = result.rule;
    cards.appendChild(card);
  }

  if (chunk.input_level === "figure") {
    suggestion.classList.add("empty");
    suggestion.textContent = "Suggestions are not generated for figures - "
      + "use the breached rules on the left to fix the figure by hand.";
  } else if (chunk.suggestion) {
    suggestion.classList.remove("empty");
    renderDiff(suggestion, chunk.text, chunk.suggestion);
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
    el("stat-pages").textContent = String(data.approx_pages_excl_annex);
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
    row.querySelector(".where").textContent =
      `link text: "${link.text}" - in: ${link.tabs.join(", ")}` +
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

  // Story
  const story = el("story-list");
  story.innerHTML = "";
  for (const h of data.story || []) {
    const item = document.createElement("div");
    item.className = `story-item lvl-${Math.min(h.level, 3)}`;
    item.textContent = h.text;
    story.appendChild(item);
  }
}

/* ---------------- nav + events ---------------- */

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
el("reviewed-box").addEventListener("change", (e) => {
  const chunk = view[index];
  if (!chunk) return;
  if (e.target.checked) checked.add(chunk.chunk_id);
  else checked.delete(chunk.chunk_id);
  saveChecked();
  el("extract-panel").classList.toggle("is-checked", e.target.checked);
  updateProgress();
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
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "ArrowLeft") el("prev-btn").click();
  if (e.key === "ArrowRight") el("next-btn").click();
});

load();
