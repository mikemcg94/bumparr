"use strict";
const $ = (s) => document.querySelector(s);
const log = (m) => { const el = $("#log"); el.textContent = (m + "\n" + el.textContent).slice(0, 4000); };
const makeEl = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = String(text);
  return node;
};

let STATE = { kind: null, search: "", parked: false, offset: 0, kinds: {} };
const PAGE = 24;

async function loadStatus() {
  let s;
  try { s = await (await fetch("/api/status")).json(); }
  catch (e) { $("#status-pill").textContent = "offline"; return; }
  STATE.kinds = s.by_kind;
  $("#status-pill").textContent = s.total + " bumpers · " + s.playable_now + " live";
  const totals = $("#totals"); totals.replaceChildren();
  [[s.total, "total"], [s.playable_now, "playable now"],
   [Object.keys(s.by_kind).length, "kinds"]].forEach(([n, label]) => {
    const box = makeEl("div", "num", n); box.appendChild(makeEl("small", "", label));
    totals.appendChild(box);
  });
  const max = Math.max(1, ...Object.values(s.by_type));
  const typeColor = { video: "#5db3a0", stream: "#c9a15d", card: "#7b83cc", image: "#9a7bcc" };
  const typeBox = $("#by-type"); typeBox.replaceChildren();
  Object.entries(s.by_type).sort((a, b) => b[1] - a[1]).forEach(([t, n]) => {
    const bar = makeEl("div", "bar"), track = makeEl("span", "track"), fill = makeEl("span", "fill");
    fill.style.width = (100 * n / max) + "%";
    fill.style.background = typeColor[t] || "#5db3a0";
    track.appendChild(fill);
    bar.append(makeEl("span", "name", t), track, makeEl("span", "n", n));
    typeBox.appendChild(bar);
  });
  renderFilters();
}

function renderFilters() {
  const kinds = Object.entries(STATE.kinds).sort((a, b) => b[1] - a[1]);
  const total = Object.values(STATE.kinds).reduce((a, b) => a + b, 0);
  const filters = $("#filters"); filters.replaceChildren();
  const chip = (k, label, n) => {
    const b = makeEl("button", "fchip" + (STATE.kind === k ? " on" : ""), label);
    b.dataset.kind = k === null ? "" : k;
    b.appendChild(makeEl("b", "", n));
    filters.appendChild(b);
  };
  chip(null, "all", total);
  kinds.forEach(([k, n]) => chip(k, k, n));
  // The other half of ?enabled=false. A parked row is the one thing you cannot
  // find by scrolling — the pool lists newest first, not parked first — and the
  // enable control only shows up once you have found one. Filters compose on
  // the server, so this narrows the current kind/search rather than replacing it.
  const parked = makeEl("button", "fchip parked" + (STATE.parked ? " on" : ""),
                        "⏸ parked only");
  parked.id = "parked-only";
  filters.appendChild(parked);
  parked.addEventListener("click", () => {
    STATE.parked = !STATE.parked;
    STATE.offset = 0;
    renderFilters();
    loadGrid(true);
  });
  // Dropping a whole category is the usual fix when a search returned junk, so
  // it is offered only while that category is actually selected — never next to
  // "all", where a mis-click would be catastrophic.
  if (STATE.kind) {
    const danger = makeEl("button", "fchip danger", '✕ delete all "' + STATE.kind + '"');
    danger.id = "drop-kind"; filters.appendChild(danger);
  }
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
  // Only the kind chips — the ones `chip()` stamped with data-kind. A bare
  // ".fchip" sweep would also catch the delete-category chip, the parked toggle
  // and every .pv-enable button in the grid, handing each of them a kind reset
  // it never asked for (and a fresh duplicate listener on every re-render).
  filters.querySelectorAll(".fchip[data-kind]").forEach((b) => b.addEventListener("click", () => {
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

// Bringing a parked row back on. The pool keeps rows the system switched off —
// a cam dropped from the YAML, a file the asset sweep could not find — and the
// only way back used to be spotting the id in the list and curling it. The
// server may answer with a `warning` (an on_this_day card is parked by the
// calendar, not by anyone, and the rotation will take it back); relay it rather
// than let the click look like the last word.
async function enableBumper(b) {
  try {
    const r = await fetch("/api/pool/enable?bumper_id=" + encodeURIComponent(b.id),
                          { method: "POST" });
    const j = await r.json();
    if (!r.ok) { log("enable failed: " + (j.error || r.status)); return; }
    log("enabled " + (b.title || b.kind || b.id) +
        (j.changed ? "" : " (already on)") +
        (j.warning ? " — " + j.warning : ""));
    await loadStatus();
    loadGrid(true);
  } catch (e) { log("enable failed: " + e); }
}

// Only a row KNOWN to be parked gets the control. /api/bumpers returns `enabled`
// as 0/1, so falsy is the parked test — but only when the key is actually there.
// /api/bumpers/random (the shuffle preview) omits it entirely and returns none
// but live rows, so a missing value must mean "no button", not "parked": an
// action control appears on evidence of a park, never on the absence of data.
function addEnable(el, b) {
  if (b.enabled === undefined || b.enabled === null || b.enabled) return;
  const x = document.createElement("button");
  x.className = "fchip pv-enable";
  x.title = "Parked — turn this bumper back on";
  x.textContent = "✓ enable";
  x.addEventListener("click", (ev) => { ev.stopPropagation(); enableBumper(b); });
  el.appendChild(x);
}

function cardEl(b) {
  const card = document.createElement("div");
  card.className = "pv-card";
  if (b.type === "video") {
    const v = document.createElement("video");
    v.muted = true; v.loop = true; v.playsInline = true; v.preload = "metadata";
    v.src = String(b.media_url || "") + "#t=2";
    const body = makeEl("div", "pv-body");
    body.append(makeEl("div", "pv-kind", b.kind || ""),
                makeEl("div", "pv-title", b.title || ""),
                makeEl("div", "pv-meta", Math.round(b.duration || 0) + "s · video"));
    card.append(v, body);
    card.addEventListener("mouseenter", () => v.play().catch(() => {}));
    card.addEventListener("mouseleave", () => { v.pause(); });
  } else if (b.type === "stream") {
    const body = makeEl("div", "pv-body");
    body.append(makeEl("div", "pv-kind", b.kind || ""),
                makeEl("div", "pv-title", b.title || ""),
                makeEl("div", "pv-meta", "live stream"));
    card.append(makeEl("div", "pv-stream", "◉ LIVE"), body);
  } else {
    const p = b.payload || {};
    const txt = p.lines ? p.lines.join("\n") : (p.number || p.text || b.title || "");
    card.className = "pv-card pv-textcard";
    card.append(makeEl("div", "pv-kind", b.kind || ""), makeEl("div", "tc", txt));
  }
  addDelete(card, b);
  addEnable(card, b);
  return card;
}

function stationEl(s) {
  const root = makeEl("div", "station-body");
  for (const name of ["live", "standby"]) {
    const ch = (s.channels || {})[name] || {};
    const row = makeEl("div", "station-row");
    row.append(makeEl("span", "lbl", name));
    if (ch.now) {
      const left = Math.max(0, Math.round((ch.now.ends_at || 0) - Date.now() / 1000));
      row.append(makeEl("span", "now", ch.now.title + " (" + ch.now.kind + ", " + left + "s left)"));
    } else {
      row.append(makeEl("span", "now muted", "off air"));
    }
    if (ch.next) row.append(makeEl("span", "next muted", "next: " + ch.next.title));
    root.append(row);
  }
  const urls = s.urls || {};
  for (const [label, key] of [["Channel M3U", "channel_m3u"], ["Guide XMLTV", "guide_xml"], ["Standby HLS", "standby"]]) {
    const row = makeEl("div", "station-url");
    row.append(makeEl("span", "lbl", label));
    const input = document.createElement("input");
    input.readOnly = true; input.className = "url"; input.value = urls[key] || "";
    input.addEventListener("focus", () => input.select && input.select());
    row.append(input);
    root.append(row);
  }
  root.append(makeEl("div", "muted", s.ffmpeg === false
    ? "ffmpeg not found: nothing can be conformed"
    : (s.conformed || 0) + " / " + (s.eligible || 0) + " conformed"));
  return root;
}

async function loadStation() {
  let s;
  try { s = await (await fetch("/api/station")).json(); } catch (e) { return; }
  const el = $("#station");
  el.textContent = "";
  el.append(stationEl(s));
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

function wireMaintenance() {
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
    await doAction("/api/starter?dry_run=" + dry, dry ? "check starter" : "run starter");
  }));
}

async function loadGrid(reset) {
  if (reset) { STATE.offset = 0; $("#grid").innerHTML = ""; }
  const params = new URLSearchParams({ limit: PAGE, offset: STATE.offset });
  if (STATE.kind) params.set("kind", STATE.kind);
  if (STATE.parked) params.set("enabled", "false");
  if (STATE.search) params.set("q", STATE.search);
  let d;
  try { d = await (await fetch("/api/bumpers?" + params)).json(); }
  catch (e) { return; }
  const grid = $("#grid");
  let shown = 0;
  for (const b of d.bumpers) {
    // text cards render from the payload included in the list response
    // (cardEl falls back to the title when it is absent)
    grid.appendChild(cardEl(b));
    shown++;
  }
  STATE.offset += d.count;
  $("#more").classList.toggle("hidden", d.count < PAGE);
  if (reset && shown === 0) grid.replaceChildren(makeEl("div", "empty", "nothing here yet — generate some cards above"));
}

async function shufflePreview() {
  // The preview draws from /api/bumpers/random, which serves only live rows:
  // leaving the parked chip lit would claim these are the parked ones.
  STATE.kind = null; STATE.search = ""; STATE.parked = false; $("#search").value = "";
  renderFilters();
  const d = await (await fetch("/api/bumpers/random?count=" + PAGE)).json();
  const grid = $("#grid"); grid.innerHTML = "";
  d.bumpers.forEach((b) => grid.appendChild(cardEl(b)));
  $("#more").classList.add("hidden");
}

async function pollJob(job, getStatus = async (jobId) =>
  (await fetch("/api/request/" + encodeURIComponent(jobId))).json(),
pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))) {
  const jobId = job.job_id;
  let current = job;
  while (jobId && current.status === "working") {
    await pause(3000);
    current = await getStatus(jobId);
  }
  return current;
}

async function doAction(url, label) {
  const btns = document.querySelectorAll(".actions button");
  btns.forEach((b) => b.disabled = true);
  log("→ " + label + " …");
  try {
    let r = await (await fetch(url, { method: "POST" })).json();
    if (r.job_id) r = await pollJob(r);
    const result = r.result === undefined ? r : r.result;
    const msg = typeof result === "string" ? result : JSON.stringify(result);
    log((["error", "unknown"].includes(r.status) ? "✗ " : "✓ ") + label + ": " +
        msg.trim().split("\n").slice(-2).join(" "));
  } catch (e) { log("✗ " + label + " failed: " + e); }
  btns.forEach((b) => b.disabled = false);
  loadStatus(); loadGrid(true);
  loadStation();
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
function boot() {
  wireMaintenance();
  $("#ask-go").addEventListener("click", submitAsk);
  $("#ask").addEventListener("keydown", (e) => { if (e.key === "Enter") submitAsk(); });

  document.querySelectorAll("[data-gen]").forEach((b) =>
    b.addEventListener("click", () => doAction("/api/generate/" + b.dataset.gen + "?n=20", "generate " + b.dataset.gen)));
  document.querySelectorAll("[data-src]").forEach((b) =>
    b.addEventListener("click", () => doAction("/api/sources/" + b.dataset.src, b.dataset.src)));
  document.querySelectorAll("[data-station]").forEach((b) =>
    b.addEventListener("click", () => doAction("/api/station/conform", "conform")));
  $("#shuffle").addEventListener("click", shufflePreview);
  $("#more").addEventListener("click", () => loadGrid(false));
  $("#search").addEventListener("input", (e) => { STATE.search = e.target.value; loadGrid(true); });

  loadStatus();
  loadGrid(true);
  loadStation();
  setInterval(loadStatus, 20000);
  setInterval(loadStation, 20000);
}

const COMMONJS = typeof module !== "undefined" && module.exports;
if (typeof document !== "undefined" && !COMMONJS) boot();
if (COMMONJS) {
  module.exports = { makeEl, cardEl, stationEl, pollJob, enableBumper };
}
