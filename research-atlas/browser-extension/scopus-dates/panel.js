/* The visible extension page owns the run. No suspended MV3 worker or hidden auto-resume. */
"use strict";
(() => {
  const core = globalThis.AtlasScopusBrowser;
  const STORAGE_KEY = "atlas_scopus_date_checkpoint_v1";
  const LOCK = "atlas_scopus_date_runner_v1";
  const $ = id => document.getElementById(id);
  const captions = {found: "取得済み", not_found: "日付の記載なし", requires_access: "アクセス確認", selector_changed: "画面の項目を確認", identity_mismatch: "論文 ID 不一致", timeout: "時間切れ", error: "取得エラー"};
  let state = null, busy = false, pauseRequested = false;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const error = message => { $("error").textContent = message || ""; $("error").hidden = !message; };
  const persist = async () => { if (state) await chrome.storage.local.set({[STORAGE_KEY]: state}); };

  function render() {
    const observations = state?.observations || [];
    const completed = observations.filter(item => ["found", "not_found"].includes(item.status)).length;
    $("queue-file").disabled = busy;
    $("start").disabled = busy || !state || !core.nextEntry(state);
    $("pause").disabled = !busy || pauseRequested;
    $("show-tab").disabled = !state || !Number.isInteger(state.tab_id);
    $("capture").disabled = busy || !state || !core.nextEntry(state) || !Number.isInteger(state.tab_id);
    $("export").disabled = !observations.length;
    $("queue-summary").textContent = state ? `${state.queue.entries.length} 件 · バッチ ${state.queue.batch_id.slice(0, 8)} · ${completed} 件を確認済み` : "キュー未読込";
    $("progress").max = state?.queue.entries.length || 1;
    $("progress").value = completed;
    $("status").textContent = state?.message || "キューの読み込みを待っています。";
    $("totals").textContent = state ? `日付あり ${observations.filter(item => item.status === "found").length} 件 / 日付の記載なし ${observations.filter(item => item.status === "not_found").length} 件 / 要確認 ${observations.filter(item => !["found", "not_found"].includes(item.status)).length} 件` : "";
    $("records").replaceChildren();
    const entries = new Map((state?.queue.entries || []).map(entry => [entry.eid, entry]));
    for (const item of observations.slice().reverse()) {
      const row = document.createElement("tr");
      const title = document.createElement("td"); title.textContent = entries.get(item.eid)?.title || item.eid;
      const id = document.createElement("small"); id.textContent = item.eid; title.append(id);
      const status = document.createElement("td"); status.textContent = captions[item.status];
      const date = document.createElement("td"); date.textContent = item.date_text || "—";
      row.append(title, status, date); $("records").append(row);
    }
  }

  async function exclusive(action) {
    if (!navigator.locks) throw new Error("このブラウザは取得の排他制御に対応していません。Chrome / Edge を更新してください。");
    return navigator.locks.request(LOCK, {ifAvailable: true}, async lock => {
      if (!lock) throw new Error("別の取得パネルが処理中です。そのパネルで一時停止してから操作してください。");
      return action();
    });
  }

  async function ensureTab(entry, navigate) {
    let tab = null;
    if (Number.isInteger(state.tab_id)) {
      try { tab = await chrome.tabs.get(state.tab_id); } catch (_) { state.tab_id = null; }
    }
    if (!tab) {
      tab = await chrome.tabs.create({url: entry.url, active: false});
      state.tab_id = tab.id; await persist();
    } else if (navigate) {
      // This tab was created by this panel. Existing user tabs are never selected or navigated.
      await chrome.tabs.update(tab.id, {url: entry.url});
    }
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline && !pauseRequested) {
      const current = await chrome.tabs.get(tab.id);
      if (current.status === "complete") {
        if (core.urlEid(current.url) !== entry.eid) {
          if (current.pendingUrl) { await wait(350); continue; }
          return {status: !current.url || /login|signin|sign-in|access/i.test(current.url) ? "requires_access" : "identity_mismatch", url: core.auditUrl(current.url), date_text: "", matched_by: "", detail: "専用タブが対象論文以外のページです。ログイン・アクセス状態を確認してください。"};
        }
        return null;
      }
      await wait(350);
    }
    return {status: "timeout", date_text: "", matched_by: "", detail: "ページの読み込みが 30 秒以内に完了しませんでした。専用タブを確認してください。"};
  }

  async function inspect(entry, manual) {
    const navigation = await ensureTab(entry, !manual);
    if (navigation || pauseRequested) return navigation;
    await chrome.scripting.executeScript({target: {tabId: state.tab_id}, files: ["core.js"]});
    const values = await chrome.scripting.executeScript({target: {tabId: state.tab_id}, func: async (eid, autoOpen) => {
      return globalThis.AtlasScopusBrowser.capture(document, window.location, eid, {auto_open: autoOpen});
    }, args: [entry.eid, !manual]});
    return values[0]?.result || {status: "error", date_text: "", matched_by: "", detail: "ページから取得結果が返りませんでした。"};
  }

  async function run(manual = false) {
    if (!state || busy) return;
    await exclusive(async () => {
      // A different panel may have advanced the same saved queue since this page was opened.
      const stored = (await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY];
      if (stored && (stored.queue?.batch_id !== state.queue.batch_id || JSON.stringify(stored.observations) !== JSON.stringify(state.observations))) throw new Error("別のパネルで保存内容が更新されました。このパネルを再読込してください。");
      busy = true; pauseRequested = false; error("");
      try {
        state.status = "running"; state.message = "取得を開始します。"; await persist(); render();
        while (!pauseRequested) {
          const entry = core.nextEntry(state);
          if (!entry) { state.status = "completed"; state.message = "このバッチの確認が完了しました。結果 JSON を保存してアプリへ読み込んでください。"; break; }
          state.message = `取得中: ${entry.title || entry.eid}`; render();
          const started = Date.now();
          let result;
          try { result = await inspect(entry, manual); }
          catch (_) { result = {status: "error", date_text: "", matched_by: "", detail: "専用タブを読み取れませんでした。タブ・拡張機能のサイトアクセス権限・ログイン状態を確認してください。"}; }
          if (pauseRequested && (!result || !["found", "not_found"].includes(result.status))) break;
          core.recordObservation(state, entry, result);
          state.message = result.detail;
          await persist(); render();
          if (!["found", "not_found"].includes(result.status)) { state.status = "paused"; break; }
          if (manual) { state.status = "paused"; state.message = "現在の画面の結果を保存しました。続きは「開始・再開」を押してください。"; break; }
          await wait(Math.max(0, 1500 - (Date.now() - started)));
        }
        if (pauseRequested) { state.status = "paused"; state.message = "一時停止しました。確認済みの結果は保存されています。"; }
        await persist();
      } finally { busy = false; render(); }
    });
  }

  $("queue-file").addEventListener("change", async event => {
    try {
      const file = event.target.files?.[0]; if (!file) return;
      if (file.size > 1024 * 1024) throw new Error("取得キューが 1 MiB を超えています。アプリで小さいバッチを出力してください。");
      const queue = core.validateQueue(JSON.parse(await file.text()));
      await exclusive(async () => {
        let stored;
        try { stored = core.restoreState((await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY]); }
        catch (_) { stored = null; }
        if (stored?.queue.batch_id === queue.batch_id) {
          state = stored; state.tab_id = null;
        } else {
          if (stored?.observations.length && !stored.exported && !window.confirm("前のバッチの結果が未出力です。キューを置き換えると前の保存結果を失います。置き換えますか？")) return;
          state = {queue, observations: [], tab_id: null, status: "paused", exported: false, message: "キューを読み込みました。「開始・再開」で取得します。"};
        }
        await persist(); error(""); render();
      });
    } catch (failure) { error(failure.message || "取得キューを読み込めませんでした。"); }
    finally { event.target.value = ""; }
  });
  $("start").addEventListener("click", () => run().catch(failure => error(failure.message)));
  $("capture").addEventListener("click", () => run(true).catch(failure => error(failure.message)));
  $("pause").addEventListener("click", () => {
    pauseRequested = true;
    if (state) state.message = "停止要求を受け付けました。現在のページ処理が終わるまで最大約 30 秒お待ちください。";
    render();
  });
  $("show-tab").addEventListener("click", async () => {
    try { await chrome.tabs.update(state.tab_id, {active: true}); }
    catch (_) { error("専用タブが閉じられています。「開始・再開」で新しい専用タブを開きます。"); }
  });
  $("export").addEventListener("click", async () => {
    const blob = new Blob([JSON.stringify(core.resultFile(state), null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `scopus-dates-${state.queue.batch_id}.json`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    // Export is permitted during a run; don't overwrite the runner's checkpoint from another panel.
    if (!busy) {
      await exclusive(async () => {
        const stored = (await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY];
        if (stored?.queue?.batch_id !== state.queue.batch_id || JSON.stringify(stored.observations) !== JSON.stringify(state.observations)) throw new Error("別のパネルで結果が更新されました。最新のパネルから結果を保存してください。");
        state.exported = true; await persist();
      }).catch(failure => error(failure.message));
    }
  });
  window.addEventListener("beforeunload", event => { if (busy) { event.preventDefault(); event.returnValue = ""; } });
  (async () => {
    try {
      state = core.restoreState((await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY]);
      // Browser tab IDs can be recycled after restart. Never reuse a persisted tab ID.
      if (state) state.tab_id = null;
      render();
    } catch (_) { error("保存した取得キューを復元できませんでした。アプリからキューを再出力して読み込んでください。"); }
  })();
})();
