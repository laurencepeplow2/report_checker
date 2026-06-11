/* Report Checker UI
 * Review: cycle chunks that have red/amber breaches - extract | rules | suggestion.
 * Document health: broken links, overused words, story (all no-AI).
 * Green flags are excluded from the UI entirely.
 */

const FLAG_ORDER = { r: 0, a: 1 };
const el = (id) => document.getElementById(id);

let allChunks = [];   // chunks with >=1 breach, results filtered to r/a
let view = [];
let index = 0;

/* ---------------- data load ---------------- */

async function load() {
  try {
    const resp = await fetch("/api/run-data");
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    el("doc-meta").textContent =
      `${data.title} - ${data.document_type} - severity: ${data.severity} - ${data.model}`;

    const total = data.chunks.length;
    allChunks = data.chunks
      .map((c) => ({ ...c, breaches: c.results.filter((r) => r.flag === "r" || r.flag === "a") }))
      .filter((c) => c.breaches.length > 0);
    el("issue-summary").textContent =
      `${allChunks.length} of ${total} checked chunks have issues`;
    applyFilter();
  } catch (err) {
    document.querySelectorAll(".view").forEach((v) => (v.hidden = true));
    const empty = el("empty-state");
    empty.hidden = false;
    empty.innerHTML = "No run data found. Run <code>test_run.py</code> first.";
  }

  try {
    const resp = await fetch("/api/analysis");
    if (resp.ok) renderHealth(await resp.json());
  } catch (err) { /* health view stays empty */ }
}

/* ---------------- review ---------------- */

function applyFilter() {
  const level = el("level-select").value;
  view = level === "all" ? allChunks : allChunks.filter((c) => c.input_level === level);
  index = 0;
  render();
}

function render() {
  const content = el("chunk-content");
  const cards = el("issue-cards");
  const suggestion = el("suggestion-content");
  content.innerHTML = "";
  cards.innerHTML = "";
  suggestion.innerHTML = "";
  el("chunk-meta").innerHTML = "";
  el("copy-btn").hidden = true;

  if (!view.length) {
    content.textContent = "No flagged chunks at this input level.";
    content.classList.add("empty");
    suggestion.textContent = "Nothing to improve here.";
    suggestion.classList.add("empty");
    el("breach-count").textContent = "0";
    el("breach-count").classList.add("none");
    el("position").textContent = "0 / 0";
    updateButtons();
    return;
  }
  content.classList.remove("empty");

  const chunk = view[index];

  for (const text of [chunk.tab, chunk.section, chunk.input_level]) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = text;
    el("chunk-meta").appendChild(tag);
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

  suggestion.classList.toggle("empty", !chunk.suggestion);
  if (chunk.suggestion) {
    suggestion.textContent = chunk.suggestion;
    el("copy-btn").hidden = false;
  } else {
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

  // Overused words
  const words = data.word_frequency || [];
  const max = words.length ? words[0].count : 1;
  const bars = el("word-bars");
  bars.innerHTML = "";
  for (const w of words.slice(0, 20)) {
    const row = document.createElement("div");
    row.className = "word-row";
    row.innerHTML = `<span class="w"></span>
      <span class="bar-track"><span class="bar" style="width:${Math.round((w.count / max) * 100)}%"></span></span>
      <span class="n">${w.count}</span>`;
    row.querySelector(".w").textContent = w.word;
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
el("copy-btn").addEventListener("click", async () => {
  const chunk = view[index];
  if (chunk?.suggestion) {
    await navigator.clipboard.writeText(chunk.suggestion);
    el("copy-btn").textContent = "Copied!";
    setTimeout(() => (el("copy-btn").textContent = "Copy suggestion"), 1200);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") el("prev-btn").click();
  if (e.key === "ArrowRight") el("next-btn").click();
});

load();
