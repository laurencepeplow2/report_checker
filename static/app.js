/* Reviewer UI: cycle through checked chunks, filter by input level. */

const FLAG_ORDER = { r: 0, a: 1, g: 2, invalid: 3 };
const FLAG_LABEL = { r: "Red", a: "Amber", g: "Green", invalid: "Invalid" };

let allChunks = [];
let view = [];      // chunks matching the level filter
let index = 0;

const el = (id) => document.getElementById(id);

async function load() {
  let data;
  try {
    const resp = await fetch("/api/run-data");
    if (!resp.ok) throw new Error(await resp.text());
    data = await resp.json();
  } catch (err) {
    document.querySelector("main").hidden = true;
    el("empty-state").hidden = false;
    return;
  }

  el("doc-meta").textContent =
    `${data.title} - ${data.document_type} - severity: ${data.severity} - ${data.model}`;
  allChunks = data.chunks;
  applyFilter();
}

function applyFilter() {
  const level = el("level-select").value;
  view = level === "all"
    ? allChunks
    : allChunks.filter((c) => c.input_level === level);
  index = 0;
  render();
}

function worstFlag(chunk) {
  const flags = chunk.results.map((r) => r.flag).filter((f) => f in FLAG_ORDER);
  if (!flags.length) return "g";
  return flags.sort((x, y) => FLAG_ORDER[x] - FLAG_ORDER[y])[0];
}

function render() {
  const panel = el("chunk-panel");
  const content = el("chunk-content");
  const cards = el("issue-cards");
  content.innerHTML = "";
  cards.innerHTML = "";

  if (!view.length) {
    el("chunk-meta").innerHTML = "";
    content.textContent = "No chunks at this input level in the current run.";
    panel.className = "panel";
    el("position").textContent = "0 / 0";
    el("issues-heading").textContent = "Checks";
    updateButtons();
    return;
  }

  const chunk = view[index];
  panel.className = `panel worst-${worstFlag(chunk)}`;

  el("chunk-meta").innerHTML = "";
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

  const sorted = [...chunk.results].sort(
    (a, b) => (FLAG_ORDER[a.flag] ?? 9) - (FLAG_ORDER[b.flag] ?? 9)
  );
  const reds = sorted.filter((r) => r.flag === "r").length;
  const ambers = sorted.filter((r) => r.flag === "a").length;
  el("issues-heading").textContent =
    `Checks (${sorted.length}) - ${reds} red, ${ambers} amber`;

  for (const result of sorted) {
    const card = document.createElement("div");
    card.className = `card flag-${result.flag}`;
    card.innerHTML = `
      <span class="flag-chip" title="${FLAG_LABEL[result.flag] ?? result.flag}">${result.flag}</span>
      <div>
        <div class="category"></div>
        <div class="rule-text"></div>
      </div>`;
    card.querySelector(".category").textContent = result.category;
    card.querySelector(".rule-text").textContent = result.rule;
    cards.appendChild(card);
  }

  el("position").textContent = `${index + 1} / ${view.length}`;
  updateButtons();
}

function updateButtons() {
  el("prev-btn").disabled = index <= 0;
  el("next-btn").disabled = index >= view.length - 1;
}

el("prev-btn").addEventListener("click", () => { if (index > 0) { index--; render(); } });
el("next-btn").addEventListener("click", () => { if (index < view.length - 1) { index++; render(); } });
el("level-select").addEventListener("change", applyFilter);
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") el("prev-btn").click();
  if (e.key === "ArrowRight") el("next-btn").click();
});

load();
