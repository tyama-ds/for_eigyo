import test from "node:test";
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFileSync} from "node:fs";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const core = require("../browser-extension/scopus-dates/core.js");
const eid = "2-s2.0-105048869659";
const other = "2-s2.0-105048869660";
const url = core.canonicalUrl(eid);
const queue = () => ({schema: core.QUEUE_SCHEMA, job_id: "a".repeat(32), batch_id: "b".repeat(32), created_at: "2026-10-14T01:00:00Z", entries: [{eid, url, title: "Example paper"}, {eid: other, url: core.canonicalUrl(other), title: "Other paper"}]});
const fresh = () => ({queue: core.validateQueue(queue()), observations: [], status: "paused"});

// Small DOM fixture implements only public DOM operations used by the extractor.
// The nesting mirrors the user-provided dt/dd row, without depending on CSS hashes.
function node(tagName, textContent = "", attrs = {}) {
  return {tagName: tagName.toUpperCase(), textContent, attrs, children: [], parentElement: null, nextElementSibling: null, previousElementSibling: null};
}
function row(label, value, testid = false) {
  const parent = node("div", "", {class: "DetailedInformationFlyout_row_CHANGED"});
  const dt = node("dt", label), dd = node("dd", value, testid ? {"data-testid": "document-info-publication-date"} : {});
  parent.children = [dt, dd]; dt.parentElement = parent; dd.parentElement = parent; dt.nextElementSibling = dd; dd.previousElementSibling = dt;
  return parent;
}
function documentFixture(rows, body = "Scopus document") {
  const all = rows.flatMap(parent => [parent, ...parent.children]);
  return {body: {innerText: body}, documentElement: {}, querySelectorAll(selector) {
    if (selector === "dt") return all.filter(item => item.tagName === "DT");
    if (selector === 'dd[data-testid="document-info-publication-date"]') return all.filter(item => item.tagName === "DD" && item.attrs["data-testid"] === "document-info-publication-date");
    return [];
  }, querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }};
}

test("user supplied unstable class / dt and dd structure yields Publication date", () => {
  const value = core.readPage(documentFixture([row("Publication date", "14 October 2026")]), url, eid);
  assert.equal(value.status, "found"); assert.equal(value.date_text, "14 October 2026"); assert.equal(value.matched_by, "label");
});
test("testid path and label agree; EID metadata verified", () => {
  const value = core.readPage(documentFixture([row(" EID: ", eid), row("Publication  date :", " 14\nOctober 2026 ", true)]), url, eid);
  assert.equal(value.matched_by, "testid"); assert.equal(value.status, "found");
});
test("matching metadata date takes precedence over incidental Preview text", () => {
  assert.equal(core.readPage(documentFixture([row("Publication date", "October 2026")], "Scopus Preview"), url, eid).status, "found");
});
test("Preview, access challenge and CAPTCHA do not manufacture date from publication year", () => {
  for (const body of ["Scopus Preview 2027", "Check your access", "Verify you are a human", "CAPTCHA"]) {
    assert.equal(core.readPage(documentFixture([row("Year", "2027")], body), url, eid).status, "requires_access");
  }
});
test("reused testid with an unrelated label is rejected", () => {
  assert.equal(core.readPage(documentFixture([row("Accepted date", "14 October 2026", true)]), url, eid).status, "selector_changed");
});
test("duplicate identical flyout values accepted; conflicting values stop", () => {
  assert.equal(core.readPage(documentFixture([row("Publication date", "14 October 2026", true), row("Publication date", "14 October 2026")]), url, eid).status, "found");
  assert.equal(core.readPage(documentFixture([row("Publication date", "14 October 2026", true), row("Publication date", "15 October 2026")]), url, eid).status, "selector_changed");
});
test("label lookup stays in its own row and ignores unrelated dates", () => {
  const emptyRow = node("div"); const dt = node("dt", "Publication date"); dt.parentElement = emptyRow; emptyRow.children = [dt];
  const doc = documentFixture([emptyRow, row("Received date", "1 January 2025")]);
  assert.equal(core.readPage(doc, url, eid).status, "pending");
});
test("visible own EID mismatch or stale SPA URL never supplies another paper's date", () => {
  const doc = documentFixture([row("EID", other), row("Publication date", "14 October 2026")]);
  assert.equal(core.readPage(doc, url, eid).status, "identity_mismatch");
  assert.equal(core.readPage(documentFixture([row("Publication date", "14 October 2026")]), core.canonicalUrl(other), eid).status, "identity_mismatch");
});
test("EIDs in a reference list are not mistaken for the current paper", () => {
  const doc = documentFixture([row("Publication date", "14 October 2026")], `References: EID ${other}`);
  assert.equal(core.readPage(doc, url, eid).status, "found");
});
test("login redirect is requires_access and auth query is excluded from audit", () => {
  const value = core.readPage(documentFixture([]), "https://login.elsevier.com/login?code=SECRET", eid);
  assert.equal(value.status, "requires_access"); assert.equal(value.url, "https://login.elsevier.com/login");
  const state = fresh(); core.recordObservation(state, state.queue.entries[0], value);
  assert.equal(state.observations[0].url, "https://login.elsevier.com/login");
});
test("no date sentinel is distinct from changed selectors", () => {
  assert.equal(core.readPage(documentFixture([row("Publication date", "Not available")]), url, eid).status, "not_found");
  assert.equal(core.readPage(documentFixture([row("Online date", "2026")]), url, eid).status, "pending");
});
test("capture accepts a stable date and settles missing structure as selector_changed", async () => {
  assert.equal((await core.capture(documentFixture([row("Publication date", "14 October 2026")]), {href: url}, eid, {timeout_ms: 1500, auto_open: false})).status, "found");
  assert.equal((await core.capture(documentFixture([]), {href: url}, eid, {timeout_ms: 1000, auto_open: false})).status, "selector_changed");
});
test("empty date field after bounded wait is recorded as missing, not inferred", async () => {
  assert.equal((await core.capture(documentFixture([row("Publication date", "")]), {href: url}, eid, {timeout_ms: 1000, auto_open: false})).status, "not_found");
});
test("queue validates identity and replaces arbitrary query with fixed safe URL", () => {
  const input = queue(); input.entries[0].url = url + "&evil=redirect";
  assert.equal(core.validateQueue(input).entries[0].url, url);
  for (const bad of ["https://evil.example/pages/publications/105048869659", "https://www.scopus.com.evil.example/pages/publications/105048869659", "https://name:pass@www.scopus.com/pages/publications/105048869659", "http://www.scopus.com/pages/publications/105048869659", core.canonicalUrl(other)]) {
    const modified = queue(); modified.entries[0].url = bad;
    assert.throws(() => core.validateQueue(modified));
  }
});
test("conflicting or malformed eid query cannot be laundered into a valid observation URL", () => {
  for (const suffix of [`&eid=${other}`, `&eid=${eid}&eid=${other}`, "&eid=", "&eid=secret-invalid", "#fragment"]) {
    const conflicting = url + suffix;
    assert.equal(core.urlEid(conflicting), null);
    const value = core.readPage(documentFixture([row("Publication date", "14 October 2026")]), conflicting, eid);
    assert.equal(value.status, "identity_mismatch");
    const state = fresh(); core.recordObservation(state, state.queue.entries[0], value);
    assert.equal(core.urlEid(state.observations[0].url), null);
    assert.equal(state.observations[0].url.includes("secret-invalid"), false);
    assert.equal(core.restoreState(state).observations.length, 1);
  }
  assert.equal(core.urlEid(url + `&eid=${eid}&eid=${eid}`), eid);
});
test("queue rejects oversized batches, duplicate IDs and malformed timestamps", () => {
  const input = queue(); input.entries = Array.from({length: 501}, (_, i) => ({eid: `2-s2.0-${i + 1}`, url: core.canonicalUrl(`2-s2.0-${i + 1}`), title: "Paper"}));
  assert.throws(() => core.validateQueue(input));
  input.entries = input.entries.slice(0, 500); assert.equal(core.validateQueue(input).entries.length, 500);
  input.entries[1] = input.entries[0]; assert.throws(() => core.validateQueue(input));
  const noZone = queue(); noZone.created_at = "2026-10-14T00:00:00"; assert.throws(() => core.validateQueue(noZone));
  assert.equal(core.validEid("2-s2.0-0123"), false); assert.equal(core.validEid("2-s2.0-" + "9".repeat(21)), false);
});
test("durable resume retries failed entry without re-fetching prior found dates", () => {
  const state = fresh();
  core.recordObservation(state, state.queue.entries[0], {status: "found", date_text: "14 October 2026", matched_by: "label"});
  core.recordObservation(state, state.queue.entries[1], {status: "timeout"}); state.status = "running";
  const restored = core.restoreState(JSON.parse(JSON.stringify(state)));
  assert.equal(restored.status, "paused"); assert.equal(core.nextEntry(restored).eid, other);
  core.recordObservation(restored, restored.queue.entries[1], {status: "found", date_text: "15 October 2026", matched_by: "testid"});
  assert.equal(core.nextEntry(restored), null); assert.equal(restored.observations.length, 2);
  assert.deepEqual(Object.keys(core.resultFile(restored)), ["schema", "job_id", "batch_id", "observations"]);
});
test("failed retry never erases a successful observation", () => {
  const state = fresh(); const entry = state.queue.entries[0];
  core.recordObservation(state, entry, {status: "found", date_text: "14 October 2026", matched_by: "label"});
  core.recordObservation(state, entry, {status: "error"});
  assert.equal(state.observations[0].status, "found");
});
test("malformed persisted results are discarded and damaged queue is rejected", () => {
  const state = fresh(); state.observations = [{eid, status: "found", date_text: "2026"}, {eid: other, url: core.canonicalUrl(other), status: "running", date_text: "", matched_by: "", observed_at: "2026-10-14T01:00:00Z"}];
  assert.deepEqual(core.restoreState(state).observations, []);
  assert.throws(() => core.restoreState({queue: {schema: "evil"}}));
  assert.equal(core.restoreState(null), null);
});
test("extension is restricted to Scopus and needs no cookie, downloads, or debugger permission", () => {
  const manifest = JSON.parse(readFileSync(new URL("../browser-extension/scopus-dates/manifest.json", import.meta.url)));
  assert.equal(manifest.manifest_version, 3); assert.deepEqual(manifest.permissions, ["storage", "scripting"]);
  assert.deepEqual(manifest.host_permissions, ["https://www.scopus.com/*", "https://scopus.com/*"]);
  assert.equal(manifest.background, undefined);
});

const turn = () => new Promise(resolve => setImmediate(resolve));
async function panelFixture(saved = null, captureResult = () => ({status: "found", date_text: "14 October 2026", matched_by: "label", detail: "取得しました"})) {
  const elements = new Map();
  function element() {
    return {textContent: "", disabled: false, hidden: false, children: [], events: {}, addEventListener(name, fn) { this.events[name] = fn; }, replaceChildren() { this.children = []; }, append(...items) { this.children.push(...items); }};
  }
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  let stored = saved ? structuredClone(saved) : null, locked = false, tabsCreated = 0, tabsUpdated = 0, captureCalls = 0;
  const tabs = new Map();
  const chrome = {
    storage: {local: {async get(key) { return {[key]: stored ? structuredClone(stored) : null}; }, async set(value) { stored = structuredClone(Object.values(value)[0]); }}},
    tabs: {
      async create({url}) { const tab = {id: ++tabsCreated, url, status: "complete"}; tabs.set(tab.id, tab); return tab; },
      async get(id) { if (!tabs.has(id)) throw new Error("missing"); return tabs.get(id); },
      async update(id, params) { tabsUpdated++; Object.assign(tabs.get(id), params); return tabs.get(id); },
    },
    scripting: {async executeScript(options) { if (options.files) return []; captureCalls++; return [{result: await captureResult(captureCalls)}]; }},
  };
  const context = vm.createContext({
    AtlasScopusBrowser: core, chrome, console,
    document: {getElementById: get, createElement: element},
    window: {addEventListener() {}, confirm() { return true; }},
    navigator: {locks: {async request(_name, _options, fn) { if (locked) return fn(null); locked = true; try { return await fn({}); } finally { locked = false; } }}},
    setTimeout: callback => { queueMicrotask(callback); return 1; }, Date, URL, Map, JSON,
  });
  vm.runInContext(readFileSync(new URL("../browser-extension/scopus-dates/panel.js", import.meta.url), "utf8"), context);
  await turn();
  return {get, stored: () => stored, stats: () => ({tabsCreated, tabsUpdated, captureCalls}), async importQueue() { await get("queue-file").events.change({target: {files: [{size: 200, text: async () => JSON.stringify(queue())}], value: "queue.json"}}); }};
}
test("control panel checkpoints a found paper, stops on failure and resumes only remaining paper", async () => {
  const panel = await panelFixture(null, count => count === 2 ? {status: "requires_access", detail: "ログインしてください"} : {status: "found", date_text: "14 October 2026", matched_by: "label"});
  await panel.importQueue();
  assert.equal(panel.stats().tabsCreated, 0);
  await panel.get("start").events.click();
  assert.equal(panel.stored().status, "paused");
  assert.equal(panel.stored().observations[0].status, "found");
  assert.equal(panel.stored().observations[1].status, "requires_access");
  assert.equal(panel.stats().tabsCreated, 1);
  await panel.get("start").events.click();
  assert.equal(panel.stored().status, "completed");
  assert.equal(panel.stats().captureCalls, 3);
  assert.equal(panel.stored().observations.length, 2);
});
test("pause saves the in-flight success and does not navigate to another paper", async () => {
  let release;
  const panel = await panelFixture(null, () => new Promise(resolve => { release = resolve; }));
  await panel.importQueue();
  const running = panel.get("start").events.click();
  await turn();
  assert.equal(typeof release, "function");
  panel.get("pause").events.click();
  release({status: "found", date_text: "14 October 2026", matched_by: "testid"});
  await running;
  assert.equal(panel.stored().status, "paused"); assert.equal(panel.stored().observations.length, 1);
  assert.equal(panel.stats().tabsCreated, 1); assert.equal(panel.stats().tabsUpdated, 0);
});
test("reopened panel never auto-runs or navigates a recycled persisted tab ID", async () => {
  const state = fresh(); state.tab_id = 42; state.status = "running";
  core.recordObservation(state, state.queue.entries[0], {status: "found", date_text: "14 October 2026", matched_by: "label"});
  const panel = await panelFixture(state);
  assert.equal(panel.stats().tabsCreated, 0); assert.equal(panel.get("show-tab").disabled, true);
  await panel.get("start").events.click();
  assert.equal(panel.stats().tabsCreated, 1); assert.equal(panel.stats().tabsUpdated, 0);
  assert.equal(panel.stats().captureCalls, 1);
});
test("damaged checkpoint can be replaced by a freshly exported queue", async () => {
  const panel = await panelFixture({queue: {schema: "broken"}});
  assert.equal(panel.get("error").hidden, false);
  await panel.importQueue();
  assert.equal(panel.get("error").hidden, true); assert.equal(panel.stored().queue.batch_id, queue().batch_id);
});
