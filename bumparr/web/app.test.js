"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.style = {};
    this.textContent = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  addEventListener() {}
}

const NODES = new Map();

global.document = {
  createElement(tag) { return new FakeNode(tag); },
  // app.js reaches for #log to report, and for #status-pill / #grid when it
  // refreshes after an action. One lazily created node per selector is enough
  // to let those paths run without a DOM.
  querySelector(sel) {
    if (!NODES.has(sel)) NODES.set(sel, new FakeNode("div"));
    return NODES.get(sel);
  },
  querySelectorAll() { return []; },
};

const { cardEl, pollJob, enableBumper, stationEl } = require("./app.js");

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

const buttonClasses = (row) => descendants(cardEl(row))
  .filter((node) => node.tagName === "BUTTON").map((node) => node.className);

const logText = () => document.querySelector("#log").textContent;

// enableBumper refreshes the pool after a successful POST. Those follow-up
// fetches are not what these tests are about, so they reject and app.js's own
// offline/catch paths absorb them, leaving one call worth asserting on.
function stubFetch(reply) {
  const calls = [];
  document.querySelector("#log").textContent = "";
  global.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts && opts.method });
    if (!String(url).startsWith("/api/pool/enable")) throw new Error("refresh not under test");
    return { ok: reply.ok !== false, status: reply.status || 200,
             json: async () => reply.body };
  };
  return calls;
}

test.afterEach(() => { delete global.fetch; });

test("hostile API strings remain text/property values, never parsed markup", () => {
  const title = '<img src=x onerror="globalThis.pwned=1">';
  const kind = 'news" data-owned="yes';
  const media = 'https://media.example/a.mp4" onerror="globalThis.pwned=2';
  const card = cardEl({ type: "video", title, kind, media_url: media, duration: 4 });
  const nodes = descendants(card);

  assert.equal(nodes.filter((node) => node.tagName === "IMG").length, 0);
  assert.equal(nodes.find((node) => node.className === "pv-title").textContent, title);
  assert.equal(nodes.find((node) => node.className === "pv-kind").textContent, kind);
  assert.equal(nodes.find((node) => node.tagName === "VIDEO").src, media + "#t=2");
  assert.equal(globalThis.pwned, undefined);
});

test("an attacker-controlled type cannot create an element", () => {
  // enabled:1 keeps this row off the parked path, so the node list stays the
  // shape this test is about: whatever `type` says, no element comes from it.
  const card = cardEl({
    type: '<iframe src="javascript:alert(1)">',
    kind: "safe",
    enabled: 1,
    payload: { text: "literal <script>not markup</script>" },
  });
  const nodes = descendants(card);
  assert.deepEqual(nodes.map((node) => node.tagName), ["DIV", "DIV", "DIV", "BUTTON"]);
  assert.equal(nodes[2].textContent, "literal <script>not markup</script>");
});

const enableButton = (card) => descendants(card).find(
  (node) => node.tagName === "BUTTON" && String(node.className).split(" ").includes("pv-enable"));

test("a parked row's card offers the enable control", () => {
  const button = enableButton(cardEl(
    { type: "stream", kind: "webcam", title: "harbour", enabled: 0 }));
  assert.ok(button, "a parked row should carry an enable button");
  assert.equal(button.textContent, "✓ enable");
});

test("an enabled row's card has no enable control", () => {
  // Asserted against the parked twin rather than against undefined: on its own,
  // "the button is absent" is also what deleting addEnable entirely would say.
  const row = { type: "stream", kind: "webcam", title: "harbour" };
  assert.deepEqual(buttonClasses({ ...row, enabled: 1 }), ["pv-del"]);
  assert.deepEqual(buttonClasses({ ...row, enabled: 0 }), ["pv-del", "fchip pv-enable"]);
});

test("a row that never says whether it is parked gets no enable control", () => {
  // /api/bumpers/random — the shuffle preview — returns nothing but live rows
  // and has no `enabled` key at all. Reading that undefined as parked put the
  // pill on every card in the preview, each click a POST that logged
  // "(already on)". Missing data is not evidence of a park.
  const row = { type: "stream", kind: "webcam", title: "harbour" };
  assert.equal(enableButton(cardEl(row)), undefined);
  assert.deepEqual(buttonClasses(row), ["pv-del"]);
  assert.deepEqual(buttonClasses({ ...row, enabled: null }), ["pv-del"]);
});

test("the enable control is built from DOM nodes, not row markup", () => {
  // Same rule as the first test: a hostile title reaches the button only as a
  // property, never as parsed markup, and never as its label.
  const title = '<img src=x onerror="globalThis.pwned=3">';
  const button = enableButton(cardEl(
    { type: "card", kind: "on_this_day", title, enabled: 0, payload: { text: "x" } }));
  assert.equal(button.textContent, "✓ enable");
  assert.equal(descendants(cardEl({ type: "card", title, enabled: 0 }))
    .filter((node) => node.tagName === "IMG").length, 0);
  assert.equal(globalThis.pwned, undefined);
});

test("enableBumper escapes ids carrying scheme and path characters", async () => {
  // Pool ids are not URL-safe: "stream:cam:foo", "vid:ambient/x y.mp4". Raw,
  // the slash would re-point the request and the space would break it.
  const calls = stubFetch({ body: { changed: true } });
  await enableBumper({ id: "stream:cam:foo", title: "harbour" });
  await enableBumper({ id: "vid:ambient/x y.mp4", title: "ambient" });
  assert.deepEqual(calls.filter((c) => c.url.startsWith("/api/pool/enable")), [
    { url: "/api/pool/enable?bumper_id=stream%3Acam%3Afoo", method: "POST" },
    { url: "/api/pool/enable?bumper_id=vid%3Aambient%2Fx%20y.mp4", method: "POST" },
  ]);
});

test("a refused enable is reported, not swallowed", async () => {
  stubFetch({ ok: false, status: 404, body: { error: "not found" } });
  await enableBumper({ id: "ghost", title: "ghost cam" });
  assert.match(logText(), /enable failed: not found/);
  assert.doesNotMatch(logText(), /enabled ghost cam/);

  // No error field either: the status is what is left to say, and saying
  // nothing would leave a dead button looking like a working one.
  stubFetch({ ok: false, status: 503, body: {} });
  await enableBumper({ id: "ghost", title: "ghost cam" });
  assert.match(logText(), /enable failed: 503/);
});

test("the server's warning is relayed to the log", async () => {
  // A calendar-managed card comes back with a warning that the rotation will
  // take it away again. Dropping it would let the click look like the last word.
  stubFetch({ body: { changed: true, warning: "the rotation parks it again within the hour" } });
  await enableBumper({ id: "card:on_this_day:abc", title: "moon landing" });
  assert.match(logText(),
    /enabled moon landing — the rotation parks it again within the hour/);
});

test("an enable that changed nothing says so", async () => {
  stubFetch({ body: { changed: false } });
  await enableBumper({ id: "s", title: "harbour" });
  assert.match(logText(), /enabled harbour \(already on\)/);
});

test("action polling continues past the old five-minute cap", async () => {
  let calls = 0;
  const getStatus = async (jobId) => {
    assert.equal(jobId, "slow-job");
    calls++;
    return calls <= 105
      ? { status: "working" }
      : { status: "done", result: "landed" };
  };
  const pause = async (ms) => assert.equal(ms, 3000);

  const result = await pollJob(
    { job_id: "slow-job", status: "working" }, getStatus, pause);

  assert.equal(calls, 106);
  assert.deepEqual(result, { status: "done", result: "landed" });
});

test("station panel renders now/next and URLs as text and values, never markup", () => {
  const s = {
    ffmpeg: true, conformed: 3, eligible: 5,
    urls: { channel_m3u: "http://x/station/channel.m3u\"><img src=x>", guide_xml: "http://x/g.xml", standby: "http://x/s.m3u8" },
    channels: {
      live: { now: { id: "a", title: "<b>Ident</b>", kind: "station_id", started_at: 0, ends_at: Date.now() / 1000 + 5 }, next: { id: "b", title: "Next & co", kind: "trivia" } },
      standby: { now: null, next: null },
    },
  };
  const el = stationEl(s);
  const text = JSON.stringify(el);
  assert.ok(text.includes("<b>Ident</b>"));
  assert.ok(!text.includes("innerHTML"));
  const inputs = [];
  (function walk(n) { if (n.tagName === "INPUT") inputs.push(n); (n.children || []).forEach(walk); })(el);
  assert.equal(inputs.length, 3);
  assert.ok(inputs[0].value.includes("<img src=x>"));
  assert.ok(inputs.every((i) => i.readOnly === true));
  assert.ok(text.includes("off air"));
  assert.ok(text.includes("3 / 5 conformed"));
});

test("station panel says when ffmpeg is missing", () => {
  const el = stationEl({ ffmpeg: false, conformed: 0, eligible: 4, urls: {}, channels: {} });
  assert.ok(JSON.stringify(el).includes("ffmpeg not found"));
});
