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
  const isDoc = (p) => p.startsWith("@") || !/\.(md|markdown)$/i.test(p);
  const FT = { word: "W", excel: "X", powerpoint: "P", pdf: "PDF", email: "✉", csv: "CSV", html: "HTML", text: "TXT", code: "{ }", note: "MD" };
  const GRP_NAME = { word: "Word", excel: "Excel", powerpoint: "PowerPoint", pdf: "PDF", email: "メール", csv: "CSV", html: "HTML", text: "テキスト", code: "データ" };
  const ftBadge = (grp) => `<span class="ft ft-${esc(grp || "text")}">${esc(FT[grp] || "?")}</span>`;
  const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B");
  const fmtTime = (t) => { if (!t) return "—"; const d = new Date(t * 1000); return d.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" }) + " " + d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" }); };
  const folderOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const icon = (d) => `<svg viewBox="0 0 24 24">${d}</svg>`;

  // ------------------------------------------------------------ 状態
  const S = {
    state: {}, tree: [], folders: [], titleMap: new Map(), pathMap: new Map(), mtimes: new Map(),
    cur: null, mode: store.get("mode", "prev"), dirty: false, saving: null, saveTimer: 0, conflict: null,
    rtab: store.get("rtab", "links"), panel: "files", closed: new Set(store.get("closed", [])), selFolder: "",
    hist: [], histPos: -1, chat: [], depth: store.get("depth", 1), graph: null, tag: "",
    folderView: null, treeKind: store.get("treeKind", "all"),
    entityView: null, entityData: null, profile: null, peopleKind: store.get("peopleKind", "person"), gPeople: store.get("gPeople", false),
    sources: [], index: null, jobSeen: null, aiScope: store.get("aiScope", "all"), gDocs: store.get("gDocs", false),
  };

  // ------------------------------------------------------------ ツリー
  async function loadTree() {
    const t = await api.get("/api/tree");
    S.tree = t.notes; S.folders = t.folders; S.sources = t.sources || [];
    S.titleMap = new Map(); S.pathMap = new Map(); S.mtimes = new Map();
    const sorted = [...t.notes].sort((a, b) => (a.kind === "note" ? 0 : 1) - (b.kind === "note" ? 0 : 1) || a.path.length - b.path.length);
    for (const n of sorted) {
      const k = n.title.toLowerCase();
      if (!S.titleMap.has(k)) S.titleMap.set(k, n.path);
      if (n.kind === "note") S.pathMap.set(n.path.replace(/\.(md|markdown)$/i, "").toLowerCase(), n.path);
      else {
        S.pathMap.set(n.path.toLowerCase(), n.path);
        const stem = k.replace(/\.[^.]+$/, "");
        if (!S.titleMap.has(stem)) S.titleMap.set(stem, n.path);
      }
      S.mtimes.set(n.path, n.mtime_ns);
    }
    renderTree();
  }

  function resolve(target) {
    let k = (target || "").trim().toLowerCase();
    if (!k) return S.cur ? S.cur.path : null;
    if (S.pathMap.has(k)) return S.pathMap.get(k);
    if (k.endsWith(".md")) k = k.slice(0, -3);
    if (k.includes("/")) { if (S.pathMap.has(k)) return S.pathMap.get(k); k = k.split("/").pop(); }
    return S.titleMap.get(k) || null;
  }

  function renderTree() {
    const el = $("#tree");
    const q = $("#filter").value.trim().toLowerCase();
    const kind = S.treeKind;
    const want = (n) => kind === "all" || (kind === "note" ? n.kind === "note" : n.kind !== "note");
    $$("#treeKind button").forEach((b) => b.classList.toggle("on", b.dataset.k === kind));
    if (q) {
      const hits = S.tree.filter((n) => want(n) && n.path.toLowerCase().includes(q));
      el.innerHTML = hits.length ? hits.map((n) => fileBtn(n, 0, true)).join("") : '<div class="empty">該当するノート・資料はありません</div>';
      return;
    }
    const root = { folders: {}, notes: [] };
    const node = (path) => {
      let cur = root;
      if (!path) return cur;
      for (const part of path.split("/")) cur = cur.folders[part] ||= { folders: {}, notes: [], n: 0 };
      return cur;
    };
    S.folders.forEach((f) => node(f));
    S.sources.forEach((s) => { if (s.prefix) node(s.prefix); });
    S.tree.forEach((n) => { if (want(n)) node(n.folder).notes.push(n); });
    const count = (nd) => (nd.n = nd.notes.length + Object.values(nd.folders).reduce((a, f) => a + count(f), 0));
    count(root);
    const srcLabel = Object.fromEntries(S.sources.map((s) => [s.prefix, s.label]));
    const coll = (a, b) => a.localeCompare(b, "ja", { numeric: true });
    const walk = (nd, path, depth) => {
      let h = "";
      const names = Object.keys(nd.folders).sort((a, b) => (a.startsWith("@") ? 1 : 0) - (b.startsWith("@") ? 1 : 0) || coll(a, b));
      for (const name of names) {
        const fp = path ? path + "/" + name : name;
        const sub = nd.folders[name];
        if (kind !== "all" && !sub.n) continue;                       // 絞り込み中は空のフォルダを隠す
        const closed = S.closed.has(fp);
        const src = !path && name.startsWith("@");
        const ext = fp.startsWith("@");
        h += `<div class="folder${closed ? " closed" : ""}${src ? " src" : ""}${S.folderView === fp ? " on" : ""}" data-folder="${esc(fp)}" style="padding-left:${6 + depth * 14}px" draggable="${ext ? "false" : "true"}" title="${src ? "外部フォルダ（読み取り専用）" : esc(fp)}" role="button" tabindex="0"><span class="car">▾</span>${src ? icon('<path d="M3 6.5h7l2 2h9V19H3z"/><path d="M14 13h5M16.5 10.5l2.5 2.5-2.5 2.5"/>') : ""}<span class="fname">${esc(src ? srcLabel[name] || name : name)}</span><span class="fcount">${sub.n || ""}</span><button class="fmore" data-fmore="${esc(fp)}" title="フォルダの操作" tabindex="-1">⋯</button></div>`;
        if (!closed) h += walk(sub, fp, depth + 1);
      }
      nd.notes.sort((a, b) => (a.kind === "note" ? 0 : 1) - (b.kind === "note" ? 0 : 1) || coll(a.title, b.title)).forEach((n) => (h += fileBtn(n, depth)));
      return h;
    };
    el.innerHTML = walk(root, "", 0) || `<div class="empty">${kind === "doc" ? "資料がありません。フォルダにファイルをドロップすると追加できます。" : "ノートがありません。右上の＋で作成します。"}</div>`;
  }
  function fileBtn(n, depth, showFolder = false) {
    const on = S.cur && S.cur.path === n.path ? " on" : "";
    const doc = n.kind && n.kind !== "note";
    const err = n.status && n.status !== "ok";
    const movable = !n.path.startsWith("@");
    return `<button class="file${on}${doc ? " doc" : ""}${err ? " err" : ""}" data-open="${esc(n.path)}" draggable="${movable ? "true" : "false"}" title="${esc(n.path)}${err ? "（読み込みエラー）" : ""}" style="padding-left:${20 + depth * 14}px">${doc ? ftBadge(n.grp) : ""}${esc(n.title)}${showFolder && n.folder ? ` <small style="color:var(--muted)">${esc(docLabel(n.folder))}</small>` : ""}</button>`;
  }

  $("#tree").addEventListener("click", (e) => {
    const more = e.target.closest("[data-fmore]");
    if (more) { e.stopPropagation(); const r = more.getBoundingClientRect(); folderMenu(r.left, r.bottom + 2, more.dataset.fmore); return; }
    const f = e.target.closest("[data-folder]");
    if (f) {
      const fp = f.dataset.folder; S.selFolder = fp;
      if (!e.target.closest(".car") && !(S.folderView === fp && !S.closed.has(fp))) { openFolder(fp); return; }   // 名前 → 一覧を開く
      S.closed.has(fp) ? S.closed.delete(fp) : S.closed.add(fp);                                               // ▾ → 開閉だけ
      store.set("closed", [...S.closed]); renderTree();
    }
  });
  $("#tree").addEventListener("keydown", (e) => { const f = e.target.closest("[data-folder]"); if (f && e.key === "Enter") openFolder(f.dataset.folder); });
  $("#tree").addEventListener("contextmenu", (e) => {
    const b = e.target.closest("[data-open]");
    const f = e.target.closest("[data-folder]");
    if (b) { e.preventDefault(); noteMenu(e.clientX, e.clientY, b.dataset.open); }
    else if (f) { e.preventDefault(); folderMenu(e.clientX, e.clientY, f.dataset.folder); }
  });
  // ドラッグ: ノート・資料・フォルダをフォルダへ移動。パソコンのファイルをフォルダに落とすと資料として追加
  const dropTarget = (e) => e.target.closest("[data-folder]");
  const clearDrop = () => $$(".dropt").forEach((x) => x.classList.remove("dropt"));
  $("#tree").addEventListener("dragstart", (e) => {
    const b = e.target.closest("[data-open]"), f = e.target.closest("[data-folder]");
    if (b) e.dataTransfer.setData("text/mycel-path", b.dataset.open);
    else if (f) e.dataTransfer.setData("text/mycel-folder", f.dataset.folder);
  });
  $("#tree").addEventListener("dragover", (e) => {
    const types = [...e.dataTransfer.types];
    if (!types.some((t) => t === "text/mycel-path" || t === "text/mycel-folder" || t === "Files")) return;
    e.preventDefault(); e.stopPropagation();
    clearDrop(); (dropTarget(e) || $("#tree")).classList.add("dropt");
  });
  $("#tree").addEventListener("dragleave", (e) => { if (e.target.classList) e.target.classList.remove("dropt"); });
  $("#tree").addEventListener("drop", async (e) => {
    const types = [...e.dataTransfer.types];
    clearDrop();
    const f = dropTarget(e);
    const folder = f ? f.dataset.folder : "";
    if (types.includes("Files")) {
      e.preventDefault(); e.stopPropagation();                        // AI 取り込みではなく、このフォルダへ追加
      await addFiles(folder, [...e.dataTransfer.files]);
      return;
    }
    const path = e.dataTransfer.getData("text/mycel-path"), fold = e.dataTransfer.getData("text/mycel-folder");
    if (!path && !fold) return;
    e.preventDefault();
    if (folder.startsWith("@")) { toast("外部フォルダは読み取り専用です（Vault 内のフォルダに移動してください）", true); return; }
    if (path) { if (folderOf(path) !== folder) await moveTo(path, folder); }
    else if (fold && fold !== folder && folderOf(fold) !== folder) await renameFolder(fold, (folder ? folder + "/" : "") + fold.split("/").pop());
  });
  $("#filter").addEventListener("input", renderTree);
  $("#treeKind").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) { S.treeKind = b.dataset.k; store.set("treeKind", S.treeKind); renderTree(); } });
  $("#btnCollapse").onclick = () => { S.folders.forEach((f) => S.closed.add(f)); S.sources.forEach((s) => s.prefix && S.closed.add(s.prefix)); store.set("closed", [...S.closed]); renderTree(); };

  // ------------------------------------------------------------ フォルダ・資料の整理
  async function addFiles(folder, files) {
    if (folder.startsWith("@")) { toast("外部フォルダには追加できません（読み取り専用）", true); return; }
    let ok = 0, last = null;
    for (const f of files) {
      try {
        const res = await fetch("/api/file/upload", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(f.name), "X-Folder": encodeURIComponent(folder) }, body: f });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `エラー (${res.status})`);
        if (data.status === "error") toast(`${f.name}: 追加しましたが読み込めませんでした（${data.error}）`, true);
        ok++; last = data.path;
      } catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    if (ok) {
      S.closed.delete(folder); store.set("closed", [...S.closed]);
      await loadTree(); refreshFolder();
      toast(`${ok} 件を「${folder ? docLabel(folder) : "Vault 直下"}」に追加しました`);
      if (ok === 1 && S.folderView === null && last) openNote(last);
    }
  }
  function pickFiles(folder) {
    const inp = document.createElement("input");
    inp.type = "file"; inp.multiple = true; inp.accept = IG_ACCEPT + ",.md";
    inp.onchange = () => addFiles(folder, [...inp.files]);
    inp.click();
  }
  /** ノート・資料をフォルダへ移動（資料は拡張子を保つ）。 */
  async function moveTo(path, folder) {
    const name = path.split("/").pop();
    if (!isDoc(path)) return renameNote(path, (folder ? folder + "/" : "") + titleOf(path));
    return moveItem(path, (folder ? folder + "/" : "") + name);
  }
  async function moveItem(path, newPath) {
    try {
      const r = await api.post("/api/item/move", { path, new_path: newPath });
      await loadTree();
      if (S.cur && S.cur.path === path) await openNote(r.path, { push: false });
      else if (S.cur && r.updated.includes(S.cur.path)) await openNote(S.cur.path, { push: false });
      refreshFolder();
      toast(r.updated.length ? `移動し、${r.updated.length} 件のノートのリンクを更新しました` : "移動しました");
      return r.path;
    } catch (e) { fail(e); return null; }
  }
  async function deleteItem(path) {
    if (!isDoc(path)) return deleteNote(path);
    const ok = await confirmBox("資料を削除", `「${esc(path.split("/").pop())}」を削除しますか？<br><span class="hint">Vault 内の .mycel/trash に移動します。つながりも外れます。</span>`, "削除する", true);
    if (!ok) return;
    try {
      await api.post("/api/item/delete", { path });
      await loadTree();
      if (S.cur && S.cur.path === path) { S.cur = null; renderMain(); renderRight(); }
      refreshFolder();
      toast("削除しました");
    } catch (e) { fail(e); }
  }
  async function newFolder(parent = "") {
    const name = await promptBox("新しいフォルダ", "フォルダ名（/ で階層）", parent && !parent.startsWith("@") ? parent + "/" : "");
    if (!name || !name.trim()) return;
    try {
      const r = await api.post("/api/folder/create", { path: name.trim() });
      await loadTree(); S.closed.delete(folderOf(r.path)); refreshFolder();
      toast(`フォルダ「${r.path}」を作りました`);
    } catch (e) { fail(e); }
  }
  async function renameFolder(path, newPath = null) {
    if (newPath === null) {
      newPath = await promptBox("フォルダの名前を変更・移動", "新しい場所（/ で階層）", path);
      if (!newPath || newPath.trim() === path) return;
    }
    try {
      const r = await api.post("/api/folder/rename", { path, new_path: newPath.trim() });
      if (S.closed.delete(path)) S.closed.add(r.path);
      await loadTree();
      if (S.folderView !== null && (S.folderView === path || S.folderView.startsWith(path + "/"))) S.folderView = r.path + S.folderView.slice(path.length);
      if (S.cur && (S.cur.path.startsWith(path + "/") || r.updated.includes(S.cur.path))) await openNote(S.cur.path.startsWith(path + "/") ? r.path + S.cur.path.slice(path.length) : S.cur.path, { push: false });
      refreshFolder();
      toast(`${r.moved} 件を移動しました${r.updated.length ? `（${r.updated.length} 件のノートのリンクを更新）` : ""}`);
    } catch (e) { fail(e); }
  }
  async function deleteFolder(path) {
    const n = S.tree.filter((x) => x.path.startsWith(path + "/")).length;
    const ok = await confirmBox("フォルダを削除", `「${esc(path)}」${n ? `と中の ${n} 件` : ""}を削除しますか？<br><span class="hint">フォルダごと Vault 内の .mycel/trash に移動します。</span>`, "削除する", true);
    if (!ok) return;
    try {
      await api.post("/api/folder/delete", { path });
      await loadTree();
      if (S.cur && S.cur.path.startsWith(path + "/")) { S.cur = null; renderMain(); renderRight(); }
      if (S.folderView !== null && (S.folderView === path || S.folderView.startsWith(path + "/"))) openFolder(folderOf(path));
      toast("削除しました");
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ ノートを開く
  async function openNote(path, { heading = "", push = true, mode = null } = {}) {
    await flushSave();
    let data;
    try { data = await api.get("/api/note", { path }); } catch (e) { fail(e); return; }
    S.cur = data; S.dirty = false; S.conflict = null; S.folderView = null; S.entityView = null;
    if (mode && !data.readonly) setMode(mode, false);
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
    if (S.entityView) { renderEntityView(); $("#conflict").hidden = true; return; }
    if (S.folderView !== null) { renderFolderView(); $("#conflict").hidden = true; return; }
    const c = S.cur;
    $("#titleIn").disabled = !c || (c.readonly && !c.editable);
    $("#mEdit").disabled = !!(c && c.readonly);
    $("#titleIn").value = c ? c.title : "";
    $("#crumb").textContent = c && c.folder ? docLabel(c.folder) + " /" : "";
    document.title = c ? `${c.title} — Mycel` : "Mycel";
    $("#mPrev").classList.toggle("on", S.mode === "prev");
    $("#mEdit").classList.toggle("on", S.mode === "edit");
    $("#conflict").hidden = !S.conflict;
    if (!c) {
      $("#body").innerHTML = `<div class="welcome"><h1>Mycel</h1><p>左のファイルからノートを開くか、<b>Ctrl+K</b> でノートを探して開いてください。見つからない名前を入力すると新しく作れます。</p></div>`;
      updateStats(); return;
    }
    if (c.readonly) renderDoc();
    else if (S.mode === "edit") renderEditor(); else renderPreview();
    updateStats();
  }

  const docLabel = (p) => { const m = p.match(/^@([^/]+)\/?(.*)$/); if (!m) return p; const s = S.sources.find((x) => x.id === m[1]); return (s ? s.label : "@" + m[1]) + (m[2] ? " / " + m[2] : ""); };
  /** 資料（Word・PDF など）の表示。読み取り専用。 */
  function renderDoc() {
    const c = S.cur;
    const plain = c.grp === "code" || c.grp === "text";
    const status = c.status === "ok" ? "" : c.status === "pending"
      ? `<div class="notice">この資料はまだ読み込まれていません。<button class="btn sm" data-docact="update">今すぐ読み込む</button></div>`
      : `<div class="notice err">読み込めませんでした: ${esc(c.error || c.status)}</div>`;
    const stale = c.stale && c.status !== "pending"
      ? `<div class="notice">元のファイルが変更されています。表示・検索・AI は前回読み込んだ内容です。<button class="btn sm" data-docact="update">この資料を更新</button></div>` : "";
    const gone = c.exists === false ? `<div class="notice err">元のファイルが見つかりません（「更新」で一覧から消えます）。</div>` : "";
    $("#body").innerHTML = `<div class="docbar">${ftBadge(c.grp)}<span class="dpath" title="${esc(c.source_path || "")}">${esc(docLabel(c.path))}</span><span class="dmeta">読込 ${esc(fmtTime(c.indexed_at))}</span><span class="sp"></span>
        <button class="btn sm" data-docact="open"${c.exists === false ? " disabled" : ""}>アプリで開く</button>
        <a class="btn sm" href="/api/file?path=${encodeURIComponent(c.path)}" download>ダウンロード</a>
        <button class="btn sm pri" data-docact="ai"${c.status === "ok" ? "" : " disabled"} title="ローカル LLM が要約・名前・タグ・関連ノートを付けたノートの下書きを作ります">AI でノート化</button>
        <button class="btn sm" data-docact="import"${c.status === "ok" ? "" : " disabled"} title="本文をそのまま Markdown ノートとして Vault に保存します">本文をノートに</button>
        <button class="btn sm" data-docact="update" title="この資料だけ読み込み直します">再読込</button></div>
      ${gone}${stale}${status}
      <article class="preview md docview">${plain ? `<pre class="doctext">${esc(c.text)}</pre>` : MD.render(c.text, { resolve })}</article>`;
    $$("[data-docact]", $("#body")).forEach((b) => (b.onclick = () => docAction(b.dataset.docact)));
  }
  async function docAction(act) {
    const c = S.cur; if (!c) return;
    try {
      if (act === "open") { await api.post("/api/file/open", { path: c.path }); toast("既定のアプリで開きました"); }
      else if (act === "update") { await updateIndex([c.path], `「${c.title}」を読み込み直しています`); }
      else if (act === "ai") { await openIngest({ paths: [c.path], autoRun: true }); }
      else if (act === "import") {
        const r = await api.post("/api/note/import", { path: c.path });
        await loadTree(); await openNote(r.path); toast(`ノート「${titleOf(r.path)}」として取り込みました`);
      }
    } catch (e) { fail(e); }
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
    if (mode === "edit" && S.cur && S.cur.readonly) { toast("資料は読み取り専用です（「ノートとして取り込む」で編集できます）"); return; }
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
    if (S.cur.readonly) { $("#stInfo").textContent = `資料 ・ バックリンク ${(S.cur.backlinks || []).length}`; $("#stWords").textContent = `${text.replace(/\s/g, "").length.toLocaleString()} 文字`; return; }
    const links = (text.match(/\[\[[^\[\]\n]+?\]\]/g) || []).length;
    $("#stInfo").textContent = `リンク ${links} ・ バックリンク ${(S.cur.backlinks || []).length}`;
    $("#stWords").textContent = `${text.replace(/\s/g, "").length.toLocaleString()} 文字`;
  }

  /** 現在のノートの本文を関数で書き換えて保存する（AI の提案の反映など）。 */
  function mutateCurrent(fn) {
    if (!S.cur || S.cur.readonly) return;
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

  // 読み込み状況と、開いているファイルの外部変更を見張る（重い処理はしない）
  async function poll() {
    let running = false;
    if (!document.hidden) {
      try {
        const st = await api.get("/api/index/status");
        running = !!(st.job && st.job.state === "running");
        await applyIndexStatus(st);
        if (ingestUI && running && st.job.kind === "ingest") ingestUI.refresh();
        if (S.cur && !S.dirty && !S.saving && !S.conflict) {
          const path = S.cur.path;
          const v = await api.get("/api/note/version", { path });
          if (S.cur && S.cur.path === path) {
            if (!v.exists) { if (!S.cur.gone) { S.cur.gone = true; toast(`「${S.cur.title}」は別の場所で削除または移動されました`, true); } }
            else if (v.version !== S.cur.version) {
              if (S.cur.readonly) { if (!S.cur.stale) { S.cur.stale = true; renderMain(); } }
              else { const d = await api.get("/api/note", { path }); if (!S.dirty && d.version !== S.cur.version) { S.cur = d; renderMain(); renderRight(); toast("外部の変更を読み込みました"); } }
            }
          }
        }
      } catch { /* サーバ停止中など */ }
    }
    setTimeout(poll, running ? 700 : 4000);
  }

  // ------------------------------------------------------------ 読み込み（インデックス）の状態と更新
  const JOB_DONE = { update: "読み込み", scan: "確認", ingest: "AI 取り込み", people: "人物・組織の AI 抽出" };
  async function applyIndexStatus(st) {
    S.index = st;
    renderIndexChip();
    const job = st.job;
    if (!job || job.state === "running") return;
    const key = job.kind + ":" + job.started;
    if (S.jobSeen === key) return;
    const first = S.jobSeen === null;
    S.jobSeen = key;
    if (first && Date.now() / 1000 - (job.finished || 0) > 10) return;    // 起動前に終わった処理は通知しない
    const r = job.result || {};
    if (job.state === "done" && job.kind === "update") {
      const n = (r.added || 0) + (r.modified || 0);
      toast(`${job.label}: 新規 ${r.added || 0} ・ 変更 ${r.modified || 0} ・ 削除 ${r.deleted || 0}${r.errors ? ` ・ エラー ${r.errors}` : ""}${r.embedded ? ` ・ 意味検索 ${r.embedded} 区画` : ""}${r.embed_error ? "（意味検索は失敗: " + r.embed_error + "）" : ""}`, !!r.embed_error);
      await loadTree();
      if (!S.cur) { const start = resolve("ホーム") || (S.tree.find((x) => x.kind === "note") || {}).path; if (start) openNote(start); }
      else if (!S.dirty && (n || r.deleted)) { const cur = S.cur.path; if (S.tree.some((x) => x.path === cur) || !isDoc(cur)) openNote(cur, { push: false }).catch(() => {}); }
      if (S.rtab !== "links" || !S.cur) renderRight();
    } else if (job.state === "done" && job.kind === "scan") {
      const pend = (r.added || 0) + (r.modified || 0) + (r.deleted || 0);
      if (!first && pend) toast(`未反映の変更が ${pend} 件あります（「更新」で読み込みます）`);
    } else if (job.state === "done" && job.kind === "ingest") {
      toast(`AI 取り込み: 下書き ${r.drafted || 0} 件${r.errors ? ` ・ エラー ${r.errors} 件` : ""}。確認して保存してください`, !!r.errors);
      if (!ingestUI) openIngest();
    } else if (job.state === "cancelled") toast(job.message);
    else if (job.state === "error") toast(`${JOB_DONE[job.kind] || "処理"}に失敗しました: ${job.message}`, true);
    if (scopeUI) scopeUI.refresh();
    if (ingestUI) ingestUI.refresh();
    if (job.kind === "update") refreshFolder();
    if ((job.kind === "update" || job.kind === "people") && S.panel === "people") loadPeople();
    if (job.kind === "people" && job.state === "done") { toast(`人物・組織の AI 抽出: ${r.extracted || 0} 件${r.errors ? `（失敗 ${r.errors} 件）` : ""}`); refreshEntity(); }
  }

  function renderIndexChip() {
    const el = $("#stIndex"), st = S.index; if (!el || !st) return;
    const job = st.job;
    if (job && job.state === "running") {
      const pct = job.total ? ` ${job.done}/${job.total}` : job.done ? ` ${job.done} 件` : "";
      el.innerHTML = `<span class="spin"></span><span class="ixt" title="${esc(job.current || "")}">${esc(job.label)} ・ ${esc(job.phase)}${pct}${job.current ? " ・ " + esc(job.current.split("/").pop()) : ""}</span><button class="btn xs" id="ixCancel">中断</button>`;
      $("#ixCancel").onclick = async () => { try { await api.post("/api/index/cancel"); } catch (e) { fail(e); } };
      return;
    }
    const p = st.pending, pend = p.added + p.modified + p.deleted;
    el.innerHTML = `<button class="ixbtn" id="ixScope" title="読み込み範囲を開く ・ 最終更新 ${esc(fmtTime(st.last_update))}">${icon('<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>')}ノート ${st.notes} ・ 資料 ${st.docs}${st.errors ? ` ・ <span class="ng">エラー ${st.errors}</span>` : ""}${pend ? ` ・ <b class="pend">未反映 ${pend}</b>` : ""}</button><button class="btn xs${pend ? " pri" : ""}" id="ixUpdate" title="変更されたファイルだけを読み込みます（範囲は「読み込み範囲」で指定）">更新</button>`;
    $("#ixScope").onclick = () => openScope();
    $("#ixUpdate").onclick = () => updateIndex(null);
  }

  async function updateIndex(prefixes, msg = "") {
    try {
      const st = await api.post("/api/index/update", { prefixes });
      toast(msg || `${st.label} を開始しました`);
      S.index = { ...(S.index || {}), job: st }; renderIndexChip();
      setTimeout(poll, 300);
    } catch (e) { fail(e); }
  }
  async function checkIndex(prefixes) {
    try { await api.post("/api/index/check", { prefixes }); setTimeout(poll, 300); } catch (e) { if (!(e.data && e.data.busy)) fail(e); }
  }

  function folderMenu(x, y, folder) {
    const src = S.sources.find((s) => s.prefix && (folder === s.prefix));
    const ext = folder.startsWith("@");
    popMenu(x, y, [
      { label: "一覧で開く", run: () => openFolder(folder) },
      ...(ext ? [] : [
        { label: "資料を追加（ファイルを選ぶ）…", run: () => pickFiles(folder) },
        { label: "ここに新しいノート", run: () => newNote(folder) },
        { label: "ここに新しいフォルダ…", run: () => newFolder(folder) },
      ]),
      { label: "資料のつながりを AI で提案…", run: () => proposeRelations(folder) },
      "-",
      { label: "このフォルダを更新", run: () => updateIndex([folder]) },
      { label: "変更を確認", run: () => checkIndex([folder]) },
      ...(src ? [] : [{ label: "読み込み範囲から外す", run: () => excludeFolder(folder) }]),
      ...(ext ? [] : ["-",
        { label: "名前を変更・移動…", run: () => renameFolder(folder) },
        { label: "フォルダを削除", danger: true, run: () => deleteFolder(folder) },
      ]),
    ]);
  }
  async function excludeFolder(folder) {
    const ok = await confirmBox("読み込み範囲から外す", `「${esc(folder)}」を読み込み範囲から外しますか？<br><span class="hint">ファイルは消えません。一覧・検索・AI の対象から外れます（すぐ反映します）。</span>`, "外す");
    if (!ok) return;
    try {
      const sc = await api.get("/api/scope");
      await api.post("/api/scope", { exclude: [...sc.exclude, folder] });
      await updateIndex([folder], `「${folder}」を範囲から外しました`);
    } catch (e) { fail(e); }
  }


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
    if (S.cur.readonly) { await moveItem(S.cur.path, (S.cur.folder ? S.cur.folder + "/" : "") + v); return; }
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
  $("#btnFolderView").onclick = () => openFolder(S.selFolder || "");
  $("#btnNewFolder").onclick = () => newFolder(S.folderView !== null ? S.folderView : S.selFolder);
  async function daily() {
    try { const r = await api.post("/api/daily"); if (r.created) await loadTree(); await openNote(r.path); } catch (e) { fail(e); }
  }
  $("#btnDaily").onclick = daily;

  async function moveNote(path) {
    const opts = ["", ...S.folders].map((f) => `<option value="${esc(f)}"${f === folderOf(path) ? " selected" : ""}>${f ? esc(f) : "（Vault 直下）"}</option>`).join("");
    const m = modal({
      title: "フォルダへ移動",
      body: `<div class="form"><label for="mvSel">移動先</label><select id="mvSel">${opts}</select><label for="mvNew">新しいフォルダ</label><input id="mvNew" type="text" placeholder="（入力すると優先）"></div>`,
      buttons: [{ label: "キャンセル" }, { label: "移動", primary: true, onClick: () => { const f = ($("#mvNew").value.trim() || $("#mvSel").value).replace(/^\/+|\/+$/g, ""); moveTo(path, f); } }],
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
    if (isDoc(path)) {
      popMenu(x, y, [
        { label: "開く", run: () => openNote(path) },
        { label: "アプリで開く", run: () => api.post("/api/file/open", { path }).catch(fail) },
        { label: "この資料を再読込", run: () => updateIndex([path]) },
        { label: "AI でノート化…", run: () => openIngest({ paths: [path], autoRun: true }) },
        { label: "本文をノートに取り込む", run: async () => { try { const r = await api.post("/api/note/import", { path }); await loadTree(); openNote(r.path); } catch (e) { fail(e); } } },
        { label: "リンク用の名前をコピー", run: () => copyText(`[[${path}]]`) },
        ...(path.startsWith("@") ? [] : ["-",
          { label: "名前を変更…", run: async () => { const cur = path.split("/").pop(); const v = await promptBox("資料の名前を変更", "新しい名前（拡張子は変わりません）", cur.replace(/\.[^.]+$/, "")); if (v && v.trim()) moveItem(path, (folderOf(path) ? folderOf(path) + "/" : "") + v.trim()); } },
          { label: "フォルダへ移動…", run: () => moveNote(path) },
          { label: "削除", danger: true, run: () => deleteItem(path) },
        ]),
      ]);
      return;
    }
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
    if (S.cur.readonly) { noteMenu(r.right - 230, r.bottom + 4, S.cur.path); return; }
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
    if (!S.cur && S.entityView) { p.innerHTML = '<div class="hint" style="padding:8px 2px">人物・組織のページです。<br><br>・登場する文書の × → その文書から外す（抽出し直しても外したまま）<br>・⋯ → 同一人物としてまとめる／人名ではない<br>・「別の呼び方」の × → まとめを外す<br>・「AI で人物像を推定」→ 登場する文書からローカル LLM が所属・案件・関係者を推定</div>'; return; }
    if (!S.cur && S.folderView !== null) { p.innerHTML = '<div class="hint" style="padding:8px 2px">フォルダの一覧を表示しています。<br><br>・ファイルをドロップ → このフォルダに資料を追加<br>・行を選ぶ → まとめて移動・AI でノート化・削除<br>・2 件選ぶ → つなぐ<br>・「つながりを提案」→ 内容の近い資料の組をまとめてつなぐ<br>・右クリック → 名前の変更など</div>'; return; }
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
    let h = c.readonly ? relationsHtml(c) : "";
    h += `<div class="rh"><span>バックリンク</span><span>${bl.length}</span></div>`;
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
    if (!c.readonly) h += relationsHtml(c);
    h += `<div class="rh"><span>登場する人物・組織</span></div><div id="docPeople" class="srcs"><span class="spin"></span></div>`;
    p.innerHTML = h;
    bindRelations(p);
    loadDocPeople(p, c.path);
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
    p.innerHTML = `<div class="gctl"><span>範囲</span><div class="seg">${[1, 2, 0].map((d) => `<button data-d="${d}" class="${S.depth === d ? "on" : ""}">${d ? d + " ホップ" : "全体"}</button>`).join("")}</div><label title="リンクされていない資料も表示"><input type="checkbox" id="gDocs"${S.gDocs ? " checked" : ""}> 資料</label><label title="人物・組織を介したつながりも表示"><input type="checkbox" id="gPeople"${S.gPeople ? " checked" : ""}> 人物</label><span style="flex:1"></span><button class="btn sm" id="gBig">拡大</button></div><canvas class="graph" id="gSmall" aria-label="ノートのつながり。ノードをクリックで開く"></canvas><div class="hint" style="margin-top:8px">ドラッグで移動、ホイールで拡大縮小。薄い輪は未作成のリンク、四角は資料、◆ は人物、▲ は組織（「人物」をオン）。色はフォルダごと。</div>`;
    $("#gDocs").onchange = (e) => { S.gDocs = e.target.checked; store.set("gDocs", S.gDocs); renderGraphPane(p); };
    $("#gPeople").onchange = (e) => { S.gPeople = e.target.checked; store.set("gPeople", S.gPeople); renderGraphPane(p); };
    $$("[data-d]", p).forEach((b) => (b.onclick = () => { S.depth = +b.dataset.d; store.set("depth", S.depth); renderGraphPane(p); }));
    $("#gBig").onclick = () => bigGraph(S.cur ? S.cur.path : null);
    if (S.graph) S.graph.destroy();
    S.graph = new ForceGraph($("#gSmall"), { onOpen: graphOpen });
    try {
      const q = S.depth ? { path: S.cur.path, depth: S.depth } : {};
      if (S.gDocs) q.docs = "1";
      if (S.gPeople) q.people = "1";
      const data = await api.get("/api/graph", q);
      if (S.graph) S.graph.setData(data, S.cur.path);
    } catch (e) { fail(e); }
  }
  function graphOpen(path, title) {
    const m = path && path.match(/^~([po]):(.*)$/);
    if (m) return openEntity(m[1] === "p" ? "person" : "org", m[2]);
    if (path) openNote(path); else openTarget(title);
  }
  async function bigGraph(center) {
    const m = modal({ title: "グラフ（Vault 全体）", body: '<canvas class="graph big" id="gBigC" aria-label="Vault 全体のグラフ"></canvas>', buttons: [], wide: true, onClose: () => g.destroy() });
    m.body.style.padding = "0"; m.body.style.overflow = "hidden";
    const g = new ForceGraph($("#gBigC"), { onOpen: (p, t) => { m.close(); graphOpen(p, t); } });
    const q = {}; if (S.gDocs) q.docs = "1"; if (S.gPeople) q.people = "1";
    try { g.setData(await api.get("/api/graph", q), center); } catch (e) { fail(e); }
  }
  $("#btnGraph").onclick = () => bigGraph(S.cur ? S.cur.path : null);

  // ---- AI
  async function renderAI(p) {
    let st = S.state.llm || {};
    const scopes = [["all", "すべて（読み込み済みの全体）"], ["vault", "Vault"], ...S.sources.filter((s) => s.prefix).map((s) => ["src:" + s.prefix, "外部: " + s.label])];
    if (S.cur.folder) scopes.push(["folder", `このフォルダ（${S.cur.folder.split("/").pop()}）`]);
    if (!scopes.some(([k]) => k === S.aiScope)) S.aiScope = "all";
    p.innerHTML = `<div class="rh"><span>ノート・資料に質問</span><span id="aiMode"></span></div>
      ${st.chat ? "" : `<div class="notice">LLM が未設定です。関連ノートの検索だけ動きます。<br><button class="btn sm" style="margin-top:6px" id="aiSetup">LLM を設定する</button></div>`}
      <div class="chat" id="chat"></div>
      <div class="askbox"><textarea id="askIn" placeholder="例: A社の決裁者が気にしていることは？（Ctrl+Enter で送信）"></textarea>
        <div class="row"><label title="検索する範囲。読み込み済みの内容から探します（その場で読み直しはしません）">対象 <select id="askScope">${scopes.map(([k, l]) => `<option value="${esc(k)}"${k === S.aiScope ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label></div>
        <div class="row"><label><input type="checkbox" id="askCur" checked> 開いているファイルを含める</label><span><button class="btn sm" id="chatClear" title="会話を消す">クリア</button> <button class="btn sm pri" id="askGo">質問</button></span></div></div>
      <div class="rh"><span>このノート</span></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn sm" id="aiSum"${st.chat ? "" : " disabled"}>要約とタグ提案</button><button class="btn sm" id="aiSug">リンク候補を探す</button></div>
      <div id="aiOut" style="margin-top:8px"></div>
      <div class="rh"><span>検索インデックス</span></div><div id="aiIdx" class="hint">確認中…</div>`;
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
        const sc = $("#askScope").value;
        const prefixes = sc === "all" ? null : sc === "vault" ? [""] : sc === "folder" ? [S.cur.folder] : [sc.slice(4)];
        const r = await api.post("/api/ai/ask", { question: q, path: $("#askCur").checked ? S.cur.path : "", history, prefixes });
        Object.assign(a, { pending: false, content: r.answer, sources: r.sources, message: r.message });
      } catch (e) { Object.assign(a, { pending: false, content: "", message: e.message }); }
      drawChat();
      p.scrollTop = chat.offsetTop + chat.scrollHeight;
    };
    $("#askGo").onclick = ask;
    $("#askScope").onchange = (e) => { S.aiScope = e.target.value; store.set("aiScope", S.aiScope); };
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
      const emb = s.embed ? `意味検索: ${s.embedded} / ${s.chunks} 区画。` : "意味検索: 未設定（設定の「LLM」で Embed モデルを登録すると併用します）。";
      idx.innerHTML = `キーワード索引: ${s.chunks} 区画。${emb}<br>索引は「更新」を押したときだけ作ります（質問のたびに読み直しません）。 <button class="btn sm" id="embBuild">範囲と更新…</button>`;
      $("#embBuild").onclick = () => openScope();
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
    if (name === "people") { loadPeople(); setTimeout(() => $("#peopleQ").focus(), 0); }
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
      el.innerHTML = results.length ? `<div class="empty">${results.length} 件</div>` + results.map((r) => `<button class="res" data-open="${esc(r.path)}"><b>${r.kind === "doc" ? ftBadge((S.tree.find((n) => n.path === r.path) || {}).grp) : ""}${esc(r.title)}</b><span>${markTitle(r.snippet, first)}</span><small>${esc(folderOf(r.path))}</small></button>`).join("") : '<div class="empty">見つかりませんでした。</div>';
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
      { label: "AI 取り込み（文書をノートにする）…", run: () => openIngest() },
      { label: "新しいフォルダ…", run: () => newFolder(S.selFolder) },
      { label: "人物・組織の一覧", run: () => showPanel("people") },
      { label: "人物・組織を AI で詳しく抽出", run: async () => { try { await api.post("/api/people/extract", {}); setTimeout(poll, 300); } catch (e) { fail(e); } } },
      { label: "フォルダの一覧を開く（Vault）", run: () => openFolder("") },
      { label: "資料のつながりを提案（Vault 全体）…", run: () => proposeRelations("") },
      { label: "読み込み範囲…", run: () => openScope() },
      { label: "更新（変更されたファイルを読み込む）", run: () => updateIndex(null) },
      { label: "変更を確認（読み込みはしない）", run: () => checkIndex(null) },
      { label: "インデックスを作り直す（全ファイルを読み直す）", run: async () => { if (await confirmBox("作り直し", "すべてのファイルを読み込み直します。資料が多いと時間がかかります。", "作り直す")) { try { await api.post("/api/index/rebuild"); setTimeout(poll, 300); } catch (e) { fail(e); } } } },
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
        items = S.tree.map((n) => ({ label: n.title, hint: (n.kind === "note" ? "" : "資料 ・ ") + n.folder, path: n.path, s: Math.max(score(n.title, q), score(n.path, q) * 0.5) }))
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

  // ------------------------------------------------------------ 読み込み範囲（インタラクティブに決める）
  let scopeUI = null;
  const FSTATE = {
    indexed: ["読込済み", ""], new: ["未読込", "new"], modified: ["変更あり", "mod"], error: ["エラー", "ng"],
    excluded: ["除外", "off"], type_off: ["形式オフ", "off"], too_big: ["大きすぎ", "off"], indexed_out: ["範囲外（更新で削除）", "mod"],
  };
  function statBadges(st) {
    if (!st) return "";
    const pend = [st.added ? `+${st.added}` : "", st.modified ? `~${st.modified}` : "", st.deleted ? `−${st.deleted}` : ""].filter(Boolean).join(" ");
    return `<span class="sb">読込 ${st.indexed}</span>${pend ? `<span class="sb mod" title="未反映（+新規 ~変更 −削除）">${pend}</span>` : ""}${st.errors ? `<span class="sb ng">エラー ${st.errors}</span>` : ""}`;
  }

  async function openScope() {
    if (scopeUI) return;
    let info;
    try { info = await api.get("/api/scope"); } catch (e) { fail(e); return; }
    const open = new Set(store.get("scopeOpen", [""]));
    const sel = new Set();
    const body = document.createElement("div");
    body.className = "scope";
    body.innerHTML = `<div class="scope-top"><div id="scSum" class="scsum"></div><span class="sp"></span>
        <button class="btn sm" id="scCheck" title="日時とサイズだけを見て、未反映の変更を数えます（本文は読みません）">変更を確認</button>
        <button class="btn sm pri" id="scUpdSel" disabled>選択した範囲を更新</button>
        <button class="btn sm" id="scUpdAll" title="範囲内で変更されたファイルだけを読み込みます">すべて更新</button></div>
      <div class="scope-grid">
        <div><div class="hint" style="margin:0 0 6px">チェックを外したフォルダは読み込みません。行をクリックで選択（Ctrl で複数）し「選択した範囲を更新」で、その部分だけを読み込みます。</div>
          <div class="scope-tree" id="scTree" role="tree"></div></div>
        <div class="scope-side">
          <h4>読み込む形式</h4><div id="scTypes"></div>
          <h4>1 ファイルの上限</h4><div><input type="number" id="scMax" min="1" max="2048" style="width:80px"> MB</div>
          <h4>外部フォルダ（読み取り専用）</h4><div id="scSrc"></div>
          <button class="btn sm" id="scAdd" style="margin-top:6px">＋ フォルダを追加</button>
          <p class="hint">共有フォルダなどを追加すると、Vault の外の資料も検索・AI の対象にできます。ファイルは変更しません。</p>
        </div>
      </div>`;
    const m = modal({ title: "読み込み範囲", body, buttons: [{ label: "閉じる" }], wide: true, onClose: () => { scopeUI = null; } });
    m.el.querySelector(".modal").classList.add("xwide");

    const drawSide = () => {
      $("#scTypes", body).innerHTML = info.type_groups.map((g) => {
        const na = g.id === "pdf" && !info.pdf_available;
        return `<label class="tg" title="${esc(g.exts.join(" "))}"><input type="checkbox" data-type="${esc(g.id)}"${info.types.includes(g.id) ? " checked" : ""}> ${ftBadge(g.id)} ${esc(g.label)}${na ? ' <small class="ng">要 pip install pypdf</small>' : ""}</label>`;
      }).join("");
      $("#scMax", body).value = info.max_mb;
      const ext = info.sources.filter((s) => s.id !== "vault");
      $("#scSrc", body).innerHTML = ext.length ? ext.map((s) => `<div class="srcrow"><div><b>${esc(s.label)}</b><small>${esc(s.path)}${s.exists ? "" : ' <span class="ng">見つかりません</span>'}</small></div><button class="ibtn" data-rmsrc="${esc(s.id)}" title="範囲から外す">${icon('<path d="M6 6l12 12M18 6L6 18"/>')}</button></div>`).join("") : '<div class="hint">まだありません。</div>';
      $$("[data-type]", body).forEach((c) => (c.onchange = () => saveScope({ types: $$("[data-type]", body).filter((x) => x.checked).map((x) => x.dataset.type) }, null)));
      $$("[data-rmsrc]", body).forEach((b) => (b.onclick = () => removeSource(b.dataset.rmsrc)));
    };
    const drawSum = () => {
      const st = S.index || info.status;
      const p = st.pending, pend = p.added + p.modified + p.deleted;
      const job = st.job && st.job.state === "running" ? st.job : null;
      $("#scSum", body).innerHTML = job ? `<span class="spin"></span> ${esc(job.label)} ・ ${esc(job.phase)} ${job.total ? job.done + "/" + job.total : ""}`
        : `ノート ${st.notes} ・ 資料 ${st.docs}${st.errors ? ` ・ <span class="ng">エラー ${st.errors}</span>` : ""} ・ ${pend ? `<b class="pend">未反映 ${pend}</b>（+${p.added} ~${p.modified} −${p.deleted}）` : "未反映なし"} ・ 最終更新 ${esc(fmtTime(st.last_update))}${st.last_scan ? ` ・ 確認 ${esc(fmtTime(st.last_scan))}` : ""}`;
      $("#scCheck", body).disabled = $("#scUpdAll", body).disabled = !!job;
      const b = $("#scUpdSel", body);
      b.disabled = !!job || !sel.size; b.textContent = sel.size ? `選択した範囲を更新（${sel.size}）` : "選択した範囲を更新";
    };

    const row = (it, depth, kind) => {
      const pad = 8 + depth * 18;
      if (kind === "file") {
        const [lbl, cls] = FSTATE[it.status] || [it.status, ""];
        return `<div class="srow file${sel.has(it.path) ? " sel" : ""}" data-sel="${esc(it.path)}" style="padding-left:${pad + 22}px">${ftBadge(it.group)}<span class="nm" title="${esc(it.path)}">${esc(it.name)}</span><span class="fst ${cls}" title="${esc(it.error || "")}">${esc(lbl)}</span><small>${fmtSize(it.size)}</small></div>`;
      }
      const isOpen = open.has(it.path);
      const direct = info.exclude.includes(it.path);
      const inherited = it.excluded && !direct;
      const src = kind === "source";
      return `<div class="srow${sel.has(it.path) ? " sel" : ""}${it.excluded ? " off" : ""}" data-sel="${esc(it.path)}" style="padding-left:${pad}px">
          <button class="car${isOpen ? " open" : ""}" data-tog="${esc(it.path)}" aria-label="開く">▸</button>
          ${src ? `<span class="srcic">${icon(it.path ? '<path d="M3 6.5h7l2 2h9V19H3z"/><path d="M14 13h5M16.5 10.5l2.5 2.5-2.5 2.5"/>' : '<path d="M3 6.5h7l2 2h9V19H3z"/>')}</span>` : `<input type="checkbox" data-inc="${esc(it.path)}"${it.excluded ? "" : " checked"}${inherited ? " disabled title=\"上のフォルダが除外されています\"" : ' title="読み込む"'}>`}
          <span class="nm">${esc(it.name)}</span>${it.excluded ? "" : statBadges(it.stats)}
          <span class="sp"></span>${it.excluded ? "" : `<button class="btn xs" data-upd="${esc(it.path)}" title="このフォルダの変更だけを読み込みます">更新</button>`}</div>
        <div data-kids="${esc(it.path)}"${isOpen ? "" : " hidden"}></div>`;
    };
    async function fillKids(path, depth) {
      const box = $$("[data-kids]", body).find((d) => d.dataset.kids === path);
      if (!box) return;
      let r;
      try { r = await api.get("/api/scope/tree", { path }); } catch (e) { box.innerHTML = `<div class="hint" style="padding-left:${30 + depth * 18}px">${esc(e.message)}</div>`; return; }
      box.innerHTML = r.folders.map((f) => row(f, depth, "folder")).join("") + r.files.map((f) => row(f, depth, "file")).join("")
        + (r.unsupported ? `<div class="hint" style="padding-left:${30 + depth * 18}px">対象外の形式 ${r.unsupported} 件（画像など）</div>` : "")
        + (!r.folders.length && !r.files.length && !r.unsupported ? `<div class="hint" style="padding-left:${30 + depth * 18}px">空のフォルダです</div>` : "");
      await Promise.all(r.folders.filter((f) => open.has(f.path)).map((f) => fillKids(f.path, depth + 1)));
    }
    async function drawTree() {
      const tree = $("#scTree", body);
      const scroll = tree.scrollTop;
      tree.innerHTML = info.sources.map((s) => row({ path: s.prefix, name: s.id === "vault" ? `Vault（${s.path}）` : s.label, excluded: false, stats: s.stats }, 0, "source")).join("");
      await Promise.all(info.sources.filter((s) => open.has(s.prefix)).map((s) => fillKids(s.prefix, 1)));
      tree.scrollTop = scroll;
    }
    async function reload() {
      try { info = await api.get("/api/scope"); } catch (e) { fail(e); return; }
      drawSide(); drawSum(); await drawTree();
    }
    async function saveScope(update, checkPrefixes) {
      try {
        info = { ...info, ...(await api.post("/api/scope", update)) };
        drawSide();
        await api.post("/api/index/check", { prefixes: checkPrefixes }).catch(() => {});
        toast("範囲を変更しました。「更新」で反映します");
        setTimeout(poll, 300);
        await drawTree();
      } catch (e) { fail(e); }
    }
    async function removeSource(sid) {
      const s = info.sources.find((x) => x.id === sid);
      if (!(await confirmBox("外部フォルダを外す", `「${esc(s.label)}」を読み込み範囲から外しますか？<br><span class="hint">ファイルは消えません。読み込んだ内容は一覧・検索・AI から消えます。</span>`, "外す"))) return;
      try {
        info = { ...info, ...(await api.post("/api/scope", { sources: info.sources.filter((x) => x.id !== "vault" && x.id !== sid).map((x) => ({ id: x.id, path: x.path, label: x.label })) })) };
        await updateIndex(["@" + sid], `「${s.label}」を外しました`);
        await reload(); await loadTree();
      } catch (e) { fail(e); }
    }

    {
      const tree = $("#scTree", body);
      tree.addEventListener("click", async (e) => {
        const tog = e.target.closest("[data-tog]");
        if (tog) {
          const p = tog.dataset.tog, kids = $$("[data-kids]", body).find((d) => d.dataset.kids === p);
          if (open.has(p)) { open.delete(p); kids.hidden = true; tog.classList.remove("open"); }
          else { open.add(p); kids.hidden = false; tog.classList.add("open"); const depth = (p ? p.split("/").length : 0) + (p.startsWith("@") ? 0 : 1); kids.innerHTML = '<div class="hint" style="padding-left:40px"><span class="spin"></span></div>'; await fillKids(p, depth); }
          store.set("scopeOpen", [...open]);
          return;
        }
        const upd = e.target.closest("[data-upd]");
        if (upd) { updateIndex([upd.dataset.upd]); return; }
        if (e.target.closest("[data-inc]")) return;
        const r = e.target.closest("[data-sel]");
        if (!r) return;
        const p = r.dataset.sel;
        if (!(e.ctrlKey || e.metaKey)) { const had = sel.has(p) && sel.size === 1; sel.clear(); $$(".srow.sel", body).forEach((x) => x.classList.remove("sel")); if (had) { drawSum(); return; } }
        if (sel.has(p)) { sel.delete(p); r.classList.remove("sel"); } else { sel.add(p); r.classList.add("sel"); }
        drawSum();
      });
      tree.addEventListener("change", (e) => {
        const c = e.target.closest("[data-inc]"); if (!c) return;
        const p = c.dataset.inc;
        const ex = new Set(info.exclude);
        if (c.checked) { ex.delete(p); [...ex].forEach((x) => { if (x.startsWith(p + "/")) ex.delete(x); }); } else ex.add(p);
        saveScope({ exclude: [...ex] }, [p]);
      });
    }
    $("#scMax", body).onchange = (e) => saveScope({ max_mb: e.target.value }, null);
    $("#scCheck", body).onclick = () => checkIndex(null);
    $("#scUpdAll", body).onclick = () => updateIndex(null);
    $("#scUpdSel", body).onclick = () => { if (sel.size) { updateIndex([...sel]); sel.clear(); $$(".srow.sel", body).forEach((x) => x.classList.remove("sel")); drawSum(); } };
    $("#scAdd", body).onclick = () => addSourceDialog(async (path, label) => {
      const cur = info.sources.filter((x) => x.id !== "vault").map((x) => ({ id: x.id, path: x.path, label: x.label }));
      try {
        info = { ...info, ...(await api.post("/api/scope", { sources: [...cur, { path, label }] })) };
        const added = info.sources[info.sources.length - 1];
        open.add(added.prefix); store.set("scopeOpen", [...open]);
        await reload(); await loadTree();
        if (await confirmBox("外部フォルダを追加しました", `「${esc(added.label)}」を今すぐ読み込みますか？<br><span class="hint">あとで範囲を絞ってから「更新」してもかまいません。</span>`, "今すぐ読み込む")) updateIndex([added.prefix]);
        else checkIndex([added.prefix]);
        return true;
      } catch (e) { fail(e); return false; }
    });

    scopeUI = { refresh: async () => { if (!scopeUI) return; await reload(); }, sum: drawSum };
    drawSide(); drawSum(); drawTree();
  }

  function addSourceDialog(onAdd) {
    const body = document.createElement("div");
    body.innerHTML = `<div class="form" style="grid-template-columns:90px 1fr"><label for="brPath">場所</label><div style="display:flex;gap:6px"><input id="brPath" type="text" style="flex:1" placeholder="例: \\\\server\\share\\営業資料 ・ D:\\資料"><button class="btn sm" id="brGo">移動</button></div>
      <label for="brLabel">表示名</label><input id="brLabel" type="text" placeholder="空欄ならフォルダ名"></div>
      <div class="browse" id="brList"></div>`;
    let cur = "";
    const go = async (path) => {
      try {
        const r = await api.get("/api/scope/browse", { path });
        cur = r.path; $("#brPath", body).value = r.path;
        $("#brList", body).innerHTML = (r.parent !== null && r.parent !== undefined ? `<button class="res" data-br="${esc(r.parent)}"><b>↑ 上のフォルダ</b></button>` : "")
          + r.dirs.map((d) => `<button class="res" data-br="${esc(d.path)}"><b>${icon('<path d="M3 6.5h7l2 2h9V19H3z"/>')} ${esc(d.name)}</b></button>`).join("")
          + (r.dirs.length ? "" : '<div class="empty">サブフォルダはありません</div>');
      } catch (e) { fail(e); }
    };
    modal({ title: "外部フォルダを追加", body, buttons: [{ label: "キャンセル" }, { label: "このフォルダを追加", primary: true, onClick: async () => { const p = $("#brPath", body).value.trim() || cur; if (!p) return false; return (await onAdd(p, $("#brLabel", body).value.trim())) ? undefined : false; } }] });
    $("#brList", body).addEventListener("click", (e) => { const b = e.target.closest("[data-br]"); if (b) go(b.dataset.br); });
    $("#brGo", body).onclick = () => go($("#brPath", body).value.trim());
    $("#brPath", body).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(e.target.value.trim()); } });
    go("");
  }

  // ------------------------------------------------------------ 人物・組織（文書に出てくる人と会社で文書をつなぐ）
  const PICON = { person: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/>', org: '<path d="M4 20V6l8-3v17M12 9h8v11M7 9h2M7 13h2M15 13h2M15 16h2M7 16h2"/>' };
  const pIcon = (t) => `<span class="picon ${t}">${icon(PICON[t] || PICON.person)}</span>`;
  async function loadPeople() {
    const el = $("#peopleList");
    try {
      const r = await api.get("/api/people", { type: S.peopleKind, q: $("#peopleQ").value.trim() });
      S.peopleStatus = r.status;
      $$("#peopleKind button").forEach((b) => b.classList.toggle("on", b.dataset.k === S.peopleKind));
      el.innerHTML = r.entities.length ? r.entities.map((e) => `<button class="res prow2${S.entityView && S.entityView.key === e.key && S.entityView.type === e.type ? " on" : ""}" data-ent="${esc(e.type)}" data-key="${esc(e.key)}"><b>${pIcon(e.type)}${esc(e.name)}<span class="n">${e.docs}</span></b><small>${esc([e.org, e.title, e.role].filter(Boolean).join(" ・ ")) || "&nbsp;"}</small></button>`).join("")
        : `<div class="empty">${$("#peopleQ").value ? "見つかりません。" : "まだ見つかっていません。ノートや資料に「田中部長」「鈴木様」「株式会社○○」のように書かれていると、自動で拾います。"}</div>`;
      const st = r.status;
      $("#peopleFoot").innerHTML = `<div>人物 ${st.people} ・ 組織 ${st.orgs}<br>AI で抽出済み ${st.ai_extracted} / ${st.documents} 件</div>
        <button class="btn sm" id="peopleAI" title="ローカル LLM が文書ごとに人物・組織・所属・立場を読み取ります（まだ読んでいない文書だけ）"${st.llm ? "" : " disabled"}>AI で詳しく抽出</button>
        ${r.ignored.length ? `<button class="btn sm" id="peopleIgn">除外した名前（${r.ignored.length}）</button>` : ""}`;
      $("#peopleAI").onclick = async () => { try { await api.post("/api/people/extract", {}); toast("人物・組織を AI で抽出しています（ステータスバーで進み具合を確認できます）"); setTimeout(poll, 300); } catch (e) { fail(e); } };
      if ($("#peopleIgn")) $("#peopleIgn").onclick = () => ignoredDialog(r.ignored);
    } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  function ignoredDialog(list) {
    const body = document.createElement("div");
    body.innerHTML = `<p class="hint" style="margin-top:0">「人名（組織名）ではない」にした名前です。戻すと、また一覧に出ます。</p>` + list.map((x) => `<div class="srcrow"><div><b>${pIcon(x.type)}${esc(x.key)}</b></div><button class="btn sm" data-unign="${esc(x.key)}" data-t="${esc(x.type)}">戻す</button></div>`).join("");
    const m = modal({ title: "除外した名前", body });
    $$("[data-unign]", body).forEach((b) => (b.onclick = async () => { try { await api.post("/api/people/unignore", { type: b.dataset.t, key: b.dataset.unign }); b.closest(".srcrow").remove(); loadPeople(); } catch (e) { fail(e); } }));
    return m;
  }
  $("#peopleList").addEventListener("click", (e) => { const b = e.target.closest("[data-ent]"); if (b) openEntity(b.dataset.ent, b.dataset.key); });
  $("#peopleKind").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) { S.peopleKind = b.dataset.k; store.set("peopleKind", S.peopleKind); loadPeople(); } });
  let peopleTimer;
  $("#peopleQ").addEventListener("input", () => { clearTimeout(peopleTimer); peopleTimer = setTimeout(loadPeople, 200); });

  async function openEntity(type, key) {
    await flushSave();
    S.entityView = { type, key }; S.folderView = null; S.cur = null; S.entityData = null; S.profile = null;
    renderTree(); renderRight(); renderMain();
    try { S.entityData = await api.get("/api/people/entity", { type, key }); } catch (e) { fail(e); S.entityView = null; renderMain(); return; }
    renderMain();
    if (S.panel === "people") loadPeople();
  }
  async function refreshEntity() {
    if (!S.entityView) return;
    try { S.entityData = await api.get("/api/people/entity", S.entityView); } catch (e) { S.entityView = null; S.entityData = null; toast(e.message, true); }
    renderMain(); if (S.panel === "people") loadPeople();
  }
  function renderEntityView() {
    const d = S.entityData;
    $("#titleIn").disabled = true; $("#mEdit").disabled = true;
    $("#crumb").textContent = S.entityView.type === "org" ? "組織 /" : "人物 /";
    $("#titleIn").value = d ? d.name : "";
    document.title = `${d ? d.name : "人物"} — Mycel`;
    $("#stInfo").textContent = d ? `${d.appearances.length} 件の文書に登場` : ""; $("#stWords").textContent = "";
    if (!d) { $("#body").innerHTML = '<div class="welcome"><span class="spin"></span></div>'; return; }
    const chips = (xs, cls = "") => xs.map((x) => `<span class="chip ${cls}">${esc(x.name)}${x.count ? ` <small>${x.count}</small>` : ""}</span>`).join("");
    const entChips = (xs) => xs.map((x) => `<button class="chip" data-ent="${esc(x.type)}" data-key="${esc(x.key)}">${esc(x.name)} <small>${x.count}</small></button>`).join("");
    const aff = d.type === "person" ? `<div class="ehrow"><span class="lbl">所属</span>${d.affiliations.length ? d.affiliations.map((x) => `<button class="chip" data-ent="org" data-key="${esc(x.key)}">${esc(x.name)} <small>${x.count}</small></button>`).join("") : d.affiliations_inferred.length ? d.affiliations_inferred.map((x) => `<button class="chip unres" data-ent="org" data-key="${esc(x.key)}" title="一緒に出てくる組織からの推定">${esc(x.name)}?</button>`).join("") + ' <small class="hint">（一緒に出てくる組織からの推定）</small>' : '<span class="hint">不明</span>'}${d.ambiguous ? ' <small class="pend" title="同じ名前の別人が混ざっている可能性があります。違う場合は文書ごとに外してください">所属が複数</small>' : ""}</div>` : "";
    const apps = d.appearances.map((a) => `<tr><td><a data-open="${esc(a.path)}">${a.kind === "note" ? ftBadge("note") : ftBadge(a.grp)}${esc(a.title)}</a><div class="ctx">${esc(a.context)}</div></td><td>${a.roles.map((r) => `<span class="chip">${esc(r)}</span>`).join("") || '<span class="hint">—</span>'}${a.titles.length ? `<small class="hint"> ${esc(a.titles.join("・"))}</small>` : ""}</td><td class="hint">${a.mtime_ns ? esc(fmtTime(a.mtime_ns / 1e9)) : ""}${a.method === "llm" ? ' <span class="sb">AI</span>' : a.manual ? ' <span class="sb">手動</span>' : ""}</td><td><button class="rx" data-hide="${esc(a.path)}" title="この文書からこの${d.type === "org" ? "組織" : "人"}を外す（抽出し直しても外したまま）">×</button></td></tr>`).join("");
    $("#body").innerHTML = `<div class="eview">
      <div class="ehead">${pIcon(d.type)}<h1>${esc(d.name)}</h1><span class="hint">${esc(d.kind_label)}</span><span class="sp"></span>
        <button class="btn sm pri" id="evProfile" title="ローカル LLM が、登場する文書から所属・関わっている案件・関係者を推定します">AI で${d.type === "org" ? "組織像" : "人物像"}を推定</button>
        ${d.note ? `<button class="btn sm" data-open="${esc(d.note)}">ノートを開く</button>` : '<button class="btn sm" id="evNote">ノートを作る</button>'}
        <button class="ibtn" id="evMore" title="名前を直す">⋯</button></div>
      ${aff}
      ${d.titles.length ? `<div class="ehrow"><span class="lbl">肩書</span>${chips(d.titles)}</div>` : ""}
      ${d.roles.length ? `<div class="ehrow"><span class="lbl">立場</span>${chips(d.roles)}</div>` : ""}
      ${d.aliases.length ? `<div class="ehrow"><span class="lbl">別の呼び方</span>${d.aliases.map((a) => `<span class="chip">${esc(a.key)}${a.user ? ` <button class="cx" data-unmerge="${esc(a.key)}" title="まとめを外す">×</button>` : ' <small title="同じ組織のフルネームに自動でまとめました">自動</small>'}</span>`).join("")}</div>` : ""}
      <div id="evOut"></div>
      <h3>登場する文書 <small>${d.appearances.length}</small></h3>
      <table class="ftable etable"><thead><tr><th>文書</th><th>立場</th><th>更新</th><th></th></tr></thead><tbody>${apps}</tbody></table>
      <div class="egrid">
        <div><h3>一緒に出てくる人</h3>${d.co_people.length ? entChips(d.co_people) : '<span class="hint">なし</span>'}</div>
        <div><h3>${d.type === "org" ? "所属している人" : "一緒に出てくる組織"}</h3>${d.type === "org" ? (d.members.length ? entChips(d.members) : '<span class="hint">なし</span>') : (d.co_orgs.length ? entChips(d.co_orgs) : '<span class="hint">なし</span>')}</div>
      </div>
      <h3>関係がありそうな文書 <small class="hint">名前は出てこないが、所属・一緒に出てくる人・案件の言葉から推定</small></h3>
      ${d.related.length ? d.related.map((r) => `<div class="card" data-open="${esc(r.path)}"><b>${r.kind === "note" ? "" : ftBadge(r.grp)}${esc(r.title)}</b><span>${esc(r.snippet)}</span></div>`).join("") : '<div class="hint">見つかりませんでした。</div>'}
    </div>`;
    const b = $("#body");
    $$("[data-ent]", b).forEach((x) => (x.onclick = () => openEntity(x.dataset.ent, x.dataset.key)));
    $$("[data-hide]", b).forEach((x) => (x.onclick = async () => {
      try { await api.post("/api/people/hide", { path: x.dataset.hide, type: d.type, key: d.key }); toast(`「${d.name}」をこの文書から外しました（右パネルの「外した人を戻す」で戻せます）`); refreshEntity(); } catch (e) { fail(e); }
    }));
    $$("[data-unmerge]", b).forEach((x) => (x.onclick = async () => { try { await api.post("/api/people/unmerge", { type: d.type, key: x.dataset.unmerge }); toast("まとめを外しました"); refreshEntity(); } catch (e) { fail(e); } }));
    $("#evProfile").onclick = () => entityProfile(d);
    if ($("#evNote")) $("#evNote").onclick = async () => {
      const org = d.affiliations && d.affiliations[0] ? `（${d.affiliations[0].name}）` : "";
      const title = d.type === "person" ? `${d.name}${(d.titles[0] || {}).name || ""}${org}` : d.name;
      try {
        const r = await api.post("/api/note/create", { folder: d.type === "person" ? "人物" : "組織", title, text: `# ${title}\n\n- 所属: ${(d.affiliations || []).map((x) => `[[${x.name}]]`).join("、")}\n- 立場: ${d.roles.map((x) => x.name).join("、")}\n\n## メモ\n\n#${d.type === "person" ? "人物" : "組織"}\n` });
        await loadTree(); openNote(r.path, { mode: "edit" });
      } catch (e) { fail(e); }
    };
    $("#evMore").onclick = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      popMenu(r.right - 260, r.bottom + 4, [
        { label: d.type === "org" ? "同じ組織としてまとめる…" : "同一人物としてまとめる…", run: () => mergeEntity(d) },
        { label: d.type === "org" ? "組織名ではない（一覧から除く）" : "人名ではない（一覧から除く）", danger: true, run: async () => {
          if (!(await confirmBox("一覧から除く", `「${esc(d.name)}」を${d.type === "org" ? "組織名" : "人名"}ではないとして一覧から除きますか？<br><span class="hint">人物パネルの「除外した名前」から戻せます。</span>`, "除く"))) return;
          try { await api.post("/api/people/ignore", { type: d.type, key: d.key }); S.entityView = null; S.entityData = null; renderMain(); loadPeople(); toast("一覧から除きました"); } catch (err) { fail(err); }
        } },
      ]);
    };
    if (S.profile && S.profile.key === d.key) drawProfile(S.profile.data);
  }
  function mergeEntity(d) {
    api.get("/api/people", { type: d.type }).then((r) => {
      openPalette(r.entities.filter((x) => x.key !== d.key).map((x) => ({ label: x.name, hint: [x.org, `${x.docs} 件`].filter(Boolean).join(" ・ "), run: async () => {
        if (!(await confirmBox("まとめる", `「${esc(d.name)}」を「${esc(x.name)}」にまとめますか？<br><span class="hint">まとめた後も「別の呼び方」の × で外せます。</span>`, "まとめる"))) return;
        try { await api.post("/api/people/merge", { type: d.type, key: d.key, into: x.key }); openEntity(d.type, x.key); toast("まとめました"); } catch (e) { fail(e); }
      } })), d.type === "org" ? "まとめ先の組織を選ぶ" : "まとめ先の人物を選ぶ");
    }).catch(fail);
  }
  async function entityProfile(d) {
    const out = $("#evOut"); out.innerHTML = '<div class="eprof"><span class="spin"></span> 登場する文書から推定しています…</div>';
    try { const r = await api.post("/api/people/profile", { type: d.type, key: d.key }); S.profile = { key: d.key, data: r }; drawProfile(r); }
    catch (e) { out.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
  }
  function drawProfile(r) {
    const out = $("#evOut"); if (!out) return;
    out.innerHTML = `<div class="eprof">${r.answer ? `<div class="md">${MD.render(r.answer, { resolve })}</div>` : `<div class="hint">${esc(r.message || "")}</div><ul>${r.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`}
      ${r.sources.length ? `<div class="srcs">${r.sources.map((s) => `<button class="chip" data-open="${esc(s.path)}">[${s.n}] ${esc(s.title)}</button>`).join("")}</div>` : ""}</div>`;
  }

  /** 右パネル: この文書に出てくる人物・組織（外す・足す・戻す）。 */
  async function loadDocPeople(p, path) {
    const box = $("#docPeople", p); if (!box) return;
    try {
      const r = await api.get("/api/people/of", { path });
      if (!S.cur || S.cur.path !== path) return;
      box.innerHTML = (r.entities.length ? r.entities.map((e) => `<span class="chip ent ${e.type}" data-ent="${esc(e.type)}" data-key="${esc(e.key)}" title="${e.docs} 件の文書に登場${e.method === "llm" ? "（AI で抽出）" : e.manual ? "（手で追加）" : ""}">${pIcon(e.type)}${esc(e.name)}${e.roles.length ? `<small>${esc(e.roles.join("・"))}</small>` : ""}<button class="cx" data-hidep="${esc(e.key)}" data-t="${esc(e.type)}" title="この文書から外す">×</button></span>`).join("")
        : '<div class="hint">見つかっていません。</div>')
        + `<div class="relbtns"><button class="btn sm" id="pplAdd">＋ 人物・組織を追加</button>${r.hidden.length ? `<button class="btn sm" id="pplRestore">外した人を戻す（${r.hidden.length}）</button>` : ""}</div>`;
      $$("[data-ent]", box).forEach((x) => (x.onclick = (ev) => { if (!ev.target.closest(".cx")) openEntity(x.dataset.ent, x.dataset.key); }));
      $$("[data-hidep]", box).forEach((x) => (x.onclick = async (ev) => { ev.stopPropagation(); try { await api.post("/api/people/hide", { path, type: x.dataset.t, key: x.dataset.hidep }); loadDocPeople(p, path); } catch (e) { fail(e); } }));
      $("#pplAdd", box).onclick = () => addPersonDialog(path, () => loadDocPeople(p, path));
      if ($("#pplRestore", box)) $("#pplRestore", box).onclick = async () => {
        for (const h of r.hidden) { try { await api.post("/api/people/unhide", { path, type: h.type, key: h.key }); } catch (e) { fail(e); } }
        loadDocPeople(p, path);
      };
    } catch (e) { box.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
  }
  function addPersonDialog(path, done) {
    const body = document.createElement("div");
    body.innerHTML = `<div class="form"><label for="apType">種類</label><select id="apType"><option value="person">人物</option><option value="org">組織</option></select>
      <label for="apName">名前</label><input id="apName" type="text" placeholder="例: 田中部長（A社） ・ 株式会社○○">
      <label for="apRole">立場（任意）</label><input id="apRole" type="text" placeholder="例: 決裁者・窓口・出席者・競合"></div>
      <p class="hint">抽出されなかった人を、この文書に手で結びつけます。（A社）のように書くと所属も記録します。</p>`;
    modal({ title: "人物・組織を追加", body, buttons: [{ label: "キャンセル" }, { label: "追加", primary: true, onClick: async () => {
      const name = $("#apName", body).value.trim(); if (!name) return false;
      try { await api.post("/api/people/add", { path, type: $("#apType", body).value, name, role: $("#apRole", body).value }); done(); } catch (e) { fail(e); return false; }
    } }] });
  }

  // ------------------------------------------------------------ フォルダの一覧表示
  async function openFolder(path) {
    await flushSave();
    S.folderView = path; S.cur = null; S.entityView = null;
    S.closed.delete(path); store.set("closed", [...S.closed]);
    renderTree(); renderRight();
    await refreshFolder(true);
  }
  let fvSel = new Set(), fvData = null;
  async function refreshFolder(reset = false) {
    if (S.folderView === null) return;
    try { fvData = await api.get("/api/folder", { path: S.folderView }); } catch (e) { fail(e); return; }
    if (reset) fvSel = new Set();
    [...fvSel].forEach((p) => { if (!fvData.items.some((i) => i.path === p)) fvSel.delete(p); });
    renderMain();
  }
  function renderFolderView() {
    const d = fvData, ed = d && d.editable;
    $("#titleIn").disabled = true; $("#mEdit").disabled = true;
    $("#titleIn").value = d ? (d.path ? (d.path.includes("/") || !d.path.startsWith("@") ? d.path.split("/").pop() : d.label) : "Vault") : "";
    $("#crumb").textContent = "フォルダ /";
    document.title = `${d ? d.label : "フォルダ"} — Mycel`;
    $("#stInfo").textContent = d ? `フォルダ ${d.folders.length} ・ ノート ${d.items.filter((i) => i.kind === "note").length} ・ 資料 ${d.items.filter((i) => i.kind !== "note").length}` : "";
    $("#stWords").textContent = "";
    if (!d) { $("#body").innerHTML = '<div class="welcome"><span class="spin"></span></div>'; return; }
    const parts = d.path ? d.path.split("/") : [];
    const crumbs = [`<a data-fv="">Vault</a>`].concat(parts.map((p, i) => {
      const fp = parts.slice(0, i + 1).join("/");
      return `<a data-fv="${esc(fp)}">${esc(i === 0 && p.startsWith("@") ? docLabel(p) : p)}</a>`;
    })).join(" / ");
    const rows = d.folders.map((f) => `<tr class="fvf" data-fv="${esc(f.path)}"><td></td><td>${icon(f.source ? '<path d="M3 6.5h7l2 2h9V19H3z"/><path d="M14 13h5M16.5 10.5l2.5 2.5-2.5 2.5"/>' : '<path d="M3 6.5h7l2 2h9V19H3z"/>')} ${esc(f.name)}</td><td>フォルダ</td><td>${f.count} 件</td><td></td><td></td></tr>`).join("")
      + d.items.map((n) => `<tr class="${fvSel.has(n.path) ? "sel" : ""}${n.status !== "ok" ? " err" : ""}" data-row="${esc(n.path)}" draggable="${n.path.startsWith("@") ? "false" : "true"}"><td><input type="checkbox" data-fsel="${esc(n.path)}"${fvSel.has(n.path) ? " checked" : ""} aria-label="選択"></td><td><a data-open="${esc(n.path)}">${n.kind === "note" ? ftBadge("note") : ftBadge(n.grp)}${esc(n.title)}</a>${n.status !== "ok" ? ' <small class="ng">読み込みエラー</small>' : ""}</td><td>${n.kind === "note" ? "ノート" : esc(GRP_NAME[n.grp] || "資料")}</td><td>${fmtSize(n.size || 0)}</td><td>${n.mtime_ns ? esc(fmtTime(n.mtime_ns / 1e9)) : ""}</td><td>${n.relations ? `<span class="sb">${n.relations}</span>` : ""}</td></tr>`).join("");
    const n = fvSel.size, docsSel = [...fvSel].filter(isDoc);
    $("#body").innerHTML = `<div class="fview">
      <div class="fvhead"><div class="crumbs">${crumbs}</div><span class="sp"></span>
        ${ed ? `<button class="btn sm pri" id="fvAdd">＋ 資料を追加</button><button class="btn sm" id="fvNote">新しいノート</button><button class="btn sm" id="fvFolder">新しいフォルダ</button>` : '<span class="hint">外部フォルダ（読み取り専用）</span>'}
        <button class="btn sm" id="fvProp" title="内容の近い資料の組を探して、まとめてつなぎます">つながりを提案</button><button class="ibtn" id="fvMore" title="フォルダの操作">⋯</button></div>
      ${ed ? '<div class="fvdrop" id="fvDrop">パソコンのファイルをここにドロップすると、このフォルダに資料として追加します（AI で要約したノートにしたいときは受信箱ボタンの「AI 取り込み」）</div>' : ""}
      <div class="fvbar"${n ? "" : " hidden"}><b>${n} 件を選択</b>
        ${ed ? '<button class="btn sm" data-fva="move">フォルダへ移動…</button>' : ""}
        ${docsSel.length ? `<button class="btn sm" data-fva="ingest">AI でノート化（${docsSel.length}）</button>` : ""}
        ${n === 2 ? '<button class="btn sm" data-fva="relate">この 2 件をつなぐ…</button>' : ""}
        ${ed ? '<button class="btn sm danger" data-fva="delete">削除</button>' : ""}
        <button class="btn sm" data-fva="clear">選択を解除</button></div>
      <table class="ftable"><thead><tr><th><input type="checkbox" id="fvAll" aria-label="すべて選択"${d.items.length && n === d.items.length ? " checked" : ""}></th><th>名前</th><th>種類</th><th>サイズ</th><th>更新</th><th title="資料同士のつながり">つながり</th></tr></thead>
      <tbody>${rows || '<tr><td></td><td colspan="5" class="hint">空のフォルダです。</td></tr>'}</tbody></table></div>`;
    const b = $("#body");
    $$("[data-fv]", b).forEach((a) => (a.onclick = () => openFolder(a.dataset.fv)));
    if (ed) { $("#fvAdd").onclick = () => pickFiles(d.path); $("#fvNote").onclick = () => newNote(d.path); $("#fvFolder").onclick = () => newFolder(d.path); }
    $("#fvProp").onclick = () => proposeRelations(d.path);
    $("#fvMore").onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); folderMenu(r.right - 240, r.bottom + 4, d.path); };
    $("#fvAll").onchange = (e) => { fvSel = e.target.checked ? new Set(d.items.map((i) => i.path)) : new Set(); renderFolderView(); };
    b.querySelector("tbody").addEventListener("change", (e) => { const c = e.target.closest("[data-fsel]"); if (c) { c.checked ? fvSel.add(c.dataset.fsel) : fvSel.delete(c.dataset.fsel); renderFolderView(); } });
    b.querySelector("tbody").addEventListener("contextmenu", (e) => { const r = e.target.closest("[data-row]"); if (r) { e.preventDefault(); noteMenu(e.clientX, e.clientY, r.dataset.row); } });
    b.querySelector("tbody").addEventListener("dragstart", (e) => { const r = e.target.closest("[data-row]"); if (r) e.dataTransfer.setData("text/mycel-path", r.dataset.row); });
    $$("[data-fva]", b).forEach((x) => (x.onclick = () => folderBulk(x.dataset.fva)));
    const fv = $(".fview", b);
    fv.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files") && ed) { e.preventDefault(); e.stopPropagation(); fv.classList.add("over"); } });
    fv.addEventListener("dragleave", (e) => { if (e.target === fv) fv.classList.remove("over"); });
    fv.addEventListener("drop", (e) => { if ([...e.dataTransfer.types].includes("Files") && ed) { e.preventDefault(); e.stopPropagation(); fv.classList.remove("over"); addFiles(d.path, [...e.dataTransfer.files]); } });
  }
  async function folderBulk(act) {
    const paths = [...fvSel];
    if (act === "clear") { fvSel.clear(); renderFolderView(); return; }
    if (act === "ingest") { openIngest({ paths: paths.filter(isDoc) }); return; }
    if (act === "relate") { relateDialog(paths[0], paths[1]); return; }
    if (act === "move") {
      const opts = ["", ...S.folders].map((f) => `<option value="${esc(f)}"${f === S.folderView ? " selected" : ""}>${f ? esc(f) : "（Vault 直下）"}</option>`).join("");
      modal({ title: `${paths.length} 件をフォルダへ移動`, body: `<div class="form"><label for="mvSel2">移動先</label><select id="mvSel2">${opts}</select><label for="mvNew2">新しいフォルダ</label><input id="mvNew2" type="text" placeholder="（入力すると優先）"></div>`,
        buttons: [{ label: "キャンセル" }, { label: "移動", primary: true, onClick: async () => {
          const f = ($("#mvNew2").value.trim() || $("#mvSel2").value).replace(/^\/+|\/+$/g, "");
          for (const p of paths) if (folderOf(p) !== f) await moveTo(p, f);
          fvSel.clear(); refreshFolder();
        } }] });
      return;
    }
    if (act === "delete") {
      if (!(await confirmBox("まとめて削除", `${paths.length} 件を削除しますか？<br><span class="hint">Vault 内の .mycel/trash に移動します。</span>`, "削除する", true))) return;
      for (const p of paths) { try { await api.post("/api/item/delete", { path: p }); } catch (e) { fail(e); } }
      fvSel.clear(); await loadTree(); refreshFolder(); toast("削除しました");
    }
  }

  // ------------------------------------------------------------ 資料同士のつながり
  async function relateDialog(a, b = null) {
    const done = async (target) => {
      const label = await promptBox("つながりの説明（任意）", "例: 改訂版 / 添付資料 / 前提となる仕様", "");
      if (label === null) return;
      try {
        await api.post("/api/relations/add", { a, b: target, label });
        toast("つなぎました");
        afterRelationChange();
      } catch (e) { fail(e); }
    };
    if (b) return done(b);
    openPalette(S.tree.filter((n) => n.path !== a).map((n) => ({ label: n.title, hint: (n.kind === "note" ? "ノート ・ " : "資料 ・ ") + docLabel(n.folder), run: () => done(n.path) })), "つなぐノート・資料を選ぶ");
  }
  async function afterRelationChange() {
    if (S.cur) { try { const d = await api.get("/api/note", { path: S.cur.path }); S.cur.relations = d.relations; } catch { /* 無視 */ } renderRight(); }
    refreshFolder();
  }
  function relationsHtml(c) {
    const rels = c.relations || [];
    let h = `<div class="rh"><span>つながり</span><span>${rels.length}</span></div>`;
    h += rels.length ? rels.map((r) => `<div class="card rel${r.exists ? "" : " gone"}" data-open="${r.exists ? esc(r.path) : ""}"><b>${r.kind === "note" ? "" : ftBadge(r.grp)}${esc(r.title)}${r.origin === "ai" ? ' <small class="sb">AI</small>' : ""}</b>${r.label ? `<span>${esc(r.label)}</span>` : ""}${r.exists ? "" : '<span class="ng">見つかりません（移動・削除された可能性）</span>'}<button class="rx" data-unrel="${esc(r.path)}" title="つながりを外す">×</button></div>`).join("")
      : `<div class="hint">${c.readonly ? "資料には [[リンク]] を書けないので、ここで他の資料・ノートとつなげます。" : "資料やノートとのつながりを追加できます。"}</div>`;
    h += `<div class="relbtns"><button class="btn sm" id="relAdd">＋ つなぐ…</button><button class="btn sm" id="relSug" title="内容の近いノート・資料を探し、ローカル LLM が関係を判断します">似ているものを探す（AI）</button></div><div id="relOut"></div>`;
    return h;
  }
  function bindRelations(p) {
    const c = S.cur;
    $$("[data-unrel]", p).forEach((b) => (b.onclick = async (e) => {
      e.stopPropagation();
      try { await api.post("/api/relations/remove", { a: c.path, b: b.dataset.unrel }); afterRelationChange(); } catch (err) { fail(err); }
    }));
    $("#relAdd", p).onclick = () => relateDialog(c.path);
    $("#relSug", p).onclick = async () => {
      const out = $("#relOut", p); out.innerHTML = '<span class="spin"></span> 探しています…';
      try {
        const r = await api.get("/api/relations/suggest", { path: c.path });
        out.innerHTML = r.suggestions.length ? r.suggestions.map((s) => `<div class="card" data-open="${esc(s.path)}"><b>${s.kind === "note" ? "" : ftBadge(s.grp)}${esc(s.title)}</b><span>${esc(s.reason || s.snippet)}</span><div class="acts"><button class="btn sm" data-relto="${esc(s.path)}" data-label="${esc(s.reason || "")}">つなぐ</button></div></div>`).join("") : '<div class="hint">候補は見つかりませんでした。</div>';
        $$("[data-relto]", out).forEach((b) => (b.onclick = async (e) => {
          e.stopPropagation();
          try { await api.post("/api/relations/add", { a: c.path, b: b.dataset.relto, label: b.dataset.label, origin: "ai" }); b.closest(".card").remove(); afterRelationChange(); } catch (err) { fail(err); }
        }));
      } catch (e) { out.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
    };
  }
  async function proposeRelations(prefix) {
    const m = modal({ title: `資料のつながりを提案（${prefix ? docLabel(prefix) : "Vault"}）`, body: '<span class="spin"></span> 内容の近い資料を探しています…', wide: false, buttons: [] });
    let pairs;
    try { pairs = (await api.post("/api/relations/propose", { prefix })).pairs; } catch (e) { m.close(); fail(e); return; }
    m.close();
    const body = document.createElement("div");
    body.innerHTML = pairs.length ? `<p class="hint" style="margin-top:0">キーワード索引で内容の近さを測った候補です（100% ＝ ほぼ同じ内容）。つなぐ組を選んでください。1 件ずつ AI に理由を判断させたいときは、資料を開いて「似ているものを探す（AI）」を使います。</p>
      <div class="proplist">${pairs.map((p, i) => `<label class="prow"><input type="checkbox" data-pi="${i}"${p.score >= 0.5 ? " checked" : ""}><span class="pa">${ftBadge(p.a_grp)}${esc(p.a_title)}</span><span class="pm"><i style="width:${Math.round(p.score * 100)}%"></i><small>${Math.round(p.score * 100)}%</small></span><span class="pa">${ftBadge(p.b_grp)}${esc(p.b_title)}</span></label>`).join("")}</div>`
      : '<p class="hint">つなぐ候補は見つかりませんでした（資料が少ないか、内容が離れています）。</p>';
    modal({ title: `資料のつながりを提案（${pairs.length} 組）`, body, wide: false, buttons: [{ label: "閉じる" }, ...(pairs.length ? [{ label: "選んだ組をつなぐ", primary: true, onClick: async () => {
      const sel = $$("[data-pi]:checked", body).map((c) => pairs[+c.dataset.pi]);
      if (!sel.length) return false;
      try { const r = await api.post("/api/relations/many", { pairs: sel.map((p) => ({ a: p.a, b: p.b })), origin: "ai" }); toast(`${r.added} 組をつなぎました`); afterRelationChange(); } catch (e) { fail(e); return false; }
    } }] : [])] });
  }

  // ------------------------------------------------------------ AI 取り込み（記法の無い文書 → つながったノート）
  let ingestUI = null;
  const IG_ST = { queued: ["待機", "off"], processing: ["処理中", "mod"], ready: ["下書き完了", "new"], error: ["エラー", "ng"], saved: ["保存済み", ""] };
  const IG_ACCEPT = ".pdf,.docx,.xlsx,.xlsm,.pptx,.eml,.txt,.log,.csv,.tsv,.html,.htm,.json,.xml,.yaml,.yml";

  async function uploadFiles(files) {
    let ok = 0;
    for (const f of files) {
      try {
        const res = await fetch("/api/ingest/upload", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(f.name) }, body: f });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `エラー (${res.status})`);
        ok++;
      } catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    if (ok) toast(`${ok} 件のファイルを追加しました。「AI で下書きを作る」を押してください`);
    return ok;
  }

  async function openIngest({ files = null, paths = null, autoRun = false } = {}) {
    if (!ingestUI) buildIngest();
    if (files && files.length) await uploadFiles(files);
    if (paths && paths.length) { try { await api.post("/api/ingest/add", { paths }); } catch (e) { fail(e); } }
    await ingestUI.refresh();
    if (autoRun) ingestUI.run();
  }

  function buildIngest() {
    let data = { drafts: [], options: {}, llm: {} }, cur = null, sel = new Set(), tab = "prev", editTimer = 0;
    const body = document.createElement("div");
    body.className = "ing";
    body.innerHTML = `<div class="ing-llm" id="igLlm"></div>
      <div class="ing-grid">
        <div class="ing-left">
          <div class="drop" id="igDrop" tabindex="0" role="button">${icon('<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4 14v5h16v-5"/>')}<b>ここにファイルをドロップ</b><small>またはクリックして選ぶ ・ PDF / Word / Excel / PowerPoint / メール / テキスト</small><input type="file" id="igFile" multiple accept="${IG_ACCEPT}" hidden></div>
          <button class="btn sm" id="igPick">読み込み済みの資料から選ぶ…</button>
          <div class="ing-list" id="igList" role="listbox" aria-label="取り込むファイル"></div>
          <details class="ing-opts" id="igOpts"><summary>取り込みの設定</summary><div class="form"></div></details>
          <div class="ing-acts"><button class="btn pri" id="igRun">AI で下書きを作る</button><button class="btn sm danger" id="igDiscard" disabled>選択を破棄</button></div>
        </div>
        <div class="ing-right" id="igDetail"></div>
      </div>`;
    const m = modal({
      title: "AI 取り込み — 文書をつながったノートにする", body, wide: true,
      buttons: [{ label: "保存済みを片付ける", onClick: () => { discard(data.drafts.filter((d) => d.status === "saved").map((d) => d.id), false); return false; } },
                { label: "閉じる" },
                { label: "下書きをすべて保存", primary: true, onClick: () => { save(data.drafts.filter((d) => d.status === "ready").map((d) => d.id)); return false; } }],
      onClose: () => { flushEdit(); ingestUI = null; },
    });
    m.el.querySelector(".modal").classList.add("xwide");
    const saveAllBtn = $$("footer .btn", m.el).pop();

    const opt = (k) => data.options[k];
    function drawLlm() {
      const l = data.llm, el = $("#igLlm", body);
      if (!l.chat) el.innerHTML = `<span class="dot err"></span><span>LLM が未設定です。本文の取り込みとキーワード検索での関連付けだけ行います。</span><button class="btn sm" id="igSetup">ローカル LLM を設定</button>`;
      else el.innerHTML = `<span class="dot${l.local ? "" : " dirty"}"></span><span>${l.local ? "ローカル LLM" : "<b>外部の LLM</b>"}: <b>${esc(l.model)}</b> <small>${esc(l.base_url)}</small>${l.embed_model ? ` ・ 意味検索: ${esc(l.embed_model)}` : " ・ 関連付けはキーワード検索（Embed モデル未設定）"}</span>${l.local ? "" : '<span class="ng">文書の内容が外部に送られます</span>'}<button class="btn sm" id="igSetup">LLM の設定</button>`;
      $("#igSetup", body).onclick = () => openSettings("llm");
    }
    function drawOpts() {
      const o = data.options, box = $("#igOpts .form", body);
      if (box.dataset.done) return;
      box.dataset.done = "1";
      box.innerHTML = `<label for="igDest">ノートの保存先</label><input id="igDest" type="text" value="${esc(o.dest_folder || "")}" placeholder="（Vault 直下）">
        <label></label><label class="ck"><input type="checkbox" id="igKeep"${o.keep_original ? " checked" : ""}> 原本を Vault に保存する（ノートから原本へリンク）</label>
        <label for="igOrig">原本の保存先</label><input id="igOrig" type="text" value="${esc(o.original_folder || "")}" placeholder="（Vault 直下）">
        <label></label><label class="ck"><input type="checkbox" id="igBody"${o.include_body ? " checked" : ""}> ノートに本文も入れる（原本を保存しないときは常に入れます）</label>
        <label></label><label class="ck"><input type="checkbox" id="igNew"${o.link_new_names ? " checked" : ""}> まだノートの無い顧客名・人名も [[リンク]] にする（オフでも人物・組織としてつながります）</label>
        <label></label><label class="ck"><input type="checkbox" id="igLlmUse"${o.use_llm ? " checked" : ""}> AI（LLM）で要約・名前・タグ・関連を作る</label>`;
    }
    const options = () => ({ dest_folder: $("#igDest", body).value, keep_original: $("#igKeep", body).checked, original_folder: $("#igOrig", body).value,
      include_body: $("#igBody", body).checked, link_new_names: $("#igNew", body).checked, use_llm: $("#igLlmUse", body).checked });

    function drawList() {
      const el = $("#igList", body), ds = data.drafts;
      el.innerHTML = ds.length ? ds.map((d) => {
        const [lbl, cls] = IG_ST[d.status] || [d.status, ""];
        return `<div class="igrow${cur === d.id ? " on" : ""}" data-id="${esc(d.id)}" role="option"><input type="checkbox" data-ck="${esc(d.id)}"${sel.has(d.id) ? " checked" : ""} aria-label="選択">${ftBadge(d.grp)}<span class="nm" title="${esc(d.origin === "doc" ? d.source : d.name)}">${esc(d.status === "ready" || d.status === "saved" ? d.title || d.name : d.name)}${d.origin === "doc" ? ' <small>読込済みの資料</small>' : ""}</span><span class="fst ${cls}">${d.status === "processing" ? '<span class="spin"></span> ' : ""}${esc(lbl)}</span></div>`;
      }).join("") : '<div class="empty">まだファイルがありません。上にドロップしてください。</div>';
      const todo = ds.filter((d) => ["queued", "error"].includes(d.status)).length;
      const running = S.index && S.index.job && S.index.job.state === "running";
      const runBtn = $("#igRun", body);
      runBtn.disabled = running || !(sel.size ? [...sel].some((id) => (ds.find((d) => d.id === id) || {}).status !== "saved") : todo);
      runBtn.textContent = running && S.index.job.kind === "ingest" ? "作成中…" : sel.size ? `選択した ${sel.size} 件の下書きを作る` : `AI で下書きを作る（${todo}）`;
      $("#igDiscard", body).disabled = !sel.size;
      const ready = ds.filter((d) => d.status === "ready").length;
      saveAllBtn.disabled = !ready; saveAllBtn.textContent = `下書きをすべて保存（${ready}）`;
    }

    function drawDetail() {
      const el = $("#igDetail", body);
      const d = data.drafts.find((x) => x.id === cur);
      if (!d) {
        el.innerHTML = `<div class="ing-help"><h3>文書を「つながったノート」にします</h3><ol>
          <li><b>ファイルを入れる</b> — 左にドロップ（アプリのどこにドロップしても開きます）。読み込み済みの資料からも選べます。</li>
          <li><b>AI が読む</b> — ローカル LLM が本文を区切って読み、タイトル・要約・要点・登場する顧客名や人名・タグをまとめます。</li>
          <li><b>RAG で関連付け</b> — 要約と名前で既存のノート・資料を検索し、本当に関係するものを LLM が選んで理由を付けます。</li>
          <li><b>確認して保存</b> — 下書きを直して保存。原本は資料として Vault に入り、ノートから <code>[[原本]]</code> でたどれます。</li></ol>
          <p class="hint">保存したノートは <code>[[顧客名]]</code> などのリンクでグラフ・バックリンク・AI の質問につながります。LLM がなくても本文の取り込みはできます。</p></div>`;
        return;
      }
      const head = `<div class="igh">${ftBadge(d.grp)}<b>${esc(d.name)}</b><small>${fmtSize(d.size)}${d.chars ? ` ・ ${d.chars.toLocaleString()} 文字` : ""}${d.model ? ` ・ ${esc(d.model)}` : d.status === "ready" ? " ・ AI なし" : ""}</small></div>`;
      if (d.status === "queued" || d.status === "processing") {
        el.innerHTML = head + `<div class="ing-wait">${d.status === "processing" ? `<span class="spin"></span> ${esc(d.phase || "処理中")}` : "待機中です。「AI で下書きを作る」を押すと始まります。"}</div>`;
        return;
      }
      if (d.status === "error") {
        el.innerHTML = head + `<div class="notice err">${esc(d.error)}</div><button class="btn sm" data-igact="rerun">作り直す</button> <button class="btn sm danger" data-igact="discard">破棄</button>`;
        bindActs(d); return;
      }
      if (d.status === "saved") {
        el.innerHTML = head + `<div class="notice">ノート「${esc(titleOf(d.saved_path))}」として保存しました。${d.original_path && d.origin === "upload" ? `原本は <code>${esc(d.original_path)}</code> にあります。` : ""}</div><button class="btn sm pri" data-igact="open">ノートを開く</button>`;
        bindActs(d); return;
      }
      el.innerHTML = head + `${d.error ? `<div class="notice">${esc(d.error)}</div>` : ""}
        <div class="form igpath"><label for="igPath">保存するノート</label><input id="igPath" type="text" value="${esc(d.note_path.replace(/\.md$/, ""))}"></div>
        ${d.related.length ? `<div class="srcs igrel"><span class="hint">関連（RAG）:</span>${d.related.map((r) => `<button class="chip" data-open="${esc(r.path)}" title="${esc(r.snippet)}">${esc(r.title)}${r.reason ? " — " + esc(r.reason) : ""}</button>`).join("")}</div>` : ""}
        <div class="seg igtabs"><button data-tab="prev" class="${tab === "prev" ? "on" : ""}">プレビュー</button><button data-tab="edit" class="${tab === "edit" ? "on" : ""}">編集</button></div>
        <div class="igbody">${tab === "edit" ? `<textarea class="editor" id="igMd" spellcheck="false" aria-label="下書き">${esc(d.markdown)}</textarea>` : `<article class="md">${MD.render(d.markdown, { resolve })}</article>`}</div>
        <div class="ing-acts"><button class="btn pri" data-igact="save">このノートを保存</button><button class="btn sm" data-igact="rerun" title="設定を変えて作り直します">作り直す</button><button class="btn sm danger" data-igact="discard">破棄</button></div>`;
      $$("[data-tab]", el).forEach((b) => (b.onclick = () => { flushEdit(); tab = b.dataset.tab; drawDetail(); }));
      const ta = $("#igMd", el);
      if (ta) ta.addEventListener("input", () => { d.markdown = ta.value; clearTimeout(editTimer); editTimer = setTimeout(() => flushEdit(), 700); });
      $("#igPath", el).addEventListener("change", async (e) => { try { const r = await api.post("/api/ingest/edit", { id: d.id, note_path: e.target.value }); d.note_path = r.note_path; } catch (err) { fail(err); e.target.value = d.note_path.replace(/\.md$/, ""); } });
      $$("[data-open]", el).forEach((b) => b.addEventListener("click", () => m.close()));
      bindActs(d);
    }
    let pendingEdit = null;
    function flushEdit() {
      clearTimeout(editTimer);
      const d = data.drafts.find((x) => x.id === cur);
      if (!d || d.status !== "ready") return Promise.resolve();
      const ta = $("#igMd", body);
      if (!ta) return pendingEdit || Promise.resolve();
      pendingEdit = api.post("/api/ingest/edit", { id: d.id, markdown: ta.value }).catch(fail);
      return pendingEdit;
    }
    function bindActs(d) {
      $$("[data-igact]", $("#igDetail", body)).forEach((b) => (b.onclick = async () => {
        const act = b.dataset.igact;
        if (act === "save") { await flushEdit(); save([d.id]); }
        else if (act === "rerun") run([d.id]);
        else if (act === "discard") discard([d.id], true);
        else if (act === "open") { m.close(); openNote(d.saved_path); }
      }));
    }

    async function refresh() {
      try { data = await api.get("/api/ingest"); } catch (e) { fail(e); return; }
      if (cur && !data.drafts.some((d) => d.id === cur)) cur = null;
      if (!cur && data.drafts.length) cur = (data.drafts.find((d) => d.status === "ready") || data.drafts[0]).id;
      [...sel].forEach((id) => { if (!data.drafts.some((d) => d.id === id)) sel.delete(id); });
      drawLlm(); drawOpts(); drawList();
      // 編集中は本文を描き直さない
      const ta = $("#igMd", body);
      if (!(ta && document.activeElement === ta)) drawDetail();
    }
    async function run(ids = null) {
      await flushEdit();
      const target = ids || (sel.size ? [...sel] : null);
      try {
        await api.post("/api/ingest/run", { ids: target, options: options() });
        toast("AI が読み込んでいます（閉じても続きます。進み具合はステータスバー）");
        setTimeout(poll, 300); setTimeout(refresh, 400);
      } catch (e) { fail(e); }
    }
    async function save(ids) {
      if (!ids.length) return;
      await flushEdit();
      try {
        const r = await api.post("/api/ingest/save", { ids });
        r.errors.forEach((e) => toast(e.error, true));
        if (r.saved.length) {
          await loadTree();
          toast(`${r.saved.length} 件のノートを保存しました${r.embedding ? "（意味検索に登録しています）" : ""}`);
        }
        await refresh();
      } catch (e) { fail(e); }
    }
    async function discard(ids, ask) {
      if (!ids.length) return;
      if (ask && !(await confirmBox("下書きを破棄", `${ids.length} 件の下書きを破棄しますか？<br><span class="hint">アップロードしたファイルの一時コピーも消えます（元のファイルはそのままです）。</span>`, "破棄", true))) return;
      try { await api.post("/api/ingest/discard", { ids }); ids.forEach((id) => sel.delete(id)); await refresh(); } catch (e) { fail(e); }
    }

    const drop = $("#igDrop", body), fileIn = $("#igFile", body);
    drop.addEventListener("click", (e) => { if (e.target !== fileIn) fileIn.click(); });
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } });
    fileIn.onchange = async () => { const fs = [...fileIn.files]; fileIn.value = ""; if (await uploadFiles(fs)) refresh(); };
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
    $("#igPick", body).onclick = () => pickDocs(async (paths) => { try { await api.post("/api/ingest/add", { paths }); await refresh(); } catch (e) { fail(e); } });
    $("#igList", body).addEventListener("click", (e) => {
      const ck = e.target.closest("[data-ck]");
      if (ck) { ck.checked ? sel.add(ck.dataset.ck) : sel.delete(ck.dataset.ck); drawList(); return; }
      const row = e.target.closest("[data-id]");
      if (row) { flushEdit(); cur = row.dataset.id; tab = "prev"; drawList(); drawDetail(); }
    });
    $("#igRun", body).onclick = () => run();
    $("#igDiscard", body).onclick = () => discard([...sel], true);
    ingestUI = { refresh, run: () => run(), el: m.el };
  }

  /** 読み込み済みの資料を複数選ぶ。 */
  function pickDocs(onPick) {
    const docs = S.tree.filter((n) => n.kind !== "note" && n.status === "ok");
    const body = document.createElement("div");
    body.innerHTML = `<input class="field-in" id="pdQ" type="search" placeholder="名前で絞り込み" style="margin:0 0 8px;width:100%"><div class="pdlist" id="pdList"></div>`;
    const draw = () => {
      const q = $("#pdQ", body).value.trim().toLowerCase();
      const hits = docs.filter((d) => d.path.toLowerCase().includes(q)).slice(0, 300);
      $("#pdList", body).innerHTML = hits.length ? hits.map((d) => `<label class="pdrow"><input type="checkbox" value="${esc(d.path)}">${ftBadge(d.grp)}<span>${esc(d.title)}</span><small>${esc(docLabel(d.folder))}</small></label>`).join("") : '<div class="empty">資料がありません（読み込み範囲に資料フォルダを追加して「更新」してください）</div>';
    };
    modal({ title: "読み込み済みの資料から選ぶ", body, buttons: [{ label: "キャンセル" }, { label: "追加", primary: true, onClick: () => { const v = $$("#pdList input:checked", body).map((c) => c.value); if (v.length) onPick(v); } }] });
    $("#pdQ", body).addEventListener("input", draw);
    draw();
  }

  // アプリのどこにファイルをドロップしても取り込みを開く
  document.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    const files = [...e.dataTransfer.files];
    if (files.length) openIngest({ files });
  });

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
        <label>ローカル LLM</label><div class="presets"><button class="btn sm" data-preset="http://127.0.0.1:11434/v1">Ollama</button><button class="btn sm" data-preset="http://127.0.0.1:1234/v1">LM Studio</button><button class="btn sm" data-preset="http://127.0.0.1:8080/v1">llama.cpp server</button></div>
        <div class="help">文書を外部に出さないため、ローカル LLM を基本にしています。例: <code>ollama pull qwen2.5:7b</code>（チャット）と <code>ollama pull bge-m3</code>（意味検索）。</div>
        <label for="cProv">接続方式</label><select id="cProv"><option value="openai">OpenAI 互換 API（OpenAI / Ollama / LM Studio / vLLM など）</option><option value="azure">Azure OpenAI</option></select>
        <label for="cUrl">Base URL</label><input id="cUrl" type="text" value="${v("base_url")}" placeholder="例: http://127.0.0.1:11434/v1 ・ https://api.openai.com/v1">
        <div class="help" id="urlHelp"></div>
        <label for="cKey">API キー</label><input id="cKey" type="password" autocomplete="off" placeholder="${cfg.has_api_key ? "設定済み（変更する場合だけ入力）" : "ローカル LLM なら空欄で可"}">
        ${cfg.has_api_key ? '<label></label><label style="color:var(--muted)"><input type="checkbox" id="cKeyClr"> 保存済みのキーを消す</label>' : ""}
        <label for="cModel" id="modelLbl">モデル</label><div class="inrow"><input id="cModel" type="text" list="dlModels" value="${v("model")}" placeholder="例: qwen2.5:7b ・ gemma2:9b"><button class="btn sm" id="cList" type="button">一覧を取得</button></div><datalist id="dlModels"></datalist>
        <div class="help" id="cListRes"></div>
        <label for="cVer" class="az">API バージョン</label><input id="cVer" class="az" type="text" value="${v("api_version")}">
        <h4>意味検索（任意）</h4>
        <label for="cEmb">Embed モデル</label><input id="cEmb" type="text" list="dlModels" value="${v("embed_model")}" placeholder="例: bge-m3 ・ nomic-embed-text">
        <div class="help">空欄ならキーワード検索だけで関連ノートを探します。</div>
        <label for="cEmbUrl">Embed の Base URL</label><input id="cEmbUrl" type="text" value="${v("embed_base_url")}" placeholder="空欄なら上と同じ">
        <label for="cEmbKey">Embed の API キー</label><input id="cEmbKey" type="password" autocomplete="off" placeholder="${cfg.has_embed_api_key ? "設定済み" : "空欄なら上と同じ"}">
        <h4>詳細</h4>
        <label for="cTemp">Temperature</label><input id="cTemp" type="number" step="0.1" min="0" max="2" value="${v("temperature")}">
        <label for="cMax">Max tokens</label><input id="cMax" type="number" min="64" value="${v("max_tokens")}">
        <label for="cTo">タイムアウト（秒）</label><input id="cTo" type="number" min="5" value="${v("request_timeout")}">
        <div class="help">ローカル LLM は 1 回の応答に時間がかかることがあります。長い文書の取り込みでは 300 秒以上を推奨します。</div>
        <label for="cChunk">取り込みの区切り（文字）</label><input id="cChunk" type="number" min="500" step="500" value="${v("ingest_chunk_chars")}">
        <div class="help">AI 取り込みで 1 回に LLM へ送る文字数。モデルの文脈長（Ollama の既定は 2048〜4096 トークン程度）に合わせて小さめに。</div>
        <label for="cMaxChunk">取り込みで読む区画の上限</label><input id="cMaxChunk" type="number" min="1" value="${v("ingest_max_chunks")}">
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
        request_timeout: $("#cTo", body).value, ingest_chunk_chars: $("#cChunk", body).value, ingest_max_chunks: $("#cMaxChunk", body).value, use_proxy: $("#cUseProxy", body).checked, proxy_url: $("#cProxyUrl", body).value,
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
    $$("[data-preset]", body).forEach((b) => (b.onclick = () => { prov.value = "openai"; syncProv(); $("#cUrl", body).value = b.dataset.preset; $("#cList", body).click(); }));
    $("#cList", body).onclick = async () => {
      const out = $("#cListRes", body); out.innerHTML = '<span class="spin"></span> 取得中…';
      try {
        const r = await api.post("/api/llm/models", { provider: prov.value, base_url: $("#cUrl", body).value, api_key: $("#cKey", body).value });
        $("#dlModels", body).innerHTML = r.models.map((x) => `<option value="${esc(x)}">`).join("");
        out.innerHTML = `${r.models.length} 件: ${r.models.slice(0, 12).map((x) => `<a href="#" data-mdl="${esc(x)}">${esc(x)}</a>`).join(" ・ ")}${r.models.length > 12 ? " …" : ""}<br>クリックでチャット用、Shift+クリックで Embed 用に設定します。`;
        $$("[data-mdl]", out).forEach((a) => (a.onclick = (e) => { e.preventDefault(); $(e.shiftKey ? "#cEmb" : "#cModel", body).value = a.dataset.mdl; }));
        if (!$("#cModel", body).value && r.models.length) $("#cModel", body).value = r.models.find((x) => !/embed|bge|e5/i.test(x)) || r.models[0];
      } catch (e) { out.innerHTML = `<span class="ng">${esc(e.message)}</span>`; }
    };
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
  $("#btnScope").onclick = () => openScope();
  $("#btnIngest").onclick = () => openIngest();

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
    if (S.state.index) await applyIndexStatus(S.state.index);
    setTimeout(poll, 1000);
    const hash = decodeURIComponent(location.hash.slice(1));
    const start = (hash && S.tree.some((n) => n.path === hash) && hash) || (store.get("last") && S.tree.some((n) => n.path === store.get("last")) && store.get("last")) || resolve("ホーム") || (S.tree[0] && S.tree[0].path);
    if (start) openNote(start);
  })();
})();
