(function (root) {
  "use strict";
  const QUEUE_SCHEMA = "research-atlas.scopus-date-queue.v1";
  const RESULT_SCHEMA = "research-atlas.scopus-date-results.v1";
  const STATUSES = new Set(["found", "requires_access", "not_found", "selector_changed", "identity_mismatch", "timeout", "error"]);
  const normalize = value => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim();
  const label = value => normalize(value).replace(/\s*[:：]\s*$/, "").toLowerCase();
  const validEid = value => typeof value === "string" && /^2-s2\.0-[1-9]\d{0,19}$/.test(value);
  const validTime = value => typeof value === "string" && value.length <= 50 && /T.*(?:Z|[+-]\d\d:\d\d)$/i.test(value) && Number.isFinite(Date.parse(value));
  const canonicalUrl = eid => `https://www.scopus.com/pages/publications/${eid.slice(7)}?origin=resultslist`;

  function urlEid(value) {
    try {
      if (typeof value !== "string" || value.length > 2048 || /[\u0000-\u0020]/.test(value)) return null;
      const url = new URL(value);
      if (url.protocol !== "https:" || !["www.scopus.com", "scopus.com"].includes(url.hostname) || url.port || url.username || url.password || url.hash) return null;
      const match = url.pathname.match(/^\/pages\/publications\/([1-9]\d{0,19})\/?$/);
      if (!match) return null;
      const expected = `2-s2.0-${match[1]}`;
      return url.searchParams.getAll("eid").every(eid => validEid(eid) && eid === expected) ? expected : null;
    } catch (_) { return null; }
  }

  function validateQueue(input) {
    if (!input || input.schema !== QUEUE_SCHEMA || !/^[a-f0-9]{32}$/i.test(input.job_id || "") || !/^[a-f0-9]{32}$/i.test(input.batch_id || "")) throw new Error("Research Atlas で出力した取得キュー JSON を選択してください。");
    if (!Array.isArray(input.entries) || !input.entries.length || input.entries.length > 500) throw new Error("取得キューは 1～500 件にしてください。");
    if (!validTime(input.created_at)) throw new Error("キューの作成日時が不正です。");
    const seen = new Set();
    const entries = input.entries.map(entry => {
      if (!entry || !validEid(entry.eid) || typeof entry.url !== "string" || entry.url.length > 500 || urlEid(entry.url) !== entry.eid || seen.has(entry.eid)) throw new Error("Scopus ID・URL が不正、または重複しています。");
      seen.add(entry.eid);
      if (typeof entry.title !== "string" || entry.title.length > 2000) throw new Error("論文タイトルが不正です。");
      return {eid: entry.eid, url: canonicalUrl(entry.eid), title: entry.title};
    });
    return {schema: QUEUE_SCHEMA, job_id: input.job_id, batch_id: input.batch_id, created_at: input.created_at, entries};
  }

  function auditUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:") return "";
      if (urlEid(value)) return canonicalUrl(urlEid(value));
      if (["www.scopus.com", "scopus.com"].includes(url.hostname) && /^\/pages\/publications\/[1-9]\d{0,19}\/?$/.test(url.pathname)) {
        // Preserve contradictory identity evidence; never clean it into a successful record URL.
        const eids = url.searchParams.getAll("eid").slice(0, 10).map(eid => validEid(eid) ? eid : "invalid");
        if (!eids.length || eids.every(eid => eid === `2-s2.0-${url.pathname.split("/").filter(Boolean).at(-1)}`)) eids.push("invalid");
        return `${url.origin}${url.pathname}?${eids.map(eid => `eid=${encodeURIComponent(eid)}`).join("&")}`;
      }
      // Authentication redirect query strings can contain tokens; retain only origin/path.
      return `${url.origin}${url.pathname}`.slice(0, 2048);
    } catch (_) { return ""; }
  }

  function validObservation(value, queue) {
    return value && queue.entries.some(entry => entry.eid === value.eid) && typeof value.url === "string" && value.url.length <= 2048 && auditUrl(value.url) === value.url && STATUSES.has(value.status)
      && typeof value.date_text === "string" && value.date_text.length <= 300 && ["testid", "label", ""].includes(value.matched_by)
      && validTime(value.observed_at)
      && (value.status !== "found" || Boolean(value.date_text && value.matched_by && urlEid(value.url) === value.eid));
  }

  function restoreState(value) {
    if (!value || !value.queue) return null;
    const queue = validateQueue(value.queue);
    const observations = [];
    for (const item of (Array.isArray(value.observations) ? value.observations : []).slice(0, 500)) {
      if (validObservation(item, queue)) {
        const index = observations.findIndex(old => old.eid === item.eid);
        const copy = {eid: item.eid, url: item.url, status: item.status, date_text: item.date_text, matched_by: item.matched_by, observed_at: item.observed_at};
        if (index < 0) observations.push(copy);
        else if (observations[index].status !== "found") observations[index] = copy;
      }
    }
    return {queue, observations, tab_id: Number.isInteger(value.tab_id) ? value.tab_id : null,
      status: "paused", message: "保存済みの続きから再開できます。再開を押すまで取得しません。", exported: Boolean(value.exported)};
  }

  function nextEntry(state) {
    return state.queue.entries.find(entry => !state.observations.some(item => item.eid === entry.eid && ["found", "not_found"].includes(item.status))) || null;
  }

  function recordObservation(state, entry, result, now) {
    const observation = {eid: entry.eid, url: auditUrl(result.url) || canonicalUrl(entry.eid), status: result.status, date_text: String(result.date_text || ""), matched_by: result.matched_by || "", observed_at: now || new Date().toISOString()};
    if (!validObservation(observation, state.queue)) throw new Error("取得結果の形式が不正です。");
    const index = state.observations.findIndex(item => item.eid === entry.eid);
    if (index < 0) state.observations.push(observation);
    else if (state.observations[index].status !== "found") state.observations[index] = observation;
    state.exported = false;
    return observation;
  }

  function resultFile(state) {
    return {schema: RESULT_SCHEMA, job_id: state.queue.job_id, batch_id: state.queue.batch_id, observations: state.observations};
  }

  function sameRowValue(dt) {
    const next = dt.nextElementSibling;
    if (next && String(next.tagName).toLowerCase() === "dd") return next;
    const children = Array.from(dt.parentElement?.children || []);
    const terms = children.filter(node => String(node.tagName).toLowerCase() === "dt");
    const values = children.filter(node => String(node.tagName).toLowerCase() === "dd");
    return terms.length === 1 && values.length === 1 ? values[0] : null;
  }

  function readPage(doc, url, expectedEid) {
    const outcome = (status, detail, date_text = "", matched_by = "") => ({status, detail, date_text, matched_by, url: auditUrl(url)});
    if (!validEid(expectedEid)) return outcome("error", "対象の Scopus ID が不正です。");
    const text = normalize(doc.body?.innerText || "");
    const access = /(?:scopus preview|check your access|check access to (?:view|scopus)|access denied|verify (?:that )?you are (?:a )?human|unusual traffic|too many requests|captcha|temporarily blocked)/i.test(text);
    if (urlEid(url) !== expectedEid) return outcome(access || /(?:login|signin|sign-in|access)/i.test(String(url)) ? "requires_access" : "identity_mismatch", "対象論文の URL と一致しません。専用タブのログイン状態と論文を確認してください。");
    const dts = Array.from(doc.querySelectorAll("dt"));
    const eids = dts.filter(dt => label(dt.textContent) === "eid").map(dt => normalize(sameRowValue(dt)?.textContent)).filter(validEid);
    // Reference lists can contain other papers' EIDs. Only explicit metadata terms identify this paper.
    if (eids.some(eid => eid !== expectedEid)) return outcome("identity_mismatch", "画面内の EID と取得対象が一致しません。読み込みを待って再開してください。");
    const byTestid = Array.from(doc.querySelectorAll('dd[data-testid="document-info-publication-date"]'));
    if (byTestid.some(node => node.previousElementSibling && String(node.previousElementSibling.tagName).toLowerCase() === "dt" && label(node.previousElementSibling.textContent) !== "publication date")) return outcome("selector_changed", "日付の識別子と項目名が一致しません。自動取得を停止しました。");
    const byLabel = dts.filter(dt => label(dt.textContent) === "publication date").map(sameRowValue).filter(Boolean);
    const nodes = [...byTestid, ...byLabel];
    const values = [...new Set(nodes.map(node => normalize(node.textContent)).filter(Boolean))];
    if (values.length > 1) return outcome("selector_changed", "Publication date の値が複数あり一致しません。自動取得を停止しました。");
    if (values.length === 1) {
      const value = values[0];
      if (/^(?:n\/?a|not available|not provided|unknown|—|-|なし|未設定)$/i.test(value)) return outcome("not_found", "Publication date に日付の記載がありません。");
      if (value.length > 300) return outcome("selector_changed", "Publication date の形式が想定と異なります。");
      return outcome("found", "Publication date を取得しました。", value, byTestid.length ? "testid" : "label");
    }
    if (access) return outcome("requires_access", "Scopus のアクセス確認が必要です。専用タブでログイン・契約アクセスを確認後、再開してください。");
    if (nodes.length) return outcome("pending", "Publication date の表示を待っています。");
    return outcome("pending", "Detailed Information の表示を待っています。");
  }

  function findInformationTrigger(doc) {
    const nodes = Array.from(doc.querySelectorAll('button, a, [role="button"], [tabindex], span'));
    return nodes.find(node => label(node.textContent) === "show all information") || null;
  }

  async function capture(doc, location, expectedEid, options = {}) {
    const timeout = Math.min(Math.max(Number(options.timeout_ms) || 18000, 1000), 25000);
    const deadline = Date.now() + timeout;
    let hovered = false, clicked = false, stable = null, stableAt = 0, last = null;
    while (Date.now() < deadline) {
      last = readPage(doc, location.href, expectedEid);
      if (last.status === "found" || last.status === "not_found") {
        const key = `${last.status}:${last.date_text}`;
        if (stable !== key) { stable = key; stableAt = Date.now(); }
        else if (Date.now() - stableAt >= 600) return last;
      } else {
        stable = null;
        if (["requires_access", "selector_changed"].includes(last.status)) return last;
        // SPA transitions can briefly retain the old document; never accept that document's date.
        if (last.status === "pending" && options.auto_open !== false) {
          const trigger = findInformationTrigger(doc);
          if (trigger && !hovered) {
            hovered = true;
            trigger.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
            trigger.dispatchEvent(new MouseEvent("mouseenter", {bubbles: false}));
            trigger.focus?.({preventScroll: true});
          } else if (trigger && !clicked) {
            clicked = true;
            (trigger.closest('button, a, [role="button"]') || trigger).click();
          }
        }
      }
      await new Promise(resolve => {
        let observer;
        const done = () => { clearTimeout(timer); observer?.disconnect(); resolve(); };
        const timer = setTimeout(done, 500);
        if (typeof MutationObserver !== "undefined") {
          observer = new MutationObserver(done);
          observer.observe(doc.documentElement, {subtree: true, childList: true, characterData: true});
        }
      });
    }
    if (last?.status === "identity_mismatch") return last;
    if (last?.status === "pending") {
      const dateNode = doc.querySelector('dd[data-testid="document-info-publication-date"]');
      if (dateNode || Array.from(doc.querySelectorAll("dt")).some(dt => label(dt.textContent) === "publication date")) return {status: "not_found", date_text: "", matched_by: "", detail: "Publication date の項目はありますが値が空です。"};
      return {status: "selector_changed", date_text: "", matched_by: "", detail: "Publication date を確認できません。専用タブで Show all information を開き、「現在の画面から再取得」を押してください。"};
    }
    return {status: "timeout", date_text: "", matched_by: "", detail: "画面の読み込みが完了しませんでした。"};
  }

  const api = {QUEUE_SCHEMA, RESULT_SCHEMA, STATUSES, normalize, validEid, canonicalUrl, urlEid, auditUrl, validateQueue, restoreState, nextEntry, recordObservation, resultFile, sameRowValue, readPage, capture};
  root.AtlasScopusBrowser = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
