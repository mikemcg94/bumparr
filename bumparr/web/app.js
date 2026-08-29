"use strict";
const $ = (s) => document.querySelector(s);
const log = (m) => { const el = $("#log"); el.textContent = (m + "\n" + el.textContent).slice(0, 4000); };

let STATE = { kind: null, search: "", offset: 0, kinds: {} };
const PAGE = 24;

async function loadStatus() {
  let s;
  try { s = await (await fetch("/api/status")).json(); }
  catch (e) { $("#status-pill").textContent = "offline"; return; }
  STATE.kinds = s.by_kind;
  $("#status-pill").textContent = s.total + " bumpers · " + s.playable_now + " live";
  $("#totals").innerHTML =
    '<div class="num">' + s.total + '<small>total</small></div>' +
    '<div class="num">' + s.playable_now + '<small>playable now</small></div>' +
    '<div class="num">' + Object.keys(s.by_kind).length + '<small>kinds</small></div>';
  const max = Math.max(1, ...Object.values(s.by_type));
  const typeColor = { video: "#5db3a0", stream: "#c9a15d", card: "#7b83cc", image: "#9a7bcc" };
  $("#by-type").innerHTML = Object.entries(s.by_type).sort((a, b) => b[1] - a[1]).map(([t, n]) =>
    '<div class="bar"><span class="name">' + t + '</span>' +
    '<span class="track"><span class="fill" style="width:' + (100 * n / max) + '%;background:' + (typeColor[t] || "#5db3a0") + '"></span></span>' +
    '<span class="n">' + n + '</span></div>').join("");
  renderFilters();
}

function renderFilters() {
  const kinds = Object.entries(STATE.kinds).sort((a, b) => b[1] - a[1]);
  const chip = (k, label, n) =>
    '<button class="fchip' + (STATE.kind === k ? ' on' : '') + '" data-kind="' + (k === null ? '' : k) + '">' +
    label + '<b>' + n + '</b></button>';
  const total = Object.values(STATE.kinds).reduce((a, b) => a + b, 0);
  let html = chip(null, "all", total);
  html += kinds.map(([k, n]) => chip(k, k, n)).join("");
  // Dropping a whole category is the usual fix when a search returned junk, so
  // it is offered only while that category is actually selected — never next to
  // "all", where a mis-click would be catastrophic.
  if (STATE.kind) {
    html += '<button class="fchip danger" id="drop-kind">✕ delete all "' +
            STATE.kind + '"</button>';
  }
  $("#filters").innerHTML = html;
  const dk = $("#drop-kind");
  if (dk) dk.addEventListener("click", async () => {
    const k = STATE.kind, n = STATE.kinds[k] || 0;
    if (!confirm('Delete the entire "' + k + '" category?\n\n' + n +
                 " bumper(s) and their files are removed permanently.")) return;
    try {
      const r = await fetch("/api/pool/kind/" + encodeURIComponent(k), { method: "DELETE" });
      const j = await r.json();
      log("dropped category " + k + ": removed " + j.removed +
          (j.dirs_removed ? ", " + j.dirs_removed + " dir(s)" : ""));
      STATE.kind = null; STATE.offset = 0;
      await loadStatus(); loadGrid(true);
    } catch (e) { log("category delete failed: " + e); }
  });
  document.querySelectorAll(".fchip").forEach((b) => b.addEventListener("click", () => {
    STATE.kind = b.dataset.kind || null;
    STATE.offset = 0;
    renderFilters();
    loadGrid(true);
  }));
}

async function deleteBumper(b, el) {
  const what = (b.title || b.kind || "this bumper").slice(0, 60);
  if (!confirm("Delete \"" + what + "\"?\n\nThe file is removed too, so it cannot come back on the next scan.")) return;
  try {
    const r = await fetch("/api/bumpers/" + encodeURIComponent(b.id), { method: "DELETE" });
    const j = await r.json();
    if (!r.ok) { log("delete failed: " + (j.error || r.status)); return; }
    el.classList.add("gone");
    setTimeout(() => el.remove(), 220);
    log("deleted " + j.kind + " · " + (j.title || b.id) + (j.file_removed ? " (file removed)" : ""));
    loadStatus();
  } catch (e) { log("delete failed: " + e); }
}

function addDelete(el, b) {
  const x = document.createElement("button");
  x.className = "pv-del";
  x.title = "Delete this bumper";
  x.textContent = "✕";
  x.addEventListener("click", (ev) => { ev.stopPropagation(); deleteBumper(b, el); });
  el.appendChild(x);
}

function cardEl(b) {
  const el = document.createElement("div");
  el.className = "pv-card";
  if (b.type === "video") {
    el.innerHTML = '<video muted loop playsinline preload="metadata" src="' + b.media_url + '#t=2"></video>' +
      '<div class="pv-body"><div class="pv-kind">' + b.kind + '</div>' +
      '<div class="pv-title">' + esc(b.title || "") + '</div>' +
      '<div class="pv-meta">' + Math.round(b.duration || 0) + 's · video</div></div>';
    const v = el.querySelector("video");
    el.addEventListener("mouseenter", () => v.play().catch(() => {}));
    el.addEventListener("mouseleave", () => { v.pause(); });
  } else if (b.type === "stream") {
    el.innerHTML = '<div class="pv-stream">◉ LIVE</div>' +
      '<div class="pv-body"><div class="pv-kind">' + b.kind + '</div>' +
      '<div class="pv-title">' + esc(b.title || "") + '</div>' +
      '<div class="pv-meta">live stream</div></div>';
  } else {
    const p = b.payload || {};
    const txt = p.lines ? p.lines.join("\n") : (p.number || p.text || b.title || "");
    el.className = "pv-card pv-textcard";
    el.innerHTML = '<div class="pv-kind">' + b.kind + '</div><div class="tc">' + esc(txt) + '</div>';
  }
  addDelete(el, b);
  return el;
}

// Housekeeping actions. Both are safe and idempotent — they only remove debris
// or restore assets whose media is verifiably fine — so neither needs a confirm.
const MAINT = {
  tidy: { url: "/api/pool/tidy", say: (j) =>
    "tidy: removed " + j.zero_byte_files + " empty file(s), " + j.empty_dirs + " empty dir(s)" },
  revive: { url: "/api/pool/revive", say: (j) =>
    "recheck: " + j.restored + " restored, " + j.still_dead + " still unplayable, " +
    j.skipped_streams + " stream(s) skipped" },
};

document.querySelectorAll("[data-maint]").forEach((b) => b.addEventListener("click", async () => {
  const m = MAINT[b.dataset.maint];
  const label = b.textContent;
  b.disabled = true; b.textContent = "working…";
  try {
    const j = await (await fetch(m.url, { method: "POST" })).json();
    log(m.say(j));
    await loadStatus();
    loadGrid(true);
  } catch (e) { log("failed: " + e); }
  b.disabled = false; b.textContent = label;
}));

document.querySelectorAll("[data-starter]").forEach((b) => b.addEventListener("click", async () => {
  const dry = b.dataset.starter === "dry";
  if (!dry && !confirm("Run the starter seeds?\n\nThis downloads clips from the stock " +
                       "and archive sources using your own API keys. It can take several " +
                       "minutes and is deliberately paced so the archives don't throttle you."))
    return;
  const label = b.textContent;
  b.disabled = true; b.textContent = dry ? "checking…" : "seeding…";
  try {
    const j = await (await fetch("/api/starter?dry_run=" + dry, { method: "POST" })).json();
    log(j.stdout || j.stderr || "no output");
    if (!dry) { await loadStatus(); loadGrid(true); }
  } catch (e) { log("starter failed: " + e); }
  b.disabled = false; b.textContent = label;
}));

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

async function loadGrid(reset) {
  if (reset) { STATE.offset = 0; $("#grid").innerHTML = ""; }
  const params = new URLSearchParams({ limit: PAGE, offset: STATE.offset });
  if (STATE.kind) params.set("kind", STATE.kind);
  let d;
  try { d = await (await fetch("/api/bumpers?" + params)).json(); }
  catch (e) { return; }
  const grid = $("#grid");
  const q = STATE.search.toLowerCase();
  let shown = 0;
  for (const b of d.bumpers) {
    if (q && !(b.title || "").toLowerCase().includes(q)) continue;
    // need payload for text cards — the list endpoint omits it; fetch lazily only for cards
    grid.appendChild(cardEl(b));
    shown++;
  }
  STATE.offset += d.count;
  $("#more").classList.toggle("hidden", d.count < PAGE);
  if (reset && shown === 0) grid.innerHTML = '<div class="empty">nothing here yet — generate some cards above</div>';
}

async function shufflePreview() {
  STATE.kind = null; STATE.search = ""; $("#search").value = "";
  renderFilters();
  const d = await (await fetch("/api/bumpers/random?count=" + PAGE)).json();
  const grid = $("#grid"); grid.innerHTML = "";
  d.bumpers.forEach((b) => grid.appendChild(cardEl(b)));
  $("#more").classList.add("hidden");
}

async function doAction(url, label) {
  const btns = document.querySelectorAll(".actions button");
  btns.forEach((b) => b.disabled = true);
  log("→ " + label + " …");
  try {
    const r = await (await fetch(url, { method: "POST" })).json();
    log("✓ " + label + ": " + (r.output || JSON.stringify(r)).trim().split("\n").slice(-2).join(" "));
  } catch (e) { log("✗ " + label + " failed: " + e); }
  btns.forEach((b) => b.disabled = false);
  loadStatus(); loadGrid(true);
}

async function submitAsk() {
  const inp = $("#ask"), btn = $("#ask-go"), out = $("#ask-result");
  const text = inp.value.trim();
  if (!text) return;
  btn.disabled = true; inp.disabled = true;
  out.className = "ask-result working";
  out.textContent = "⋯ working on it — downloads/captures can take a bit";
  const finish = (ok, msg) => {
    out.className = "ask-result " + (ok ? "done" : "err");
    out.textContent = (ok ? "✓ " : "✗ ") + msg;
    btn.disabled = false; inp.disabled = false; inp.focus();
    loadStatus(); loadGrid(true);
  };
  let job;
  try {
    // Kick off the background job; this returns immediately (no proxy timeout).
    job = await (await fetch("/api/request", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    })).json();
  } catch (e) { return finish(false, "" + e); }
  if (!job.job_id) { return finish(job.status !== "error", job.result || "done"); }
  inp.value = "";
  // Poll the job until it finishes (up to ~5 min for big multi-clip pulls).
  let tries = 0;
  const poll = async () => {
    tries++;
    let s;
    try { s = await (await fetch("/api/request/" + job.job_id)).json(); }
    catch (e) { return finish(false, "lost track of the job: " + e); }
    if (s.status === "working") {
      out.textContent = "⋯ working on it… (" + tries * 3 + "s)";
      if (tries < 100) return void setTimeout(poll, 3000);
      return finish(false, "still going after 5 min — check the pool; it may still be landing");
    }
    finish(s.status === "done", s.result || "done");
  };
  setTimeout(poll, 2000);
}
$("#ask-go").addEventListener("click", submitAsk);
$("#ask").addEventListener("keydown", (e) => { if (e.key === "Enter") submitAsk(); });

document.querySelectorAll("[data-gen]").forEach((b) =>
  b.addEventListener("click", () => doAction("/api/generate/" + b.dataset.gen + "?n=20", "generate " + b.dataset.gen)));
document.querySelectorAll("[data-src]").forEach((b) =>
  b.addEventListener("click", () => doAction("/api/sources/" + b.dataset.src, b.dataset.src)));
$("#shuffle").addEventListener("click", shufflePreview);
$("#more").addEventListener("click", () => loadGrid(false));
$("#search").addEventListener("input", (e) => { STATE.search = e.target.value; loadGrid(true); });

loadStatus();
loadGrid(true);
setInterval(loadStatus, 20000);
