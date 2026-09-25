/* Mycel フロントエンド（依存なし）。 */
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = MD.esc;
  const app = $("#app");

  // ------------------------------------------------------------ 小物
  const store = {
    get(k, d) { try { const v = localStorage.getItem("mycel:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("mycel:" + k, JSON.stringify(v)); } catch { /* 無視 */ } },
  };

  async function request(method, path, body, params) {
    const url = path + (params ? "?" + new URLSearchParams(params) : "");
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(url, opt); } catch { const e = new Error("サーバに接続できません（Mycel が起動しているか確認してください）"); e.status = 0; throw e; }
    let data = {};
    try { data = await res.json(); } catch { /* 空 */ }
    if (!res.ok) { const e = new Error(data.error || `エラー (${res.status})`); e.status = res.status; e.data = data; throw e; }
    return data;
  }
  const api = { get: (p, params) => request("GET", p, undefined, params), post: (p, body = {}) => request("POST", p, body) };

  let toastTimer;
  function toast(msg, err = false) {
    let t = $(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
    t.textContent = msg; t.classList.toggle("err", err); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 2600);
  }
  const fail = (e) => toast(e.message || String(e), true);
  const titleOf = (p) => p.split("/").pop().replace(/\.md$/i, "");
  const folderOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const icon = (d) => `<svg viewBox="0 0 24 24">${d}</svg>`;

  // ------------------------------------------------------------ 状態
  const S = {
    state: {}, tree: [], folders: [], titleMap: new Map(), pathMap: new Map(), mtimes: new Map(),
    cur: null, mode: store.get("mode", "prev"), dirty: false, saving: null, saveTimer: 0, conflict: null,
    rtab: store.get("rtab", "links"), panel: "files", closed: new Set(store.get("closed", [])), selFolder: "",
    hist: [], histPos: -1, chat: [], depth: store.get("depth", 1), graph: null, tag: "",
  };

  // ------------------------------------------------------------ ツリー
  async function loadTree() {
    const t = await api.get("/api/tree");
    S.tree = t.notes; S.folders = t.folders;
    S.titleMap = new Map(); S.pathMap = new Map(); S.mtimes = new Map();
    const sorted = [...t.notes].sort((a, b) => a.path.length - b.path.length);
    for (const n of sorted) {
      const k = n.title.toLowerCase();
      if (!S.titleMap.has(k)) S.titleMap.set(k, n.path);
      S.pathMap.set(n.path.slice(0, -3).toLowerCase(), n.path);
      S.mtimes.set(n.path, n.mtime_ns);
    }
    renderTree();
  }

  function resolve(target) {
    let k = (target || "").trim().toLowerCase();
    if (!k) return S.cur ? S.cur.path : null;
    if (k.endsWith(".md")) k = k.slice(0, -3);
    if (k.includes("/")) { if (S.pathMap.has(k)) return S.pathMap.get(k); k = k.split("/").pop(); }
    return S.titleMap.get(k) || null;
  }

  function renderTree() {
    const el = $("#tree");
    const q = $("#filter").value.trim().toLowerCase();
    if (q) {
      const hits = S.tree.filter((n) => n.path.toLowerCase().includes(q));
      el.innerHTML = hits.length ? hits.map((n) => fileBtn(n, 0, true)).join("") : '<div class="empty">該当するノートはありません</div>';
      return;
    }
    const root = { folders: {}, notes: [] };
    const node = (path) => {
      let cur = root;
      if (!path) return cur;
      for (const part of path.split("/")) cur = cur.folders[part] ||= { folders: {}, notes: [] };
      return cur;
    };
    S.folders.forEach((f) => node(f));
    S.tree.forEach((n) => node(n.folder).notes.push(n));
    const coll = (a, b) => a.localeCompare(b, "ja", { numeric: true });
    const walk = (nd, path, depth) => {
      let h = "";
      for (const name of Object.keys(nd.folders).sort(coll)) {
        const fp = path ? path + "/" + name : name;
        const closed = S.closed.has(fp);
        h += `<button class="folder${closed ? " closed" : ""}" data-folder="${esc(fp)}" style="padding-left:${6 + depth * 14}px" draggable="false"><span class="car">▾</span>${esc(name)}</button>`;
        if (!closed) h += walk(nd.folders[name], fp, depth + 1);
      }
      nd.notes.sort((a, b) => coll(a.title, b.title)).forEach((n) => (h += fileBtn(n, depth)));
      return h;
    };
    el.innerHTML = walk(root, "", 0) || '<div class="empty">ノートがありません。右上の＋で作成します。</div>';
  }
  function fileBtn(n, depth, showFolder = false) {
    const on = S.cur && S.cur.path === n.path ? " on" : "";
    return `<button class="file${on}" data-open="${esc(n.path)}" draggable="true" title="${esc(n.path)}" style="padding-left:${20 + depth * 14}px">${esc(n.title)}${showFolder && n.folder ? ` <small style="color:var(--muted)">${esc(n.folder)}</small>` : ""}</button>`;
  }

  $("#tree").addEventListener("click", (e) => {
    const f = e.target.closest("[data-folder]");
    if (f) {
      const fp = f.dataset.folder; S.selFolder = fp;
      S.closed.has(fp) ? S.closed.delete(fp) : S.closed.add(fp);
      store.set("closed", [...S.closed]); renderTree();
    }
  });
  $("#tree").addEventListener("contextmenu", (e) => {
    const b = e.target.closest("[data-open]");
    if (!b) return;
    e.preventDefault(); noteMenu(e.clientX, e.clientY, b.dataset.open);
  });
  // ドラッグでフォルダへ移動
  $("#tree").addEventListener("dragstart", (e) => { const b = e.target.closest("[data-open]"); if (b) e.dataTransfer.setData("text/mycel-path", b.dataset.open); });
  $("#tree").addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("text/mycel-path")) { e.preventDefault(); $$(".drop").forEach((x) => x.classList.remove("drop")); (e.target.closest("[data-folder]") || $("#tree")).classList.add("drop"); } });
  $("#tree").addEventListener("dragleave", (e) => { if (e.target.classList) e.target.classList.remove("drop"); });
  $("#tree").addEventListener("drop", async (e) => {
    const path = e.dataTransfer.getData("text/mycel-path");
    $$(".drop").forEach((x) => x.classList.remove("drop"));
    if (!path) return;
    e.preventDefault();
    const f = e.target.closest("[data-folder]");
    const folder = f ? f.dataset.folder : "";
    if (folderOf(path) === folder) return;
    await renameNote(path, (folder ? folder + "/" : "") + titleOf(path));
  });
  $("#filter").addEventListener("input", renderTree);
  $("#btnCollapse").onclick = () => { S.folders.forEach((f) => S.closed.add(f)); store.set("closed", [...S.closed]); renderTree(); };

  // ------------------------------------------------------------ ノートを開く
  async function openNote(path, { heading = "", push = true, mode = null } = {}) {
    await flushSave();
    let data;
    try { data = await api.get("/api/note", { path }); } catch (e) { fail(e); return; }
    S.cur = data; S.dirty = false; S.conflict = null;
    if (mode) setMode(mode, false);
    if (push) { S.hist = S.hist.slice(0, S.histPos + 1); if (S.hist[S.histPos] !== path) S.hist.push(path); S.histPos = S.hist.length - 1; }
    store.set("last", path);
    S.mtimes.set(path, (S.tree.find((n) => n.path === path) || {}).mtime_ns);
    renderMain(); renderRight(); renderTree(); setSaveStatus();
    const act = $(".tree .file.on"); if (act) act.scrollIntoView({ block: "nearest" });
    if (heading) scrollToHeading(heading);
    else $("#body").scrollTop = 0;
  }

  async function openTarget(target, heading = "") {
    const path = resolve(target);
    if (path) return openNote(path, { heading });
    if (!target) return;
    try {
      const r = await api.post("/api/note/create", { path: target.replace(/\.md$/i, "") });
      await loadTree();
      await openNote(r.path, { mode: "edit" });
      toast(`「${titleOf(r.path)}」を作成しました`);
    } catch (e) { fail(e); }
  }

  function scrollToHeading(h) {
    const want = h.trim().toLowerCase();
    if (S.mode === "edit") {
      const ta = $("#editor"); if (!ta) return;
      const i = ta.value.split("\n").findIndex((l) => /^#{1,6}\s/.test(l) && l.replace(/^#+\s*/, "").trim().toLowerCase() === want);
      if (i >= 0) { const pos = ta.value.split("\n").slice(0, i).join("\n").length + 1; ta.focus(); ta.setSelectionRange(pos, pos); ta.scrollTop = Math.max(0, i * 25 - 60); }
      return;
    }
    const el = $$("#body .md h1, #body .md h2, #body .md h3, #body .md h4, #body .md h5, #body .md h6").find((x) => x.textContent.trim().toLowerCase() === want);
    if (el) { el.scrollIntoView({ block: "start" }); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  }

  // ------------------------------------------------------------ 本文
  function renderMain() {
    const c = S.cur;
    $("#titleIn").disabled = !c;
    $("#titleIn").value = c ? c.title : "";
    $("#crumb").textContent = c && c.folder ? c.folder + " /" : "";
    document.title = c ? `${c.title} — Mycel` : "Mycel";
    $("#mPrev").classList.toggle("on", S.mode === "prev");
    $("#mEdit").classList.toggle("on", S.mode === "edit");
    $("#conflict").hidden = !S.conflict;
    if (!c) {
      $("#body").innerHTML = `<div class="welcome"><h1>Mycel</h1><p>左のファイルからノートを開くか、<b>Ctrl+K</b> でノートを探して開いてください。見つからない名前を入力すると新しく作れます。</p></div>`;
      updateStats(); return;
    }
    if (S.mode === "edit") renderEditor(); else renderPreview();
    updateStats();
  }

  function renderPreview() {
    const top = $("#body").scrollTop;
    $("#body").innerHTML = `<article class="preview md">${MD.render(S.cur.text, { resolve })}</article>`;
    $("#body").scrollTop = top;
  }

  function renderEditor() {
    $("#body").innerHTML = `<textarea class="editor" id="editor" spellcheck="false" aria-label="Markdown を編集"></textarea>`;
    const ta = $("#editor");
    ta.value = S.cur.text;
    ta.addEventListener("input", () => { S.cur.text = ta.value; markDirty(); acUpdate(); });
    ta.addEventListener("keydown", editorKeys);
    ta.addEventListener("click", acUpdate);
    ta.addEventListener("blur", () => setTimeout(acClose, 150));
  }

  function setMode(mode, render = true) {
    let caret = null;
    const ta = $("#editor");
    if (ta) caret = ta.selectionStart;
    S.mode = mode; store.set("mode", mode);
    if (render && S.cur) {
      renderMain();
      if (mode === "edit") { const t = $("#editor"); t.focus(); if (caret !== null) t.setSelectionRange(caret, caret); }
    } else { $("#mPrev").classList.toggle("on", mode === "prev"); $("#mEdit").classList.toggle("on", mode === "edit"); }
  }
  $("#mPrev").onclick = () => setMode("prev");
  $("#mEdit").onclick = () => setMode("edit");

  function updateStats() {
    if (!S.cur) { $("#stInfo").textContent = ""; $("#stWords").textContent = ""; return; }
    const text = S.cur.text;
    const links = (text.match(/\[\[[^\[\]\n]+?\]\]/g) || []).length;
    $("#stInfo").textContent = `リンク ${links} ・ バックリンク ${(S.cur.backlinks || []).length}`;
    $("#stWords").textContent = `${text.replace(/\s/g, "").length.toLocaleString()} 文字`;
  }

  /** 現在のノートの本文を関数で書き換えて保存する（AI の提案の反映など）。 */
  function mutateCurrent(fn) {
    if (!S.cur) return;
    const ta = $("#editor");
    const next = fn(S.cur.text);
    if (next === S.cur.text) return;
    S.cur.text = next;
    if (ta) { const p = ta.selectionStart; ta.value = next; ta.setSelectionRange(Math.min(p, next.length), Math.min(p, next.length)); }
    else renderPreview();
    markDirty(); save();
  }

  // ------------------------------------------------------------ 保存
  function setSaveStatus(kind, msg) {
    const el = $("#stSave");
    if (kind === "err") el.innerHTML = `<span class="dot err"></span>${esc(msg || "保存できませんでした")}`;
    else if (S.conflict) el.innerHTML = `<span class="dot err"></span>競合`;
    else if (S.dirty || S.saving) el.innerHTML = `<span class="dot dirty"></span>${S.saving ? "保存中…" : "未保存"}`;
    else el.innerHTML = `<span class="dot"></span>保存済み`;
  }
  function markDirty() {
    S.dirty = true; setSaveStatus(); updateStats();
    clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 900);
  }
  function save() {
    clearTimeout(S.saveTimer);
    if (!S.cur || !S.dirty || S.conflict) return Promise.resolve();
    if (S.saving) { S.saveAgain = true; return S.saving; }
    const path = S.cur.path, text = S.cur.text;
    setSaveStatus("saving");
    S.saving = api.post("/api/note/save", { path, text, base_version: S.cur.version })
      .then((r) => {
        if (S.cur && S.cur.path === path) { S.cur.version = r.version; if (S.cur.text === text) S.dirty = false; }
        afterSave(path);
      })
      .catch((e) => {
        if (e.status === 409 && S.cur && S.cur.path === path) { S.conflict = { text: e.data.current_text, version: e.data.current_version }; $("#conflict").hidden = false; }
        else { setSaveStatus("err", e.message); toast(e.message, true); }
      })
      .finally(() => {
        S.saving = null; setSaveStatus();
        if (S.saveAgain || (S.dirty && !S.conflict)) { S.saveAgain = false; if (S.dirty) S.saveTimer = setTimeout(save, 300); }
      });
    return S.saving;
  }
  async function flushSave() {
    clearTimeout(S.saveTimer);
    if (S.dirty && !S.conflict) await save();
    if (S.saving) await S.saving;
  }
  let afterTimer;
  function afterSave(path) {
    clearTimeout(afterTimer);
    afterTimer = setTimeout(async () => {
      try {
        await loadTree();
        S.mtimes.set(path, (S.tree.find((n) => n.path === path) || {}).mtime_ns);
        if (S.cur && S.cur.path === path) {
          const info = await api.get("/api/links", { path });
          Object.assign(S.cur, info);
          updateStats();
          if (S.rtab !== "ai") renderRight();
        }
      } catch { /* 次のポーリングで直る */ }
    }, 400);
  }
  $("#cfTheirs").onclick = () => {
    Object.assign(S.cur, { text: S.conflict.text, version: S.conflict.version });
    S.conflict = null; S.dirty = false; renderMain(); setSaveStatus();
  };
  $("#cfMine").onclick = () => {
    S.cur.version = S.conflict.version; S.conflict = null; $("#conflict").hidden = true; S.dirty = true; save();
  };
  window.addEventListener("beforeunload", (e) => { if (S.dirty || S.saving) { save(); e.preventDefault(); e.returnValue = ""; } });

  // 外部エディタでの変更を拾う
  async function poll() {
    if (document.hidden) return;
    try {
      const before = S.cur ? S.mtimes.get(S.cur.path) : null;
      await loadTree();
      if (!S.cur) return;
      const now = S.mtimes.get(S.cur.path);
      if (now === undefined) {
        if (!S.tree.some((n) => n.path === S.cur.path)) { toast(`「${S.cur.title}」は別の場所で削除または移動されました`, true); }
        return;
      }
      if (before !== undefined && now !== before && !S.dirty && !S.saving) {
        const d = await api.get("/api/note", { path: S.cur.path });
        if (d.version !== S.cur.version) { S.cur = d; renderMain(); renderRight(); toast("外部の変更を読み込みました"); }
      }
    } catch { /* サーバ停止中など */ }
  }
  setInterval(poll, 4000);

  // ------------------------------------------------------------ クリック（リンク・タグ・カード）
  document.addEventListener("click", async (e) => {
    const wl = e.target.closest(".wl");
    if (wl) { e.preventDefault(); if (wl.dataset.path) openNote(wl.dataset.path, { heading: wl.dataset.heading }); else if (wl.dataset.target) openTarget(wl.dataset.target, wl.dataset.heading); else if (wl.dataset.heading) scrollToHeading(wl.dataset.heading); return; }
    const tag = e.target.closest(".tag[data-tag]");
    if (tag) { showPanel("tags"); selectTag(tag.dataset.tag); return; }
    const op = e.target.closest("[data-open]");
    if (op && !e.target.closest("[data-act]")) { openNote(op.dataset.open, { heading: op.dataset.heading || "" }); }
  });
  $("#body").addEventListener("change", (e) => {
    const cb = e.target.closest('input[type=checkbox][data-line]');
    if (!cb || !S.cur) return;
    const i = +cb.dataset.line;
    mutateCurrent((t) => {
      const L = t.split("\n");
      if (L[i] !== undefined) L[i] = cb.checked ? L[i].replace(/\[ \]/, "[x]") : L[i].replace(/\[[xX]\]/, "[ ]");
      return L.join("\n");
    });
  });

  // ------------------------------------------------------------ 名前変更・削除・作成
  async function renameNote(path, newPath) {
    await flushSave();
    try {
      const r = await api.post("/api/note/rename", { path, new_path: newPath, update_links: true });
      await loadTree();
      if (S.cur && S.cur.path === path) await openNote(r.path, { push: false });
      else if (S.cur && r.updated.includes(S.cur.path)) await openNote(S.cur.path, { push: false });
      toast(r.updated.length ? `名前を変更し、${r.updated.length} 件のノートのリンクを更新しました` : "名前を変更しました");
      return r.path;
    } catch (e) { fail(e); if (S.cur) $("#titleIn").value = S.cur.title; return null; }
  }
  $("#titleIn").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
    if (e.key === "Escape") { e.target.value = S.cur.title; e.target.blur(); }
  });
  $("#titleIn").addEventListener("blur", async (e) => {
    if (!S.cur) return;
    const v = e.target.value.trim();
    if (!v || v === S.cur.title) { e.target.value = S.cur.title; return; }
    await renameNote(S.cur.path, (S.cur.folder ? S.cur.folder + "/" : "") + v);
  });

  async function deleteNote(path) {
    const ok = await confirmBox("ノートを削除", `「${esc(titleOf(path))}」を削除しますか？<br><span class="hint">Vault 内の .mycel/trash に移動します。</span>`, "削除する", true);
    if (!ok) return;
    try {
      if (S.cur && S.cur.path === path) { clearTimeout(S.saveTimer); S.dirty = false; }
      await api.post("/api/note/delete", { path });
      await loadTree();
      if (S.cur && S.cur.path === path) { S.cur = null; renderMain(); renderRight(); }
      toast("削除しました");
    } catch (e) { fail(e); }
  }

  async function newNote(folder = null, template = "") {
    const f = folder ?? (S.cur ? S.cur.folder : S.selFolder);
    try {
      const r = await api.post("/api/note/create", { folder: f || "", template });
      await loadTree();
      await openNote(r.path, { mode: "edit" });
      const ti = $("#titleIn"); ti.focus(); ti.select();
    } catch (e) { fail(e); }
  }
  $("#btnNewNote").onclick = () => newNote();
  $("#btnNewFolder").onclick = async () => {
    const name = await promptBox("新しいフォルダ", "フォルダ名（/ で階層）", S.selFolder ? S.selFolder + "/" : "");
    if (name && name.trim()) newNote(name.trim().replace(/^\/+|\/+$/g, ""));
  };
  async function daily() {
    try { const r = await api.post("/api/daily"); if (r.created) await loadTree(); await openNote(r.path); } catch (e) { fail(e); }
  }
  $("#btnDaily").onclick = daily;

  async function moveNote(path) {
    const opts = ["", ...S.folders].map((f) => `<option value="${esc(f)}"${f === folderOf(path) ? " selected" : ""}>${f ? esc(f) : "（Vault 直下）"}</option>`).join("");
    const m = modal({
      title: "フォルダへ移動",
      body: `<div class="form"><label for="mvSel">移動先</label><select id="mvSel">${opts}</select><label for="mvNew">新しいフォルダ</label><input id="mvNew" type="text" placeholder="（入力すると優先）"></div>`,
      buttons: [{ label: "キャンセル" }, { label: "移動", primary: true, onClick: () => { const f = ($("#mvNew").value.trim() || $("#mvSel").value).replace(/^\/+|\/+$/g, ""); renameNote(path, (f ? f + "/" : "") + titleOf(path)); } }],
    });
    return m;
  }

  // ------------------------------------------------------------ メニュー
  function popMenu(x, y, items) {
    closeMenus();
    const m = document.createElement("div");
    m.className = "menu"; m.setAttribute("role", "menu");
    m.innerHTML = items.map((it, i) => (it === "-" ? "<hr>" : `<button data-i="${i}" class="${it.danger ? "danger" : ""}" role="menuitem">${esc(it.label)}${it.key ? `<small>${esc(it.key)}</small>` : ""}</button>`)).join("");
    document.body.appendChild(m);
    const r = m.getBoundingClientRect();
    m.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
    m.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
    m.addEventListener("click", (e) => { const b = e.target.closest("[data-i]"); if (b) { closeMenus(); items[+b.dataset.i].run(); } });
    setTimeout(() => document.addEventListener("click", closeMenus, { once: true }), 0);
    const first = m.querySelector("button"); if (first) first.focus();
    m.addEventListener("keydown", (e) => {
      const bs = $$("button", m), i = bs.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); bs[(i + 1) % bs.length].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); bs[(i - 1 + bs.length) % bs.length].focus(); }
      if (e.key === "Escape") closeMenus();
    });
  }
  function closeMenus() { $$(".menu").forEach((m) => m.remove()); }

  function noteMenu(x, y, path) {
    const isCur = S.cur && S.cur.path === path;
    popMenu(x, y, [
      { label: "開く", run: () => openNote(path) },
      { label: "名前を変更", run: async () => { if (!isCur) await openNote(path); const t = $("#titleIn"); t.focus(); t.select(); } },
      { label: "フォルダへ移動…", run: () => moveNote(path) },
      { label: "パスをコピー", run: () => copyText(path) },
      "-",
      { label: "削除", danger: true, run: () => deleteNote(path) },
    ]);
  }
  $("#btnMore").onclick = (e) => {
    if (!S.cur) return;
    const r = e.currentTarget.getBoundingClientRect();
    popMenu(r.right - 230, r.bottom + 4, [
      { label: "名前を変更", key: "F2", run: () => { const t = $("#titleIn"); t.focus(); t.select(); } },
      { label: "フォルダへ移動…", run: () => moveNote(S.cur.path) },
      { label: "テンプレートを挿入…", run: insertTemplate },
      "-",
      { label: "AI: 選択範囲を整える", run: () => aiTransform("tidy") },
      { label: "AI: アクションを抽出", run: () => aiTransform("actions") },
      { label: "AI: メール文にする", run: () => aiTransform("email") },
      { label: "AI: 指示して書き換え…", run: () => aiTransform("") },
      "-",
      { label: "パスをコピー", run: () => copyText(S.cur.path) },
      { label: "削除", danger: true, run: () => deleteNote(S.cur.path) },
    ]);
  };

  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); toast("コピーしました"); } catch { promptBox("コピー", "選択してコピーしてください", t); }
  }

  // ------------------------------------------------------------ モーダル
  function modal({ title, body, buttons = [{ label: "閉じる" }], wide = false, onClose }) {
    const ov = document.createElement("div");
    ov.className = "overlay";
    ov.innerHTML = `<div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><header><span class="t">${esc(title)}</span><button class="ibtn" data-x title="閉じる">${icon('<path d="M6 6l12 12M18 6L6 18"/>')}</button></header><div class="mb"></div>${buttons.length ? "<footer></footer>" : ""}</div>`;
    const mb = $(".mb", ov);
    if (typeof body === "string") mb.innerHTML = body; else if (body) mb.appendChild(body);
    const close = () => { ov.remove(); document.removeEventListener("keydown", onKey, true); if (onClose) onClose(); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    buttons.forEach((b) => {
      const el = document.createElement("button");
      el.className = "btn" + (b.primary ? " pri" : "") + (b.danger ? " danger" : "");
      el.textContent = b.label;
      el.onclick = async () => { if (b.onClick) { const keep = await b.onClick(); if (keep === false) return; } close(); };
      $("footer", ov).appendChild(el);
    });
    $("[data-x]", ov).onclick = close;
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
    document.body.appendChild(ov);
    const f = $("input, select, textarea", mb) || $("footer .pri", ov); if (f) f.focus();
    return { el: ov, body: mb, close };
  }
  function promptBox(title, label, value = "") {
    return new Promise((res) => {
      let done = false;
      const m = modal({
        title, body: `<div class="form"><label for="pIn">${esc(label)}</label><input id="pIn" type="text" value="${esc(value)}"></div>`,
        buttons: [{ label: "キャンセル" }, { label: "OK", primary: true, onClick: () => { done = true; res($("#pIn").value); } }],
        onClose: () => { if (!done) res(null); },
      });
      $("#pIn", m.el).addEventListener("keydown", (e) => { if (e.key === "Enter") { done = true; res(e.target.value); m.close(); } });
    });
  }
  function confirmBox(title, html, okLabel = "OK", danger = false) {
    return new Promise((res) => {
      let done = false;
      modal({ title, body: `<p style="margin:0">${html}</p>`, buttons: [{ label: "キャンセル" }, { label: okLabel, primary: !danger, danger, onClick: () => { done = true; res(true); } }], onClose: () => { if (!done) res(false); } });
    });
  }

  // ------------------------------------------------------------ [[ の補完
  let ac = null;
  function caretXY(ta, pos) {
    const div = document.createElement("div");
    const cs = getComputedStyle(ta);
    for (const p of ["fontFamily", "fontSize", "lineHeight", "paddingTop", "paddingLeft", "paddingRight", "borderWidth", "letterSpacing", "tabSize", "boxSizing"]) div.style[p] = cs[p];
    Object.assign(div.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", wordWrap: "break-word", width: ta.clientWidth + "px", top: 0, left: 0 });
    div.textContent = ta.value.slice(0, pos);
    const span = document.createElement("span"); span.textContent = "​"; div.appendChild(span);
    document.body.appendChild(div);
    const r = { x: span.offsetLeft, y: span.offsetTop + parseFloat(cs.lineHeight || 20) };
    div.remove();
    return r;
  }
  function acUpdate() {
    const ta = $("#editor"); if (!ta) return;
    const pos = ta.selectionStart, before = ta.value.slice(Math.max(0, pos - 120), pos);
    const m = before.match(/\[\[([^\[\]\n|#]*)$/);
    if (!m || ta.selectionEnd !== pos) return acClose();
    const q = m[1].toLowerCase();
    let items = S.tree.filter((n) => n.path.toLowerCase().includes(q) && (!S.cur || n.path !== S.cur.path));
    items.sort((a, b) => (a.title.toLowerCase().startsWith(q) ? 0 : 1) - (b.title.toLowerCase().startsWith(q) ? 0 : 1) || a.title.length - b.title.length);
    items = items.slice(0, 12);
    if (m[1].trim() && !resolve(m[1])) items.push({ title: m[1].trim(), path: "", create: true });
    if (!items.length) return acClose();
    if (!ac) { ac = { el: document.createElement("div"), sel: 0 }; ac.el.className = "ac"; $("#body").appendChild(ac.el); ac.el.addEventListener("mousedown", (e) => { const d = e.target.closest("[data-i]"); if (d) { e.preventDefault(); ac.sel = +d.dataset.i; acPick(); } }); }
    ac.items = items; ac.start = pos - m[1].length; ac.sel = Math.min(ac.sel, items.length - 1);
    ac.el.innerHTML = items.map((n, i) => `<div data-i="${i}" class="${i === ac.sel ? "sel" : ""}">${esc(n.title)}<small>${n.create ? "新しいリンク" : esc(n.folder || "")}</small></div>`).join("");
    const xy = caretXY(ta, pos);
    const body = $("#body");
    ac.el.style.left = Math.min(xy.x, body.clientWidth - 260) + "px";
    ac.el.style.top = (xy.y - ta.scrollTop + body.scrollTop + 4) + "px";
  }
  function acClose() { if (ac) { ac.el.remove(); ac = null; } }
  function acPick() {
    const ta = $("#editor"); const it = ac.items[ac.sel]; if (!ta || !it) return;
    const pos = ta.selectionStart, after = ta.value.slice(pos);
    const dup = S.tree.filter((n) => n.title.toLowerCase() === it.title.toLowerCase()).length > 1;
    const name = dup && it.path ? it.path.slice(0, -3) : it.title;
    const close = after.startsWith("]]") ? "" : "]]";
    ta.setRangeText(name + close, ac.start, pos, "end");
    if (!close) ta.setSelectionRange(ac.start + name.length + 2, ac.start + name.length + 2);
    acClose();
    ta.dispatchEvent(new Event("input"));
    acClose();
  }

  function editorKeys(e) {
    const ta = e.target;
    if (ac) {
      if (e.key === "ArrowDown") { e.preventDefault(); ac.sel = (ac.sel + 1) % ac.items.length; acUpdate(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); ac.sel = (ac.sel - 1 + ac.items.length) % ac.items.length; acUpdate(); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); acPick(); return; }
      if (e.key === "Escape") { e.preventDefault(); acClose(); return; }
    }
    if (e.key === "Tab") {
      e.preventDefault();
      const s = ta.selectionStart, en = ta.selectionEnd, v = ta.value;
      const ls = v.lastIndexOf("\n", s - 1) + 1;
      const block = v.slice(ls, en);
      const out = e.shiftKey ? block.replace(/^( {1,2}|\t)/gm, "") : block.replace(/^/gm, "  ");
      ta.setRangeText(out, ls, en, "preserve");
      if (s === en) { const d = out.length - block.length; ta.setSelectionRange(Math.max(ls, s + d), Math.max(ls, s + d)); }
      ta.dispatchEvent(new Event("input"));
    }
    if (e.key === "Enter" && !e.isComposing && !e.shiftKey) {
      // 箇条書きの継続
      const s = ta.selectionStart, v = ta.value, ls = v.lastIndexOf("\n", s - 1) + 1, line = v.slice(ls, s);
      const m = line.match(/^(\s*)([-*+]|\d+[.)])(\s+\[[ xX]\])?\s+(.*)$/);
      if (m && ta.selectionEnd === s) {
        e.preventDefault();
        if (!m[4].trim()) { ta.setRangeText("", ls, s, "end"); }
        else {
          let bullet = m[2]; const num = bullet.match(/^(\d+)([.)])$/); if (num) bullet = (+num[1] + 1) + num[2];
          ta.setRangeText(`\n${m[1]}${bullet}${m[3] ? " [ ]" : ""} `, s, s, "end");
        }
        ta.dispatchEvent(new Event("input"));
      }
    }
  }

  // ------------------------------------------------------------ 右パネル
  function renderRight() {
    $$(".rtabs button").forEach((b) => b.classList.toggle("on", b.dataset.r === S.rtab));
    if (S.graph && S.rtab !== "graph") { S.graph.destroy(); S.graph = null; }
    const p = $("#rpane");
    if (!S.cur) { p.innerHTML = '<div class="hint" style="padding:8px 2px">ノートを開くと、ここにリンクやグラフ、AI の結果が出ます。</div>'; return; }
    if (S.rtab === "links") renderLinks(p);
    else if (S.rtab === "graph") renderGraphPane(p);
    else renderAI(p);
  }
  $$(".rtabs button").forEach((b) => (b.onclick = () => { S.rtab = b.dataset.r; store.set("rtab", S.rtab); renderRight(); }));

  function markTitle(text, title) {
    const safe = esc(text); const t = esc(title);
    const i = safe.toLowerCase().indexOf(t.toLowerCase());
    return i < 0 ? safe : safe.slice(0, i) + "<mark>" + safe.slice(i, i + t.length) + "</mark>" + safe.slice(i + t.length);
  }

  function renderLinks(p) {
    const c = S.cur;
    const bl = c.backlinks || [], un = c.unlinked || [], out = c.outgoing || [];
    let h = `<div class="rh"><span>バックリンク</span><span>${bl.length}</span></div>`;
    h += bl.length ? bl.map((b) => `<button class="card" data-open="${esc(b.path)}"><b>${esc(b.title)}</b><span>${markTitle(b.context.replace(/\[\[([^\]|#]+)(#[^\]|]*)?(\|([^\]]*))?\]\]/g, (m, t, hh, a, al) => al || t), c.title)}</span></button>`).join("") : '<div class="hint">まだどこからもリンクされていません。</div>';
    h += `<div class="rh"><span>未リンクの言及</span><span>${un.length}</span></div>`;
    h += un.length ? un.map((u) => `<div class="card" data-open="${esc(u.path)}"><b>${esc(u.title)}</b><span>${markTitle(u.context, c.title)}</span><div class="acts"><button class="btn sm" data-act="link" data-path="${esc(u.path)}">リンクにする</button></div></div>`).join("") : '<div class="hint">ありません。</div>';
    const uniq = [...new Map(out.map((o) => [o.target.toLowerCase(), o])).values()];
    h += `<div class="rh"><span>このノートからのリンク</span><span>${uniq.length}</span></div>`;
    h += uniq.length ? `<div class="srcs">${uniq.map((o) => `<a class="wl chip${o.path ? "" : " unres"}" data-target="${esc(o.target)}"${o.path ? ` data-path="${esc(o.path)}"` : ""}>${esc(o.target)}</a>`).join("")}</div>` : '<div class="hint">ありません。</div>';
    if ((c.tags || []).length) h += `<div class="rh"><span>タグ</span></div><div class="srcs">${c.tags.map((t) => `<span class="chip tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join("")}</div>`;
    const heads = c.headings || [];
    if (heads.length) {
      const min = Math.min(...heads.map((x) => x.level));
      h += `<div class="rh"><span>アウトライン</span></div><div class="outline">${heads.map((x) => `<a class="wl-h" data-h="${esc(x.text)}" style="padding-left:${(x.level - min) * 12}px">${esc(x.text)}</a>`).join("")}</div>`;
    }
    p.innerHTML = h;
    $$(".wl-h", p).forEach((a) => (a.onclick = () => scrollToHeading(a.dataset.h)));
    $$('[data-act="link"]', p).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); linkMention(b.dataset.path); }));
  }

  /** 別ノートの中の「タイトルそのままの文字」を [[リンク]] に置き換える。 */
  async function linkMention(path) {
    const title = S.cur.title;
    try {
      const d = await api.get("/api/note", { path });
      const lines = d.text.split("\n");
      let done = false, fence = false;
      for (let i = 0; i < lines.length && !done; i++) {
        if (/^\s*(```|~~~)/.test(lines[i])) { fence = !fence; continue; }
        if (fence) continue;
        const parts = lines[i].split(/(\[\[[^\]]*\]\]|`[^`]*`)/);
        for (let j = 0; j < parts.length; j += 2) {
          const k = parts[j].indexOf(title);
          if (k >= 0) { parts[j] = parts[j].slice(0, k) + `[[${title}]]` + parts[j].slice(k + title.length); done = true; break; }
        }
        lines[i] = parts.join("");
      }
      if (!done) { toast("置き換える箇所が見つかりませんでした", true); return; }
      await api.post("/api/note/save", { path, text: lines.join("\n"), base_version: d.version });
      const info = await api.get("/api/links", { path: S.cur.path });
      Object.assign(S.cur, info); renderRight(); updateStats();
      toast(`「${d.title}」にリンクを追加しました`);
    } catch (e) { fail(e); }
  }

  async function renderGraphPane(p) {
    p.innerHTML = `<div class="gctl"><span>範囲</span><div class="seg">${[1, 2, 0].map((d) => `<button data-d="${d}" class="${S.depth === d ? "on" : ""}">${d ? d + " ホップ" : "全体"}</button>`).join("")}</div><span style="flex:1"></span><button class="btn sm" id="gBig">拡大</button></div><canvas class="graph" id="gSmall" aria-label="ノートのつながり。ノードをクリックで開く"></canvas><div class="hint" style="margin-top:8px">ドラッグで移動、ホイールで拡大縮小。薄い輪は未作成のリンクです。色はフォルダごと。</div>`;
    $$("[data-d]", p).forEach((b) => (b.onclick = () => { S.depth = +b.dataset.d; store.set("depth", S.depth); renderGraphPane(p); }));
    $("#gBig").onclick = () => bigGraph(S.cur ? S.cur.path : null);
    if (S.graph) S.graph.destroy();
    S.graph = new ForceGraph($("#gSmall"), { onOpen: graphOpen });
    try {
      const data = await api.get("/api/graph", S.depth ? { path: S.cur.path, depth: S.depth } : {});
      if (S.graph) S.graph.setData(data, S.cur.path);
    } catch (e) { fail(e); }
  }
  function graphOpen(path, title) { if (path) openNote(path); else openTarget(title); }
  async function bigGraph(center) {
    const m = modal({ title: "グラフ（Vault 全体）", body: '<canvas class="graph big" id="gBigC" aria-label="Vault 全体のグラフ"></canvas>', buttons: [], wide: true, onClose: () => g.destroy() });
    m.body.style.padding = "0"; m.body.style.overflow = "hidden";
    const g = new ForceGraph($("#gBigC"), { onOpen: (p, t) => { m.close(); graphOpen(p, t); } });
    try { g.setData(await api.get("/api/graph"), center); } catch (e) { fail(e); }
  }
  $("#btnGraph").onclick = () => bigGraph(S.cur ? S.cur.path : null);

  // ---- AI
  async function renderAI(p) {
    let st = S.state.llm || {};
    p.innerHTML = `<div class="rh"><span>ノートに質問</span><span id="aiMode"></span></div>
      ${st.chat ? "" : `<div class="notice">LLM が未設定です。関連ノートの検索だけ動きます。<br><button class="btn sm" style="margin-top:6px" id="aiSetup">LLM を設定する</button></div>`}
      <div class="chat" id="chat"></div>
      <div class="askbox"><textarea id="askIn" placeholder="例: A社の決裁者が気にしていることは？（Ctrl+Enter で送信）"></textarea>
        <div class="row"><label><input type="checkbox" id="askCur" checked> 開いているノートを含める</label><span><button class="btn sm" id="chatClear" title="会話を消す">クリア</button> <button class="btn sm pri" id="askGo">質問</button></span></div></div>
      <div class="rh"><span>このノート</span></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn sm" id="aiSum"${st.chat ? "" : " disabled"}>要約とタグ提案</button><button class="btn sm" id="aiSug">リンク候補を探す</button></div>
      <div id="aiOut" style="margin-top:8px"></div>
      <div class="rh"><span>意味検索インデックス</span></div><div id="aiIdx" class="hint">確認中…</div>`;
    const chat = $("#chat");
    const drawChat = () => {
      chat.innerHTML = S.chat.map((m) => m.role === "user" ? `<div class="msg-q">${esc(m.content)}</div>` :
        `<div class="msg-a">${m.pending ? '<span class="spin"></span> 考えています…' : `<div class="md">${m.content ? MD.render(m.content, { resolve }) : `<span class="hint">${esc(m.message || "")}</span>`}</div>`}${(m.sources || []).length ? `<div class="srcs">${m.sources.map((s) => `<button class="chip" data-open="${esc(s.path)}" data-heading="${esc(s.heading && s.heading !== s.title ? s.heading : "")}" title="${esc(s.path)}">[${s.n}] ${esc(s.title)}${s.heading && s.heading !== s.title ? " › " + esc(s.heading) : ""}</button>`).join("")}</div>` : ""}</div>`).join("");
    };
    drawChat();
    const ask = async () => {
      const q = $("#askIn").value.trim(); if (!q) return;
      $("#askIn").value = "";
      const history = S.chat.filter((m) => !m.pending && m.content).map((m) => ({ role: m.role, content: m.content }));
      S.chat.push({ role: "user", content: q }); const a = { role: "assistant", pending: true }; S.chat.push(a); drawChat();
      try {
        const r = await api.post("/api/ai/ask", { question: q, path: $("#askCur").checked ? S.cur.path : "", history });
        Object.assign(a, { pending: false, content: r.answer, sources: r.sources, message: r.message });
      } catch (e) { Object.assign(a, { pending: false, content: "", message: e.message }); }
      drawChat();
      p.scrollTop = chat.offsetTop + chat.scrollHeight;
    };
    $("#askGo").onclick = ask;
    $("#askIn").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ask(); } });
    $("#chatClear").onclick = () => { S.chat = []; drawChat(); };
    if ($("#aiSetup")) $("#aiSetup").onclick = () => openSettings("llm");
    $("#aiSum").onclick = async () => {
      const out = $("#aiOut"); out.innerHTML = '<span class="spin"></span> 要約しています…';
      try {
        const r = await api.post("/api/ai/summarize", { path: S.cur.path });
        const have = new Set((S.cur.tags || []).map((t) => t.toLowerCase()));
        out.innerHTML = `<div class="card" style="cursor:default"><span style="color:var(--ink);white-space:pre-wrap">${esc(r.summary)}</span><div class="acts"><button class="btn sm" id="sumIns">ノートに追記</button></div>${r.tags.length ? `<div class="srcs">${r.tags.map((t) => have.has(t.toLowerCase()) ? `<span class="chip">#${esc(t)} ✓</span>` : `<button class="chip add" data-addtag="${esc(t)}">＋ #${esc(t)}</button>`).join("")}</div>` : ""}</div>`;
        $("#sumIns").onclick = () => mutateCurrent((t) => insertAfterTitle(t, "> **要約** " + r.summary.replace(/\n+/g, " ")));
        $$("[data-addtag]", out).forEach((b) => (b.onclick = () => { addTag(b.dataset.addtag); b.outerHTML = `<span class="chip">#${esc(b.dataset.addtag)} ✓</span>`; }));
      } catch (e) { out.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
    };
    $("#aiSug").onclick = async () => {
      const out = $("#aiOut"); out.innerHTML = '<span class="spin"></span> 探しています…';
      try {
        const r = await api.get("/api/ai/suggest", { path: S.cur.path });
        out.innerHTML = r.suggestions.length ? r.suggestions.map((s) => `<div class="card" data-open="${esc(s.path)}"><b>${esc(s.title)}</b><span>${esc(s.snippet.replace(/\s+/g, " "))}</span><div class="acts"><button class="btn sm" data-act="rel" data-t="${esc(s.title)}">関連に追記</button></div></div>`).join("") : '<div class="hint">候補は見つかりませんでした。</div>';
        $$('[data-act="rel"]', out).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); addRelated(b.dataset.t); b.disabled = true; b.textContent = "追記しました"; }));
      } catch (e) { out.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
    };
    try {
      const s = await api.get("/api/ai/status");
      $("#aiMode").textContent = s.retrieval === "hybrid" ? "意味＋キーワード検索" : "キーワード検索";
      const idx = $("#aiIdx"); if (!idx) return;
      if (!s.embed) idx.innerHTML = "埋め込みモデルが未設定のため、キーワード検索で関連ノートを探しています。設定の「LLM」で Embed モデルを登録すると意味検索も併用します。";
      else {
        const job = s.job;
        idx.innerHTML = `${s.embedded} / ${s.chunks} 区画を登録済み。${job.state === "running" ? `<span class="spin"></span> 作成中 ${job.done}/${job.total}` : job.state === "error" ? `<span style="color:var(--danger)">${esc(job.message)}</span>` : ""} <button class="btn sm" id="embBuild"${job.state === "running" ? " disabled" : ""}>更新</button>`;
        $("#embBuild").onclick = async () => { try { await api.post("/api/ai/reindex"); toast("意味検索インデックスを作成しています"); setTimeout(() => S.rtab === "ai" && renderAI(p), 1500); } catch (e) { fail(e); } };
        if (job.state === "running") setTimeout(() => S.rtab === "ai" && $("#aiIdx") && renderAIStatusOnly(), 2000);
      }
    } catch { /* 無視 */ }
  }
  function renderAIStatusOnly() { if (S.rtab === "ai") renderAI($("#rpane")); }

  function insertAfterTitle(text, line) {
    const L = text.split("\n");
    const fm = MD.splitFrontmatter(text).start;
    let i = L.findIndex((l, k) => k >= fm && /^#\s/.test(l));
    i = i < 0 ? fm : i + 1;
    L.splice(i, 0, line, "");
    return L.join("\n");
  }
  function addTag(tag) {
    mutateCurrent((t) => {
      const L = t.replace(/\s+$/, "").split("\n");
      const last = L[L.length - 1] || "";
      if (/^(#[^\s#]+\s*)+$/.test(last.trim())) L[L.length - 1] = last.trim() + " #" + tag;
      else L.push("", "#" + tag);
      return L.join("\n") + "\n";
    });
  }
  function addRelated(title) {
    mutateCurrent((t) => {
      const L = t.split("\n");
      const h = L.findIndex((l) => /^##\s+関連\s*$/.test(l));
      if (h >= 0) { let j = h + 1; while (j < L.length && /^\s*[-*]\s/.test(L[j])) j++; L.splice(j, 0, `- [[${title}]]`); return L.join("\n"); }
      return t.replace(/\s+$/, "") + `\n\n## 関連\n- [[${title}]]\n`;
    });
  }

  async function aiTransform(preset) {
    if (!S.cur) return;
    if (!(S.state.llm || {}).chat) { toast("LLM が未設定です（設定の「LLM」から登録してください）", true); return; }
    const ta = $("#editor");
    const hasSel = ta && ta.selectionEnd > ta.selectionStart;
    const range = hasSel ? [ta.selectionStart, ta.selectionEnd] : null;
    const src = hasSel ? ta.value.slice(range[0], range[1]) : S.cur.text;
    let instruction = "";
    if (!preset) { instruction = await promptBox("AI で書き換え", "指示（例: 敬体にして、見出しを付けて）"); if (!instruction) return; }
    const m = modal({ title: hasSel ? "AI の提案（選択範囲）" : "AI の提案（ノート全体）", body: '<span class="spin"></span> 生成しています…', buttons: [] });
    try {
      const r = await api.post("/api/ai/transform", { text: src, preset, instruction });
      m.close();
      const buttons = [{ label: "閉じる" }, { label: "コピー", onClick: () => { copyText(r.text); return false; } }, { label: "末尾に追加", onClick: () => mutateCurrent((t) => t.replace(/\s+$/, "") + "\n\n" + r.text + "\n") }];
      if (hasSel) buttons.push({ label: "選択範囲を置き換え", primary: true, onClick: () => mutateCurrent((t) => t.slice(0, range[0]) + r.text + t.slice(range[1])) });
      modal({ title: "AI の提案", body: `<div class="result">${esc(r.text)}</div>${hasSel ? "" : '<p class="hint">編集モードで文章を選択してから実行すると、選択範囲だけを置き換えられます。</p>'}`, buttons });
    } catch (e) { m.close(); fail(e); }
  }

  async function insertTemplate() {
    try {
      const { templates } = await api.get("/api/templates");
      if (!templates.length) { toast(`テンプレートがありません（「${S.state.template_folder || "テンプレート"}」フォルダにノートを置いてください）`, true); return; }
      openPalette(templates.map((t) => ({ label: t.title, hint: "テンプレート", run: async () => {
        const r = await api.post("/api/template/render", { template: t.path, title: S.cur.title });
        const ta = $("#editor");
        if (ta) { const p = ta.selectionStart; mutateCurrent((x) => x.slice(0, p) + r.text + x.slice(p)); }
        else mutateCurrent((x) => x.replace(/\s+$/, "") + "\n\n" + r.text);
      } })), "テンプレートを選ぶ");
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ サイドパネル（検索・タグ・最近）
  function showPanel(name) {
    S.panel = name;
    app.classList.remove("no-side");
    $$(".rail [data-panel]").forEach((b) => b.classList.toggle("on", b.dataset.panel === name));
    $$("#side .pane").forEach((p) => (p.hidden = p.dataset.pane !== name));
    if (name === "search") { const i = $("#searchIn"); i.focus(); i.select(); }
    if (name === "tags") loadTags();
    if (name === "recent") loadRecent();
  }
  $$(".rail [data-panel]").forEach((b) => (b.onclick = () => {
    if (S.panel === b.dataset.panel && !app.classList.contains("no-side")) { app.classList.add("no-side"); b.classList.remove("on"); return; }
    showPanel(b.dataset.panel);
  }));

  let searchTimer;
  $("#searchIn").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });
  async function runSearch() {
    const q = $("#searchIn").value.trim(), el = $("#searchRes");
    if (!q) { el.innerHTML = '<div class="empty">キーワードを入力してください。</div>'; return; }
    try {
      const { results } = await api.get("/api/search", { q });
      const first = q.split(/\s+/)[0];
      el.innerHTML = results.length ? `<div class="empty">${results.length} 件</div>` + results.map((r) => `<button class="res" data-open="${esc(r.path)}"><b>${esc(r.title)}</b><span>${markTitle(r.snippet, first)}</span><small>${esc(folderOf(r.path))}</small></button>`).join("") : '<div class="empty">見つかりませんでした。</div>';
    } catch (e) { fail(e); }
  }

  async function loadTags() {
    try {
      const { tags } = await api.get("/api/tags");
      $("#tagList").innerHTML = tags.length ? tags.map((t) => `<button class="tagrow${S.tag === t.tag ? " on" : ""}" data-tagrow="${esc(t.tag)}"><span>#${esc(t.tag)}</span><span class="n">${t.count}</span></button><div data-tagnotes="${esc(t.tag)}"></div>`).join("") : '<div class="empty">タグはまだありません。本文に #タグ と書くと出てきます。</div>';
      $$("[data-tagrow]").forEach((b) => (b.onclick = () => selectTag(S.tag === b.dataset.tagrow ? "" : b.dataset.tagrow)));
      if (S.tag) selectTag(S.tag);
    } catch (e) { fail(e); }
  }
  async function selectTag(tag) {
    S.tag = tag;
    const rows = $$("[data-tagrow]");
    if (!rows.length) return loadTags();
    rows.forEach((b) => b.classList.toggle("on", b.dataset.tagrow === tag));
    $$("[data-tagnotes]").forEach((d) => (d.innerHTML = ""));
    if (!tag) return;
    const box = $$("[data-tagnotes]").find((d) => d.dataset.tagnotes === tag);
    if (!box) return;
    const { notes } = await api.get("/api/tag", { name: tag });
    box.innerHTML = notes.map((n) => `<button class="res" data-open="${esc(n.path)}" style="padding-left:20px"><b>${esc(n.title)}</b></button>`).join("");
    box.scrollIntoView({ block: "nearest" });
  }

  async function loadRecent() {
    const el = $("#recentList");
    try {
      const { events } = await api.get("/api/plugins/change_journal/recent", { limit: 40 });
      const label = { created: "作成", saved: "更新", deleted: "削除", renamed: "名前変更" };
      el.innerHTML = events.length ? events.map((ev) => {
        const d = new Date(ev.timestamp * 1000);
        const when = d.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" }) + " " + d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
        const exists = S.tree.some((n) => n.path === ev.path);
        return `<button class="res"${exists ? ` data-open="${esc(ev.path)}"` : " disabled"}><b>${esc(titleOf(ev.path))}</b><small>${label[ev.kind] || ev.kind} ・ ${when} ・ ${esc(ev.author)}${ev.old_path ? " ・ 旧: " + esc(titleOf(ev.old_path)) : ""}</small></button>`;
      }).join("") : '<div class="empty">まだ記録がありません。</div>';
    } catch (e) {
      el.innerHTML = `<div class="empty">「変更ジャーナル」プラグインを有効にすると、ここに最近の変更が出ます。<br><button class="btn sm" style="margin-top:8px" id="goPlug">プラグイン設定</button></div>`;
      $("#goPlug").onclick = () => openSettings("plugins");
    }
  }

  // ------------------------------------------------------------ パレット（Ctrl+K）
  function commands() {
    return [
      { label: "新しいノート", key: "Alt+N", run: () => newNote() },
      { label: "今日のデイリーノート", key: "Ctrl+D", run: daily },
      { label: "テンプレートから新規作成…", run: async () => { const { templates } = await api.get("/api/templates"); openPalette(templates.map((t) => ({ label: t.title, hint: "テンプレート", run: () => newNote(null, t.path) })), "テンプレートを選ぶ"); } },
      { label: "閲覧 / 編集を切り替え", key: "Ctrl+E", run: () => setMode(S.mode === "edit" ? "prev" : "edit") },
      { label: "全文検索", key: "Ctrl+Shift+F", run: () => showPanel("search") },
      { label: "グラフ（Vault 全体）", run: () => bigGraph(S.cur ? S.cur.path : null) },
      { label: "AI に質問", run: () => { S.rtab = "ai"; app.classList.remove("no-right"); renderRight(); setTimeout(() => $("#askIn") && $("#askIn").focus(), 50); } },
      { label: "テンプレートを挿入…", run: insertTemplate },
      { label: "設定", run: () => openSettings() },
      { label: "インデックスを作り直す", run: async () => { const r = await api.post("/api/reindex"); await loadTree(); toast(`${r.updated} 件のノートを読み込み直しました`); } },
      { label: "ライト / ダーク切り替え", run: toggleTheme },
    ];
  }
  function score(title, q) {
    const t = title.toLowerCase();
    if (!q) return 1;
    if (t === q) return 100; if (t.startsWith(q)) return 50; const i = t.indexOf(q); if (i >= 0) return 30 - Math.min(i, 20);
    let k = 0; for (const ch of t) { if (ch === q[k]) k++; if (k === q.length) return 5; }
    return 0;
  }
  function openPalette(fixedItems = null, placeholder = "") {
    closeMenus();
    const ov = document.createElement("div");
    ov.className = "overlay";
    ov.innerHTML = `<div class="pal" role="dialog" aria-label="クイックスイッチャー"><input placeholder="${esc(placeholder || "ノート名を入力（> でコマンド）")}" aria-label="検索"><ul role="listbox"></ul><div class="foot">↑↓ で選択 ・ Enter で開く ・ Shift+Enter で新規作成 ・ Esc で閉じる</div></div>`;
    document.body.appendChild(ov);
    const inp = $("input", ov), ul = $("ul", ov);
    let items = [], sel = 0;
    const close = () => ov.remove();
    const build = () => {
      const raw = inp.value.trim(); const q = raw.replace(/^>/, "").trim().toLowerCase();
      if (fixedItems) items = fixedItems.map((it) => ({ ...it, s: score(it.label, q) })).filter((x) => x.s > 0).sort((a, b) => b.s - a.s);
      else if (raw.startsWith(">")) items = commands().map((c) => ({ ...c, hint: c.key || "", s: score(c.label, q) })).filter((x) => x.s > 0).sort((a, b) => b.s - a.s);
      else {
        items = S.tree.map((n) => ({ label: n.title, hint: n.folder, path: n.path, s: Math.max(score(n.title, q), score(n.path, q) * 0.5) }))
          .filter((x) => x.s > 0).sort((a, b) => b.s - a.s || a.label.length - b.label.length).slice(0, 60)
          .map((x) => ({ ...x, run: () => openNote(x.path) }));
        if (raw && !resolve(raw)) items.push({ label: `「${raw}」を新規作成`, hint: "Shift+Enter", run: () => openTarget(raw), create: true });
      }
      sel = Math.min(sel, Math.max(0, items.length - 1));
      ul.innerHTML = items.map((it, i) => `<li data-i="${i}" class="${i === sel ? "sel" : ""}" role="option"><span>${esc(it.label)}</span><small>${esc(it.hint || "")}</small></li>`).join("") || '<li style="color:var(--muted)">見つかりません</li>';
      const s = $("li.sel", ul); if (s) s.scrollIntoView({ block: "nearest" });
    };
    const pick = (it) => { if (!it) return; close(); Promise.resolve(it.run()).catch(fail); };
    inp.addEventListener("input", () => { sel = 0; build(); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % Math.max(1, items.length); build(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + items.length) % Math.max(1, items.length); build(); }
      else if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        if (e.shiftKey && !fixedItems && inp.value.trim()) { close(); openTarget(inp.value.trim()); } else pick(items[sel]);
      } else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    ul.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) pick(items[+li.dataset.i]); });
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
    build(); inp.focus();
  }

  // ------------------------------------------------------------ 設定
  async function openSettings(tab = "general") {
    let cfg, plugins;
    try { [cfg, { plugins }] = await Promise.all([api.get("/api/config"), api.get("/api/plugins")]); } catch (e) { fail(e); return; }
    const v = (k) => esc(cfg[k] ?? "");
    const body = document.createElement("div");
    body.innerHTML = `<div class="mtabs">${[["general", "一般"], ["llm", "LLM"], ["plugins", "プラグイン"]].map(([k, l]) => `<button data-t="${k}">${l}</button>`).join("")}</div>
      <div data-tp="general" class="form" style="padding-top:14px">
        <label for="cVault">Vault の場所</label><input id="cVault" type="text" value="${v("vault_path")}" placeholder="${esc(S.state.vault || "")}">
        <div class="help">ノートを置くフォルダ。空欄なら mycel/vault。変更すると読み込み直します。</div>
        <label for="cUser">作成者名</label><input id="cUser" type="text" value="${v("user_name")}">
        <div class="help">変更履歴に記録されます。将来チームで共有するときに「誰が変えたか」に使います。</div>
        <label for="cDaily">デイリーノートのフォルダ</label><input id="cDaily" type="text" value="${v("daily_folder")}">
        <label for="cTpl">テンプレートのフォルダ</label><input id="cTpl" type="text" value="${v("template_folder")}">
        <div class="help">テンプレートでは {{title}} {{date}} {{time}} {{author}} が置き換わります。</div>
      </div>
      <div data-tp="llm" class="form" style="padding-top:14px">
        <label for="cProv">接続方式</label><select id="cProv"><option value="openai">OpenAI 互換 API（OpenAI / Ollama / LM Studio / vLLM など）</option><option value="azure">Azure OpenAI</option></select>
        <label for="cUrl">Base URL</label><input id="cUrl" type="text" value="${v("base_url")}" placeholder="例: http://127.0.0.1:11434/v1 ・ https://api.openai.com/v1">
        <div class="help" id="urlHelp"></div>
        <label for="cKey">API キー</label><input id="cKey" type="password" autocomplete="off" placeholder="${cfg.has_api_key ? "設定済み（変更する場合だけ入力）" : "ローカル LLM なら空欄で可"}">
        ${cfg.has_api_key ? '<label></label><label style="color:var(--muted)"><input type="checkbox" id="cKeyClr"> 保存済みのキーを消す</label>' : ""}
        <label for="cModel" id="modelLbl">モデル</label><input id="cModel" type="text" value="${v("model")}" placeholder="例: qwen2.5:14b ・ gpt-4o-mini">
        <label for="cVer" class="az">API バージョン</label><input id="cVer" class="az" type="text" value="${v("api_version")}">
        <h4>意味検索（任意）</h4>
        <label for="cEmb">Embed モデル</label><input id="cEmb" type="text" value="${v("embed_model")}" placeholder="例: nomic-embed-text ・ text-embedding-3-small">
        <div class="help">空欄ならキーワード検索だけで関連ノートを探します。</div>
        <label for="cEmbUrl">Embed の Base URL</label><input id="cEmbUrl" type="text" value="${v("embed_base_url")}" placeholder="空欄なら上と同じ">
        <label for="cEmbKey">Embed の API キー</label><input id="cEmbKey" type="password" autocomplete="off" placeholder="${cfg.has_embed_api_key ? "設定済み" : "空欄なら上と同じ"}">
        <h4>詳細</h4>
        <label for="cTemp">Temperature</label><input id="cTemp" type="number" step="0.1" min="0" max="2" value="${v("temperature")}">
        <label for="cMax">Max tokens</label><input id="cMax" type="number" min="64" value="${v("max_tokens")}">
        <label for="cTo">タイムアウト（秒）</label><input id="cTo" type="number" min="5" value="${v("request_timeout")}">
        <label for="cProxy">プロキシ</label><span><label style="color:var(--ink)"><input type="checkbox" id="cUseProxy"${cfg.use_proxy ? " checked" : ""}> プロキシを使う</label></span>
        <label for="cProxyUrl">プロキシ URL</label><input id="cProxyUrl" type="text" value="${v("proxy_url")}" placeholder="空欄なら環境変数 HTTPS_PROXY">
        <label></label><div><button class="btn" id="cTest">保存して接続テスト</button><div class="testres" id="cTestRes"></div></div>
      </div>
      <div data-tp="plugins" style="padding-top:6px">
        <p class="hint">有効にしたプラグインは保存と同時に読み込まれます。プラグインは mycel/plugins/ に置いた Python ファイルです（作り方は PLUGINS.md）。</p>
        ${plugins.map((p) => `<label class="plug"><input type="checkbox" data-plug="${esc(p.id)}"${p.enabled ? " checked" : ""}><b>${esc(p.name)}</b><p>${esc(p.description)}</p>${p.error ? `<p style="color:var(--danger)">${esc(p.error)}</p>` : ""}${p.status && Object.keys(p.status).length ? `<code>${esc(Object.entries(p.status).filter(([, x]) => x !== "" && x !== null).map(([k, x]) => `${k}: ${x}`).join(" ・ "))}</code>` : ""}</label>`).join("")}
      </div>`;
    const collect = () => {
      const d = {
        vault_path: $("#cVault", body).value, user_name: $("#cUser", body).value, daily_folder: $("#cDaily", body).value, template_folder: $("#cTpl", body).value,
        provider: $("#cProv", body).value, base_url: $("#cUrl", body).value, model: $("#cModel", body).value, api_version: $("#cVer", body).value,
        embed_model: $("#cEmb", body).value, embed_base_url: $("#cEmbUrl", body).value, temperature: $("#cTemp", body).value, max_tokens: $("#cMax", body).value,
        request_timeout: $("#cTo", body).value, use_proxy: $("#cUseProxy", body).checked, proxy_url: $("#cProxyUrl", body).value,
        plugins: $$("[data-plug]", body).filter((c) => c.checked).map((c) => c.dataset.plug),
      };
      if ($("#cKey", body).value) d.api_key = $("#cKey", body).value;
      if ($("#cEmbKey", body).value) d.embed_api_key = $("#cEmbKey", body).value;
      if ($("#cKeyClr", body) && $("#cKeyClr", body).checked) d.clear_api_key = true;
      return d;
    };
    const saveCfg = async () => {
      const d = collect();
      const vaultChanged = d.vault_path.trim() !== (cfg.vault_path || "");
      await flushSave();
      try {
        cfg = await api.post("/api/config", d);
        S.state = await api.get("/api/state");
        if (vaultChanged) { store.set("last", null); location.reload(); return true; }
        toast("設定を保存しました");
        renderRight();
        return true;
      } catch (e) { fail(e); return false; }
    };
    const m = modal({ title: "設定", body, buttons: [{ label: "キャンセル" }, { label: "保存", primary: true, onClick: async () => (await saveCfg()) ? undefined : false }] });
    m.body.style.paddingTop = "0";
    const showTab = (t) => { $$(".mtabs button", body).forEach((b) => b.classList.toggle("on", b.dataset.t === t)); $$("[data-tp]", body).forEach((p) => (p.hidden = p.dataset.tp !== t)); };
    $$(".mtabs button", body).forEach((b) => (b.onclick = () => showTab(b.dataset.t)));
    showTab(tab);
    const prov = $("#cProv", body); prov.value = cfg.provider;
    const syncProv = () => {
      const az = prov.value === "azure";
      $$(".az", body).forEach((x) => (x.hidden = !az));
      $("#modelLbl", body).textContent = az ? "デプロイ名" : "モデル";
      $("#urlHelp", body).textContent = az ? "例: https://<リソース名>.openai.azure.com" : "末尾は /v1 まで（/chat/completions は付けない）";
    };
    prov.onchange = syncProv; syncProv();
    $("#cTest", body).onclick = async () => {
      const out = $("#cTestRes", body); out.innerHTML = '<span class="spin"></span> テスト中…';
      if (!(await saveCfg())) { out.innerHTML = ""; return; }
      try {
        const r = await api.post("/api/config/test");
        const row = (name, x) => `<div>${name}: <span class="${x.ok ? "ok" : x.ok === false ? "ng" : ""}">${x.ok ? "OK" : x.ok === false ? "失敗" : "—"}</span> ${esc(x.message || "")}</div>`;
        out.innerHTML = row("チャット", r.chat) + row("埋め込み", r.embed);
      } catch (e) { out.innerHTML = `<span class="ng">${esc(e.message)}</span>`; }
    };
  }
  $("#btnSettings").onclick = () => openSettings();

  // ------------------------------------------------------------ テーマ・レイアウト
  function applyTheme(t) { if (t === "light") document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme; if (S.graph) S.graph.draw(); }
  function toggleTheme() { const t = document.documentElement.dataset.theme === "light" ? "dark" : "light"; store.set("theme", t); applyTheme(t); }
  $("#btnTheme").onclick = toggleTheme;
  applyTheme(store.get("theme", matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

  $("#btnRight").onclick = () => {
    if (innerWidth <= 900) app.classList.toggle("show-right");
    else { app.classList.toggle("no-right"); store.set("noRight", app.classList.contains("no-right")); }
    renderRight();
  };
  if (store.get("noRight", false)) app.classList.add("no-right");
  function resizer(side) {
    const r = document.createElement("div"); r.className = "resizer";
    const host = side === "left" ? $("#side") : $("#right");
    host.style.position = "relative";
    Object.assign(r.style, side === "left" ? { right: "-3px" } : { left: "-3px" });
    host.appendChild(r);
    r.onpointerdown = (e) => {
      r.setPointerCapture(e.pointerId);
      r.onpointermove = (ev) => {
        const w = side === "left" ? Math.max(160, Math.min(480, ev.clientX - 46)) : Math.max(240, Math.min(640, innerWidth - ev.clientX));
        app.style.setProperty(side === "left" ? "--side-w" : "--right-w", w + "px");
      };
      r.onpointerup = () => { r.onpointermove = null; store.set(side + "W", getComputedStyle(app).getPropertyValue(side === "left" ? "--side-w" : "--right-w")); if (S.graph) S.graph.resize(); };
    };
    const saved = store.get(side + "W", null); if (saved) app.style.setProperty(side === "left" ? "--side-w" : "--right-w", saved);
  }
  resizer("left"); resizer("right");

  // ------------------------------------------------------------ キーボード
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
    if ($(".overlay") && !(mod && k === "k")) return;
    if (mod && (k === "k" || k === "o" || k === "p") && !e.shiftKey) { e.preventDefault(); $$(".overlay").forEach((o) => o.remove()); openPalette(); }
    else if (mod && e.shiftKey && k === "p") { e.preventDefault(); openPalette(); setTimeout(() => { const i = $(".pal input"); i.value = ">"; i.dispatchEvent(new Event("input")); }, 0); }
    else if (mod && k === "s") { e.preventDefault(); save(); }
    else if (mod && k === "e") { e.preventDefault(); if (S.cur) setMode(S.mode === "edit" ? "prev" : "edit"); }
    else if (e.altKey && !mod && k === "n") { e.preventDefault(); newNote(); }
    else if (mod && k === "d") { e.preventDefault(); daily(); }
    else if (mod && e.shiftKey && k === "f") { e.preventDefault(); showPanel("search"); }
    else if (mod && e.shiftKey && k === "e") { e.preventDefault(); showPanel("files"); }
    else if (e.key === "F2" && S.cur) { e.preventDefault(); const t = $("#titleIn"); t.focus(); t.select(); }
    else if (e.altKey && e.key === "ArrowLeft" && S.histPos > 0) { e.preventDefault(); S.histPos--; openNote(S.hist[S.histPos], { push: false }); }
    else if (e.altKey && e.key === "ArrowRight" && S.histPos < S.hist.length - 1) { e.preventDefault(); S.histPos++; openNote(S.hist[S.histPos], { push: false }); }
  });

  // ------------------------------------------------------------ 起動
  (async function init() {
    try {
      S.state = await api.get("/api/state");
      await loadTree();
    } catch (e) { $("#body").innerHTML = `<div class="welcome"><h1>接続できません</h1><p>${esc(e.message)}</p></div>`; return; }
    renderMain(); renderRight();
    const hash = decodeURIComponent(location.hash.slice(1));
    const start = (hash && S.tree.some((n) => n.path === hash) && hash) || (store.get("last") && S.tree.some((n) => n.path === store.get("last")) && store.get("last")) || resolve("ホーム") || (S.tree[0] && S.tree[0].path);
    if (start) openNote(start);
  })();
})();
