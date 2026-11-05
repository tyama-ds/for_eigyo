/* Doc Vault — フロントエンド（依存なしの素の JS） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = {
  docs: [], folders: [], tags: [], status: null, config: null,
  chats: [], chat: null, messages: [], sending: false,
  folderFilter: "", tagFilter: "", viewerDoc: null,
};
const ROUTE_LABEL = { fact: "事実の確認", relation: "関係・経緯", summary: "要約・全体像",
  compare: "比較", chat: "雑談・一般" };
const STATUS_LABEL = { queued: "待機中", processing: "処理中", ready: "検索可", error: "エラー" };

// ---------------------------------------------------------------- utils

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: `HTTP ${res.status}` }; }
  if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtSize(n) {
  if (n > 1048576) return `${(n / 1048576).toFixed(1)}MB`;
  if (n > 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

function sensBadge(s) {
  return s === "public" ? '<span class="badge public">公開</span>'
    : '<span class="badge internal">社外秘</span>';
}

/* 安全な Markdown 風レンダリング（入力は必ずエスケープしてから整形） */
function renderMd(text) {
  const lines = esc(text || "").split("\n");
  const out = [];
  let list = null, inCode = false, table = [];
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r));
    out.push("<table>" + rows.map((r, i) => "<tr>" + r.replace(/^\||\|$/g, "").split("|")
      .map((c) => i === 0 ? `<th>${c.trim()}</th>` : `<td>${c.trim()}</td>`).join("") + "</tr>").join("") + "</table>");
    table = [];
  };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      flushList(); flushTable();
      out.push(inCode ? "</pre>" : "<pre>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(line + "\n"); continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) { flushList(); table.push(line.trim()); continue; }
    flushTable();
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { flushList(); out.push(`<h${h[1].length <= 2 ? 3 : 4}>${h[2]}</h${h[1].length <= 2 ? 3 : 4}>`); continue; }
    const ul = line.match(/^\s*[-*・●]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)．]\s+(.*)/);
    if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { flushList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${(ul || ol)[1]}</li>`);
      continue;
    }
    flushList();
    if (line.trim()) out.push(`<p>${line}</p>`);
  }
  flushList(); flushTable();
  if (inCode) out.push("</pre>");
  return out.join("")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[((?:S|D|C)\d+)\]/g, '<span class="cite" data-ref="$1">$1</span>');
}

// ---------------------------------------------------------------- tabs / status

function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  if (name === "docs") loadDocs();
  if (name === "graph") loadGraph();
  if (name === "settings") loadSettings();
  if (name === "chat") refreshChatHead();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

async function pollStatus() {
  const st = await api("/api/status");
  if (st.error) return;
  state.status = st;
  const prof = (n, label) => {
    const p = st.profiles[n];
    const cls = p.chat ? "ok" : "ng";
    return `<span title="${esc(p.provider)} / ${esc(p.model || "未設定")}${p.embed ? "・埋め込みあり" : ""}">`
      + `<span class="dot ${cls}"></span>${label}${p.chat ? "" : "（未設定）"}</span>`;
  };
  const busy = (st.indexer.current ? 1 : 0) + st.indexer.pending.length;
  $("#conn-status").innerHTML = prof("local", "ローカル") + prof("cloud", "クラウド")
    + `<span><span class="dot ${busy ? "busy" : "ok"}"></span>${busy ? `取り込み中 ${busy} 件` : `文書 ${st.docs} 件`}</span>`;
  if (busy && !$("#tab-docs").classList.contains("hidden")) loadDocs(true);
  if (state._wasBusy && !busy) { loadDocs(true); renderExamples(); }
  state._wasBusy = !!busy;
}

// ---------------------------------------------------------------- 相談

async function loadChats() {
  const r = await api("/api/chats");
  state.chats = r.chats || [];
  renderChatList();
}

function renderChatList() {
  $("#chat-items").innerHTML = state.chats.map((c) => `
    <div class="chat-item ${state.chat && state.chat.id === c.id ? "active" : ""}" data-id="${c.id}">
      <span class="t" title="${esc(c.title)}">${esc(c.title || "新しい相談")}</span>
      ${c.touched_internal ? '<span class="badge internal" title="社外秘資料を参照済み">秘</span>' : ""}
      <span class="x" data-del="${c.id}" title="削除">×</span>
    </div>`).join("") || '<p class="hint">まだ相談はありません。</p>';
  $$("#chat-items .chat-item").forEach((el) => el.addEventListener("click", (ev) => {
    if (ev.target.dataset.del) return;
    openChat(el.dataset.id);
  }));
  $$("#chat-items [data-del]").forEach((el) => el.addEventListener("click", async () => {
    if (!confirm("この相談を削除しますか？")) return;
    await api(`/api/chats/${el.dataset.del}/delete`, {});
    if (state.chat && state.chat.id === el.dataset.del) { state.chat = null; state.messages = []; renderMessages(); }
    loadChats();
  }));
}

async function newChat(scope) {
  const r = await api("/api/chats", { scope: scope || {} });
  state.chat = r.chat; state.messages = [];
  await loadChats();
  refreshChatHead(); renderMessages();
  $("#q").focus();
  return r.chat;
}

async function openChat(id) {
  const r = await api(`/api/chats/${id}`);
  if (r.error) return;
  state.chat = r.chat; state.messages = r.messages;
  renderChatList(); refreshChatHead(); renderMessages();
}

function refreshChatHead() {
  const c = state.chat;
  const folders = [{ path: "", count: state.docs.length }, ...state.folders.filter((f) => f.path)];
  $("#chat-folder").innerHTML = folders.map((f) =>
    `<option value="${esc(f.path)}">${f.path ? esc(f.path) : "すべての文書"}</option>`).join("");
  $("#chat-title").value = c ? c.title : "";
  $("#chat-mode").value = c ? c.mode : ((state.config && state.config.policy.chat_mode) || "auto");
  $("#chat-folder").value = c ? (c.scope.folder || "") : "";
  const ids = c ? (c.scope.doc_ids || []) : [];
  $("#chat-scope-docs").innerHTML = ids.map((id) => {
    const d = state.docs.find((x) => x.id === id);
    return `<span class="chip">📄 ${esc(d ? d.title : id)}<span class="x" data-rm="${esc(id)}">×</span></span>`;
  }).join("");
  $$("#chat-scope-docs [data-rm]").forEach((el) => el.addEventListener("click", () => {
    const scope = { ...state.chat.scope, doc_ids: state.chat.scope.doc_ids.filter((x) => x !== el.dataset.rm) };
    updateChat({ scope });
  }));
}

async function updateChat(fields) {
  if (!state.chat) await newChat();
  const r = await api(`/api/chats/${state.chat.id}/update`, fields);
  if (r.chat) { state.chat = r.chat; refreshChatHead(); loadChats(); }
}

$("#chat-mode").addEventListener("change", () => updateChat({ mode: $("#chat-mode").value }));
$("#chat-folder").addEventListener("change", () =>
  updateChat({ scope: { ...(state.chat ? state.chat.scope : {}), folder: $("#chat-folder").value } }));
$("#chat-title").addEventListener("change", () => updateChat({ title: $("#chat-title").value }));
$("#chat-new").addEventListener("click", () => newChat());

function renderExamples() {
  const ex = ["取り込んだ資料の全体像を要約して"];
  const docs = state.docs.filter((d) => d.status === "ready");
  if (docs[0]) ex.push(`「${docs[0].title.slice(0, 24)}」の要点は？`);
  const kws = docs.flatMap((d) => d.keywords || []).filter((k, i, a) => a.indexOf(k) === i);
  if (kws[0]) ex.push(`${kws[0]}について教えて`);
  if (kws[1] && kws[2]) ex.push(`${kws[1]}と${kws[2]}の関係は？`);
  $("#examples").innerHTML = docs.length
    ? ex.map((q) => `<span class="example">${esc(q)}</span>`).join("")
    : '<p>まず「文書」タブで文書を取り込んでください（サンプルも用意しています）。</p>';
  $$("#examples .example").forEach((el) => el.addEventListener("click", () => {
    $("#q").value = el.textContent; send();
  }));
}

function renderMessages() {
  const box = $("#messages");
  if (!state.messages.length) {
    box.innerHTML = "";
    box.appendChild(emptyChat());
    renderExamples();
    return;
  }
  box.innerHTML = state.messages.map(messageHtml).join("");
  bindMessageEvents(box);
  box.scrollTop = box.scrollHeight;
}

function emptyChat() {
  const div = document.createElement("div");
  div.className = "empty-chat"; div.id = "empty-chat";
  div.innerHTML = `<h2>文書について質問できます</h2>
    <p>取り込んだ文書を検索して、出典付きで回答します。質問の種類（事実・関係・要約・比較）に応じて検索方法を自動で選びます。</p>
    <div class="examples" id="examples"></div>`;
  return div;
}

function messageHtml(m, idx) {
  if (m.role === "user") {
    return `<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>`;
  }
  const meta = m.meta || {};
  const route = meta.route || {};
  const badges = [];
  if (route.type) badges.push(`<span class="badge">${ROUTE_LABEL[route.type] || route.type}</span>`);
  if (meta.profile) badges.push(`<span class="badge ${meta.profile}">${meta.profile === "local" ? "ローカル LLM" : "クラウド LLM"}</span>`);
  else if (route.type) badges.push('<span class="badge ng">LLM なし（抜粋）</span>');
  if (route.retrieval) badges.push(meta.sensitivity === "internal"
    ? '<span class="badge internal">社外秘資料を含む</span>' : '<span class="badge public">公開資料のみ</span>');
  if (meta.elapsed) badges.push(`<span class="muted">${meta.elapsed}秒</span>`);
  const sources = (meta.sources || []).map((s) => sourceHtml(s, idx)).join("");
  const trace = [];
  if (route.standalone && route.standalone !== (state.messages[idx - 1] || {}).content) {
    trace.push(`検索した質問: ${esc(route.standalone)}`);
  }
  if ((route.keywords || []).length) trace.push(`キーワード: ${esc(route.keywords.join("、"))}`);
  if ((meta.via || []).length) trace.push(`検索方法: ${esc(meta.via.join(" → "))}`);
  for (const s of meta.steps || []) trace.push(`${esc(s.t)}秒 ${esc(s.name)}: ${esc(s.detail)}`);
  if (meta.llm) trace.push(`LLM 呼び出し: ローカル ${meta.llm.calls.local} 回 / クラウド ${meta.llm.calls.cloud} 回`);
  const warns = (meta.warnings || []).map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("");
  return `<div class="msg assistant" data-idx="${idx}"><div class="bubble">
    <div class="msg-meta">${badges.join("")}</div>
    <div class="answer">${renderMd(m.content)}</div>
    ${warns}
    ${sources ? `<div class="sources">${sources}</div>` : ""}
    ${trace.length ? `<details class="trace"><summary>検索の流れ</summary>${trace.map((t) => `<div>${t}</div>`).join("")}</details>` : ""}
  </div></div>`;
}

function sourceHtml(s, idx) {
  const icon = s.kind === "community" ? "🧩" : s.kind === "doc" ? "📑" : "📄";
  return `<div class="source ${s.used === false ? "unused" : ""}" data-idx="${idx}" data-ref="${esc(s.ref)}"
      title="${s.used === false ? "回答では引用されなかった候補" : "回答で引用"}">
    <div><span class="ref">${esc(s.ref)}</span>${icon} ${esc(s.doc_title)} <span class="loc">${esc(s.locator || "")}</span>
    ${s.sensitivity === "public" ? "" : '<span class="badge internal">秘</span>'}</div>
    <div class="snip">${esc(s.snippet || "")}</div></div>`;
}

function bindMessageEvents(root) {
  $$(".cite", root).forEach((el) => el.addEventListener("click", () => {
    const msgEl = el.closest(".msg");
    openSourceRef(Number(msgEl.dataset.idx), el.dataset.ref);
  }));
  $$(".source", root).forEach((el) => el.addEventListener("click", () =>
    openSourceRef(Number(el.dataset.idx), el.dataset.ref)));
}

function openSourceRef(idx, ref) {
  const m = state.messages[idx];
  const s = m && m.meta && (m.meta.sources || []).find((x) => x.ref === ref);
  if (s) openSource(s);
}

async function send() {
  const question = $("#q").value.trim();
  if (!question || state.sending) return;
  if (!state.chat) await newChat();
  state.sending = true;
  $("#send").disabled = true;
  $("#q").value = "";
  $("#chat-hint").textContent = "";
  state.messages.push({ role: "user", content: question });
  const pending = { role: "assistant", content: "", meta: {} };
  state.messages.push(pending);
  renderMessages();
  const box = $("#messages");
  const bubble = box.lastElementChild.querySelector(".bubble");
  const answerEl = bubble.querySelector(".answer");
  const metaEl = bubble.querySelector(".msg-meta");
  metaEl.innerHTML = '<span class="status-line">質問を送信しています…</span>';
  let raw = "";
  let lastRender = 0;
  const handle = (ev) => {
    if (ev.type === "status") {
      metaEl.innerHTML = `<span class="status-line">${esc(ev.text)}</span>`;
    } else if (ev.type === "route") {
      pending.meta.route = ev.route;
    } else if (ev.type === "sources") {
      pending.meta.sources = ev.sources;
    } else if (ev.type === "delta") {
      raw += ev.text;
      const now = Date.now();
      if (now - lastRender > 60) {
        answerEl.innerHTML = renderMd(raw);
        box.scrollTop = box.scrollHeight;
        lastRender = now;
      }
    } else if (ev.type === "done") {
      state.messages[state.messages.length - 1] = ev.message;
    } else if (ev.type === "error") {
      $("#chat-hint").textContent = `エラー: ${ev.message}`;
    }
  };
  try {
    const res = await fetch(`/api/chats/${state.chat.id}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        if (block.startsWith("data: ")) {
          try { handle(JSON.parse(block.slice(6))); } catch (e) { /* 壊れた行は無視 */ }
        }
      }
    }
  } catch (e) {
    $("#chat-hint").textContent = `送信に失敗しました: ${e.message}`;
    if (!raw) state.messages.pop();
  } finally {
    state.sending = false;
    $("#send").disabled = false;
    renderMessages();
    loadChats();
    const r = await api(`/api/chats/${state.chat.id}`);
    if (r.chat) { state.chat = r.chat; refreshChatHead(); }
  }
}

$("#send").addEventListener("click", send);
$("#q").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); }
});

// ---------------------------------------------------------------- 出典ビューア

async function openSource(s) {
  if (s.kind === "community") {
    showViewer(s.doc_title, "コミュニティ要約（複数文書から抽出したテーマ）", null);
    $("#viewer-body").innerHTML = `<div class="doc-summary">${esc(s.snippet)}</div>`;
    return;
  }
  openDoc(s.doc_id, { chunkId: s.chunk_id, page: s.page, summary: s.kind === "doc" });
}

function showViewer(title, meta, doc) {
  $("#viewer").classList.remove("hidden");
  $("#viewer-title").textContent = title;
  $("#viewer-meta").innerHTML = meta;
  state.viewerDoc = doc;
  $("#viewer-download").classList.toggle("hidden", !doc);
  $("#viewer-ask").classList.toggle("hidden", !doc);
  if (doc) $("#viewer-download").href = `/api/docs/${doc.id}/file?download=1`;
}

async function openDoc(docId, opt = {}) {
  const r = await api(`/api/docs/${docId}/chunks`);
  if (r.error) { alert(r.error); return; }
  const d = r.doc;
  const meta = `${sensBadge(d.sensitivity)} ${esc(d.filename)} ・ ${esc(d.folder || "（フォルダなし）")}`
    + (d.pages ? ` ・ ${d.pages}${d.ext === ".pptx" ? "枚" : "ページ"}` : "");
  showViewer(d.title, meta, d);
  const body = $("#viewer-body");
  const isPdf = d.ext === ".pdf";
  const textView = () => {
    body.innerHTML = (d.summary ? `<div class="doc-summary"><b>要約</b><br>${esc(d.summary)}
        ${(d.keywords || []).length ? `<div class="muted">キーワード: ${esc(d.keywords.join("、"))}</div>` : ""}</div>` : "")
      + r.chunks.map((c) => `<div class="chunk ${c.id === opt.chunkId ? "hl" : ""}" id="ck-${esc(c.id)}">`
        + `<span class="loc">${esc(c.locator)}${c.heading ? " ・ " + esc(c.heading) : ""}</span>${esc(c.text)}</div>`).join("");
    const hl = opt.chunkId && document.getElementById(`ck-${opt.chunkId}`);
    if (hl) hl.scrollIntoView({ block: "center" });
  };
  if (isPdf && !opt.summary) {
    const page = opt.page || 1;
    body.innerHTML = `<div class="row gap-s" style="padding:6px 12px"><button class="btn small" id="v-text">抽出テキストで表示</button>
      <span class="muted">PDF の ${page} ページを表示しています</span></div>
      <iframe src="/api/docs/${d.id}/file#page=${page}" title="PDF"></iframe>`;
    body.querySelector("iframe").style.height = "calc(100% - 40px)";
    $("#v-text").addEventListener("click", textView);
  } else {
    textView();
  }
}

$("#viewer-close").addEventListener("click", () => $("#viewer").classList.add("hidden"));
$("#viewer-ask").addEventListener("click", async () => {
  const d = state.viewerDoc;
  if (!d) return;
  $("#viewer").classList.add("hidden");
  showTab("chat");
  await newChat({ doc_ids: [d.id] });
  $("#q").placeholder = `「${d.title}」について質問（Enter で送信）`;
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { $("#viewer").classList.add("hidden"); $("#doc-modal").classList.add("hidden"); }
});

// ---------------------------------------------------------------- 文書

async function loadDocs(quiet) {
  const r = await api("/api/docs");
  if (r.error) return;
  state.docs = r.docs; state.folders = r.folders; state.tags = r.tags;
  renderFolders(); renderDocTable();
  if (!quiet) refreshChatHead();
}

function renderFolders() {
  const items = state.folders.map((f) => {
    const depth = f.path ? f.path.split("/").length : 0;
    const name = f.path ? f.path.split("/").pop() : "すべての文書";
    return `<div class="folder ${state.folderFilter === f.path ? "active" : ""}" data-path="${esc(f.path)}"
      style="padding-left:${8 + depth * 14}px"><span>${f.path ? "📁 " : ""}${esc(name)}</span><span class="cnt">${f.count}</span></div>`;
  });
  $("#folder-tree").innerHTML = items.join("") || '<div class="folder active" data-path="">すべての文書</div>';
  $$("#folder-tree .folder").forEach((el) => el.addEventListener("click", () => {
    state.folderFilter = el.dataset.path; renderFolders(); renderDocTable();
    $("#up-folder").value = el.dataset.path;
  }));
  $("#tag-list").innerHTML = state.tags.map((t) =>
    `<span class="badge tag ${state.tagFilter === t ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`).join("")
    || '<span class="muted">なし</span>';
  $$("#tag-list .tag").forEach((el) => el.addEventListener("click", () => {
    state.tagFilter = state.tagFilter === el.dataset.tag ? "" : el.dataset.tag;
    renderFolders(); renderDocTable();
  }));
}

function flagHtml(d) {
  const f = d.flags || {};
  const item = (on, label, title) => `<span class="flag ${on ? "on" : ""}" title="${esc(title)}">${label}</span>`;
  return `<div class="flags">${item(f.bm25, "全文", "全文検索（BM25）")}${item(f.vector, "埋込", f.vector ? `埋め込み: ${f.vector}` : "埋め込みなし")}`
    + `${item(f.graph, "グラフ", f.graph ? `ナレッジグラフ（${f.graph === "local" ? "ローカル" : "クラウド"} LLM で抽出）` : "グラフなし")}`
    + `${item(f.summary, "要約", f.summary ? "文書要約あり" : "要約なし")}</div>`;
}

function renderDocTable() {
  const docs = state.docs.filter((d) =>
    (!state.folderFilter || d.folder === state.folderFilter || d.folder.startsWith(state.folderFilter + "/"))
    && (!state.tagFilter || d.tags.includes(state.tagFilter)));
  $("#docs-heading").innerHTML = `文書 <span class="muted">${docs.length} 件${state.folderFilter ? ` ・ ${esc(state.folderFilter)}` : ""}</span>`;
  if (!docs.length) {
    $("#doc-table").innerHTML = '<p class="hint">文書がありません。上の枠にファイルをドロップするか、サンプルを読み込んでください。</p>';
    return;
  }
  $("#doc-table").innerHTML = `<table class="doc-table"><thead><tr>
      <th>タイトル</th><th>区分</th><th>状態</th><th class="hide-sm">インデックス</th><th class="hide-sm">更新</th><th></th>
    </tr></thead><tbody>${docs.map((d) => {
      const ext = d.ext.replace(".", "");
      let st = `<span class="badge ${d.status === "ready" ? "ok" : d.status === "error" ? "ng" : "busy"}">${STATUS_LABEL[d.status] || d.status}</span>`;
      if (d.stage) st += `<div class="doc-sub">${esc(d.stage)}</div><div class="progress"><div style="width:${Math.round((d.progress || 0) * 100)}%"></div></div>`;
      if (d.error) st += `<div class="doc-sub" style="color:var(--ng)">${esc(d.error)}</div>`;
      if ((d.warnings || []).length) st += `<div class="doc-sub" title="${esc(d.warnings.join("\n"))}">⚠ 警告 ${d.warnings.length} 件</div>`;
      return `<tr>
        <td><span class="ext ${ext}">${esc(ext.toUpperCase())}</span><span class="doc-title" data-open="${d.id}">${esc(d.title)}</span>
          <div class="doc-sub">${esc(d.folder || "（フォルダなし）")} ・ ${esc(d.filename)} ・ ${fmtSize(d.size)}${d.pages ? ` ・ ${d.pages}${ext === "pptx" ? "枚" : "p"}` : ""}
          ${(d.tags || []).map((t) => `<span class="badge">${esc(t)}</span>`).join(" ")}</div></td>
        <td>${sensBadge(d.sensitivity)}</td>
        <td>${st}</td>
        <td class="hide-sm">${flagHtml(d)}</td>
        <td class="hide-sm doc-sub">${fmtDate(d.updated_at)}</td>
        <td class="actions"><button class="btn small" data-ask="${d.id}">質問</button>
          <button class="btn small" data-edit="${d.id}">編集</button></td>
      </tr>`;
    }).join("")}</tbody></table>`;
  $$("#doc-table [data-open]").forEach((el) => el.addEventListener("click", () => openDoc(el.dataset.open)));
  $$("#doc-table [data-edit]").forEach((el) => el.addEventListener("click", () => editDoc(el.dataset.edit)));
  $$("#doc-table [data-ask]").forEach((el) => el.addEventListener("click", async () => {
    const d = state.docs.find((x) => x.id === el.dataset.ask);
    showTab("chat");
    await newChat({ doc_ids: [el.dataset.ask] });
    $("#q").placeholder = `「${d ? d.title : ""}」について質問（Enter で送信）`;
  }));
}

async function uploadFiles(files) {
  const log = $("#upload-log");
  const folder = $("#up-folder").value.trim();
  const sens = $("#up-sens").value;
  const tags = $("#up-tags").value.trim();
  for (const file of files) {
    const line = document.createElement("div");
    line.textContent = `⏫ ${file.name} を送信中…`;
    log.prepend(line);
    const qs = new URLSearchParams({ name: file.name, folder, sensitivity: sens, tags });
    try {
      const res = await fetch(`/api/docs/upload?${qs}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
      });
      const r = await res.json();
      line.textContent = r.error ? `❌ ${file.name}: ${r.error}`
        : r.created ? `✅ ${file.name}: 登録しました（取り込み処理を開始）`
          : `ℹ ${file.name}: 同じ内容の文書が登録済みです（${r.doc.title}）`;
    } catch (e) {
      line.textContent = `❌ ${file.name}: ${e.message}`;
    }
  }
  loadDocs(); pollStatus();
}

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (ev) => { ev.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (ev) => { ev.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (ev) => uploadFiles([...ev.dataTransfer.files]));
$("#file-input").addEventListener("change", (ev) => { uploadFiles([...ev.target.files]); ev.target.value = ""; });
$("#load-samples").addEventListener("click", async () => {
  const r = await api("/api/samples", {});
  $("#upload-log").innerHTML = (r.docs || []).map((d) =>
    `<div>${d.created ? "✅" : "ℹ"} ${esc(d.title)}${d.created ? "" : "（登録済み）"}</div>`).join("");
  loadDocs(); pollStatus();
});

async function docSearch() {
  const q = $("#doc-search").value.trim();
  if (!q) { $("#search-results").innerHTML = ""; return; }
  $("#search-results").innerHTML = '<p class="hint">検索中…</p>';
  const r = await api("/api/search", { q, folder: state.folderFilter });
  $("#search-results").innerHTML = (r.results || []).map((g) => `<div class="search-hit">
      <span class="doc-title" data-open="${g.doc_id}">${esc(g.title)}</span> ${sensBadge(g.sensitivity)}
      ${g.hits.map((h) => `<div class="hit" data-open="${g.doc_id}" data-chunk="${esc(h.chunk_id)}" data-page="${h.page || ""}">
        <b>${esc(h.locator)}</b> ${esc(h.snippet)}</div>`).join("")}</div>`).join("")
    + ((r.results || []).length ? "" : '<p class="hint">一致する文書はありません。</p>')
    + `<p class="hint"><a href="#" id="search-clear">検索結果を閉じる</a></p>`;
  $$("#search-results [data-open]").forEach((el) => el.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openDoc(el.dataset.open, { chunkId: el.dataset.chunk, page: Number(el.dataset.page) || null });
  }));
  $("#search-clear").addEventListener("click", (ev) => { ev.preventDefault(); $("#search-results").innerHTML = ""; });
}
$("#doc-search-btn").addEventListener("click", docSearch);
$("#doc-search").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.isComposing) docSearch(); });

let editing = null;
function editDoc(id) {
  const d = state.docs.find((x) => x.id === id);
  if (!d) return;
  editing = d;
  $("#doc-modal-body").innerHTML = `
    <label class="field-label">タイトル</label><input type="text" id="dm-title" value="${esc(d.title)}">
    <div class="two">
      <div><label class="field-label">フォルダ（/ で階層）</label><input type="text" id="dm-folder" value="${esc(d.folder)}"></div>
      <div><label class="field-label">タグ（カンマ区切り）</label><input type="text" id="dm-tags" value="${esc((d.tags || []).join(", "))}"></div>
    </div>
    <label class="field-label">区分</label>
    <select id="dm-sens"><option value="internal">社外秘（ローカル LLM のみで処理）</option>
      <option value="public">公開（クラウド LLM でも処理可）</option></select>
    <div class="muted top-space">${esc(d.filename)} ・ ${fmtSize(d.size)} ・ ${d.chars.toLocaleString()} 文字 ・ 登録 ${fmtDate(d.created_at)}</div>
    <div class="top-space">${flagHtml(d)}</div>
    ${d.summary ? `<label class="field-label">要約</label><div class="doc-summary">${esc(d.summary)}</div>` : ""}
    ${(d.warnings || []).length ? `<label class="field-label">警告</label>${d.warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}` : ""}
    ${d.error ? `<div class="warn-line" style="color:var(--ng)">❌ ${esc(d.error)}</div>` : ""}`;
  $("#dm-sens").value = d.sensitivity;
  $("#doc-modal").classList.remove("hidden");
}
$("#dm-close").addEventListener("click", () => $("#doc-modal").classList.add("hidden"));
$("#dm-save").addEventListener("click", async () => {
  const r = await api(`/api/docs/${editing.id}/update`, {
    title: $("#dm-title").value, folder: $("#dm-folder").value,
    tags: $("#dm-tags").value.split(/[,、]/).map((t) => t.trim()).filter(Boolean),
    sensitivity: $("#dm-sens").value,
  });
  if (r.error) { alert(r.error); return; }
  $("#doc-modal").classList.add("hidden");
  loadDocs();
});
$("#dm-reindex").addEventListener("click", async () => {
  await api(`/api/docs/${editing.id}/reindex`, {});
  $("#doc-modal").classList.add("hidden");
  loadDocs(); pollStatus();
});
$("#dm-delete").addEventListener("click", async () => {
  if (!confirm(`「${editing.title}」を削除しますか？（原本と索引を削除します）`)) return;
  await api(`/api/docs/${editing.id}/delete`, {});
  $("#doc-modal").classList.add("hidden");
  loadDocs();
});

// ---------------------------------------------------------------- グラフ

let gview = null;
let gdata = null;
function graphView() {
  if (!gview) {
    gview = new GraphView($("#g-canvas"), {
      tooltip: $("#g-tooltip"), height: 620, onSelect: (n) => renderGraphDetail(n),
    });
  }
  return gview;
}

async function loadGraph() {
  if (!state.docs.length) await loadDocs(true);
  const sel = $("#g-doc");
  const cur = sel.value;
  sel.innerHTML = '<option value="">すべての文書</option>' + state.docs
    .filter((d) => d.flags && d.flags.graph)
    .map((d) => `<option value="${d.id}">${esc(d.title.slice(0, 40))}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";
  const gv = graphView();
  gv.resize();
  $("#g-hint").textContent = "読み込み中…";
  const data = await api(`/api/graph?limit=${$("#g-limit").value}&doc=${encodeURIComponent(sel.value)}`);
  renderGraphDetail(null);
  if (data.error) {
    $("#g-hint").textContent = data.error;
    $("#g-legend").innerHTML = "";
    gv.emptyMessage = data.error.slice(0, 60);
    gdata = null;
    gv.setData({ nodes: [], edges: [], communities: [] });
    return;
  }
  gdata = data;
  gv.emptyMessage = "";
  gv.setShowIsolated($("#g-isolated").checked);
  gv.setData(data);
  updateGraphHint(); renderLegend();
}

function updateGraphHint() {
  if (!gdata) return;
  const st = graphView().stats();
  let note = `エンティティ ${gdata.total_nodes} / 関係 ${gdata.total_edges} ・ 表示 ${st.shown} ノード`;
  if (gdata.truncated) note += "（関係の多い順に上限まで）";
  if (st.isolated && !$("#g-isolated").checked) note += ` ・ 孤立ノード ${st.isolated} 件は非表示`;
  $("#g-hint").textContent = note + "。ホイールで拡大縮小、背景ドラッグで移動、クリックで詳細。";
}

function renderLegend() {
  const items = graphView().legend();
  const byType = $("#g-color").value === "type";
  $("#g-legend").innerHTML = `<div class="muted">凡例（${byType ? "種別" : "コミュニティ"}）</div>`
    + items.map((it) => `<div class="legend-row" data-key="${esc(it.key)}">
      <span class="sw" style="background:${it.color}"></span>${esc(it.label.slice(0, 30))}<span class="cnt">${it.count}</span></div>`).join("");
  if (!byType) $$("#g-legend [data-key]").forEach((el) =>
    el.addEventListener("click", () => graphView().highlightCommunity(el.dataset.key)));
}

function renderGraphDetail(node) {
  const box = $("#g-detail");
  if (!node) { box.innerHTML = '<p class="hint">ノードをクリックすると、説明・関係・出典文書が表示されます。</p>'; return; }
  const rels = graphView().neighbors(node.id);
  const docs = (node.doc_ids || []).map((id, i) =>
    `<li><a href="#" data-doc="${esc(id)}">${esc((node.docs || [])[i] || id)}</a></li>`).join("");
  box.innerHTML = `<div class="detail-title">${esc(node.name)}</div>
    <div class="muted">${esc(node.type || "")}${node.community ? " ・ " + esc(node.community) : ""} ・ 関係 ${rels.length}</div>
    <p>${esc(node.description || "（説明なし）")}</p>
    ${docs ? `<div class="muted">出典文書</div><ul>${docs}</ul>` : ""}
    <button class="btn small" id="g-ask">このエンティティについて質問</button>
    <div class="muted top-space">関係（強い順）</div>
    ${rels.slice(0, 40).map((r) => `<div class="detail-rel" data-id="${esc(r.node.id)}">→ <b>${esc(r.node.name)}</b>
      <span class="muted">強さ${r.edge.strength ?? ""}</span><br><span class="muted">${esc((r.edge.description || "").slice(0, 120))}</span></div>`).join("")
      || '<p class="hint">関係はありません。</p>'}`;
  $$("[data-id]", box).forEach((el) => el.addEventListener("click", () => graphView().focus(el.dataset.id)));
  $$("[data-doc]", box).forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); openDoc(el.dataset.doc); }));
  $("#g-ask").addEventListener("click", async () => {
    showTab("chat");
    await newChat();
    $("#q").value = `${node.name}について、関係する人・組織・経緯も含めて教えて`;
    $("#q").focus();
  });
}

$("#g-reload").addEventListener("click", loadGraph);
$("#g-doc").addEventListener("change", loadGraph);
$("#g-limit").addEventListener("change", loadGraph);
$("#g-isolated").addEventListener("change", () => {
  graphView().setShowIsolated($("#g-isolated").checked); updateGraphHint(); renderLegend();
});
$("#g-color").addEventListener("change", () => { graphView().setColorMode($("#g-color").value); renderLegend(); });
$("#g-fit").addEventListener("click", () => graphView().fit());
$("#g-zoom-in").addEventListener("click", () => graphView().zoomBy(1.3));
$("#g-zoom-out").addEventListener("click", () => graphView().zoomBy(1 / 1.3));
$("#g-search").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" || ev.isComposing) return;
  const hits = graphView().search($("#g-search").value);
  if (hits.length) graphView().focus(hits[0].id);
  if ($("#g-search").value) $("#g-hint").textContent = `「${$("#g-search").value}」に一致: ${hits.length} 件`;
  else updateGraphHint();
});

// ---------------------------------------------------------------- 設定

const PRESETS = {
  local: [
    { label: "Ollama", v: { provider: "openai", base_url: "http://127.0.0.1:11434/v1", embed_provider: "openai", embed_base_url: "" } },
    { label: "LM Studio", v: { provider: "openai", base_url: "http://127.0.0.1:1234/v1", embed_provider: "openai", embed_base_url: "" } },
    { label: "vLLM / llama.cpp", v: { provider: "openai", base_url: "http://127.0.0.1:8000/v1", embed_provider: "openai", embed_base_url: "" } },
    { label: "社内 Azure OpenAI", v: { provider: "azure", base_url: "https://<リソース名>.openai.azure.com", embed_provider: "azure" } },
  ],
  cloud: [
    { label: "Claude", v: { provider: "anthropic", base_url: "", model: "claude-opus-5-5", embed_provider: "openai" } },
    { label: "OpenAI", v: { provider: "openai", base_url: "https://api.openai.com/v1", embed_provider: "openai", embed_base_url: "", embed_model: "text-embedding-3-small" } },
    { label: "Gemini", v: { provider: "openai", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", embed_provider: "openai", embed_base_url: "" } },
    { label: "Azure OpenAI", v: { provider: "azure", base_url: "https://<リソース名>.openai.azure.com", embed_provider: "azure" } },
  ],
};

function profileForm(name, p) {
  const key = (f) => `pf-${name}-${f}`;
  return `
    <div class="presets">${PRESETS[name].map((ps, i) => `<button class="btn small" data-preset="${name}:${i}">${esc(ps.label)}</button>`).join("")}</div>
    <label class="field-label">種類</label>
    <select id="${key("provider")}">
      <option value="openai">OpenAI 互換 API（Ollama / LM Studio / vLLM / OpenAI / Gemini 等）</option>
      <option value="azure">Azure OpenAI</option>
      <option value="anthropic">Anthropic Claude（pip install anthropic）</option>
    </select>
    <label class="field-label">Base URL <span class="muted" data-hint="${name}-base"></span></label>
    <input type="text" id="${key("base_url")}">
    <div class="two">
      <div><label class="field-label">API Key</label><input type="password" id="${key("api_key")}" autocomplete="off"
        placeholder="${p.has_api_key ? "（設定済み。変更時のみ入力）" : "（不要なら空）"}"></div>
      <div><label class="field-label" data-label="${name}-model">モデル</label><input type="text" id="${key("model")}" placeholder="例: qwen3:14b / claude-opus-5-5"></div>
    </div>
    <div class="two">
      <div><label class="field-label">取り込み用モデル（任意）</label><input type="text" id="${key("ingest_model")}" placeholder="空ならモデルと同じ"></div>
      <div data-only="${name}-azure"><label class="field-label">API バージョン（Azure）</label><input type="text" id="${key("api_version")}"></div>
      <div data-only="${name}-anthropic"><label class="field-label">effort（Claude・任意）</label>
        <select id="${key("effort")}"><option value="">既定</option><option>low</option><option>medium</option><option>high</option></select></div>
    </div>
    <h3 class="muted">埋め込み（意味検索）</h3>
    <p class="hint" data-only="${name}-anthropic">Claude には埋め込み API が無いため、OpenAI 互換の埋め込み API を指定するか、空欄のまま（全文検索＋グラフで検索）にします。</p>
    <div class="two">
      <div><label class="field-label">埋め込みモデル</label><input type="text" id="${key("embed_model")}" placeholder="例: bge-m3（日本語に強い） / 空なら使わない"></div>
      <div><label class="field-label">埋め込みの種類</label><select id="${key("embed_provider")}"><option value="openai">OpenAI 互換</option><option value="azure">Azure OpenAI</option></select></div>
    </div>
    <div class="two">
      <div><label class="field-label">埋め込み Base URL（空なら上と同じ）</label><input type="text" id="${key("embed_base_url")}"></div>
      <div><label class="field-label">埋め込み API Key（空なら上と同じ）</label><input type="password" id="${key("embed_api_key")}" autocomplete="off"
        placeholder="${p.has_embed_api_key ? "（設定済み）" : ""}"></div>
    </div>
    <details class="adv"><summary>詳細設定</summary>
      <div class="three">
        <div><label class="field-label">Max Tokens</label><input type="number" id="${key("max_tokens")}"></div>
        <div><label class="field-label">Context Window</label><input type="number" id="${key("context_window")}"></div>
        <div><label class="field-label">Timeout（秒）</label><input type="number" id="${key("timeout")}"></div>
      </div>
      <label class="check"><input type="checkbox" id="${key("use_proxy")}"> プロキシを使う（OFF なら環境変数のプロキシも無視して直結）</label>
      <label class="field-label">Proxy URL（空なら環境変数 HTTPS_PROXY）</label><input type="text" id="${key("proxy_url")}">
    </details>
    <div class="row gap top-space"><button class="btn" data-test="${name}">接続テスト</button>
      <span class="badge ${p.chat_ready ? "ok" : "ng"}">${p.chat_ready ? "チャット設定済み" : "チャット未設定"}</span>
      <span class="badge ${p.embed_ready ? "ok" : ""}">${p.embed_ready ? "埋め込みあり" : "埋め込みなし"}</span></div>
    <div class="test-result" id="test-${name}"></div>`;
}

const PROFILE_FIELDS = ["provider", "base_url", "model", "ingest_model", "api_version", "effort", "embed_model",
  "embed_provider", "embed_base_url", "max_tokens", "context_window", "timeout", "proxy_url"];

function syncProviderUi(name) {
  const prov = $(`#pf-${name}-provider`).value;
  $$(`[data-only="${name}-azure"]`).forEach((el) => el.classList.toggle("hidden", prov !== "azure"));
  $$(`[data-only="${name}-anthropic"]`).forEach((el) => el.classList.toggle("hidden", prov !== "anthropic"));
  const hint = { openai: "例: http://127.0.0.1:11434/v1", azure: "例: https://<リソース名>.openai.azure.com",
    anthropic: "空で既定（API キーは環境変数 ANTHROPIC_API_KEY でも可）" }[prov];
  $(`[data-hint="${name}-base"]`).textContent = hint;
  $(`[data-label="${name}-model"]`).textContent = prov === "azure" ? "デプロイ名" : "モデル";
}

async function loadSettings() {
  const cfg = await api("/api/config");
  state.config = cfg;
  for (const name of ["local", "cloud"]) {
    const card = $(`.card[data-profile="${name}"]`);
    $$(".pf", card).forEach((el) => el.remove());
    const div = document.createElement("div");
    div.className = "pf";
    div.innerHTML = profileForm(name, cfg.profiles[name]);
    card.appendChild(div);
    const p = cfg.profiles[name];
    for (const f of PROFILE_FIELDS) { const el = $(`#pf-${name}-${f}`); if (el) el.value = p[f] ?? ""; }
    $(`#pf-${name}-use_proxy`).checked = !!p.use_proxy;
    $(`#pf-${name}-provider`).addEventListener("change", () => syncProviderUi(name));
    syncProviderUi(name);
  }
  $$("[data-preset]").forEach((el) => el.addEventListener("click", () => {
    const [name, i] = el.dataset.preset.split(":");
    const v = PRESETS[name][Number(i)].v;
    for (const [k, val] of Object.entries(v)) { const inp = $(`#pf-${name}-${k}`); if (inp) inp.value = val; }
    syncProviderUi(name);
  }));
  $$("[data-test]").forEach((el) => el.addEventListener("click", async () => {
    const name = el.dataset.test;
    await saveSettings(true);
    $(`#test-${name}`).textContent = "接続テスト中…";
    const r = await api("/api/config/test", { profile: name });
    $(`#test-${name}`).innerHTML = r.error ? `❌ ${esc(r.error)}`
      : `${r.chat.ok ? "✅" : "❌"} チャット: ${esc(r.chat.message)}<br>${r.embed.ok ? "✅" : "➖"} 埋め込み: ${esc(r.embed.message)}`;
  }));
  for (const k of ["default_sensitivity", "public_ingest_profile", "chat_mode"]) $(`#p-${k}`).value = cfg.policy[k];
  for (const k of ["graph", "doc_summary", "router", "corrective", "rerank"]) $(`#r-${k}`).checked = !!cfg.rag[k];
  for (const k of ["top_k", "chunk_size", "max_graph_chunks"]) $(`#r-${k}`).value = cfg.rag[k];
  $("#up-sens").value = cfg.policy.default_sensitivity;
}

async function saveSettings(quiet) {
  const profiles = {};
  for (const name of ["local", "cloud"]) {
    const p = { use_proxy: $(`#pf-${name}-use_proxy`).checked };
    for (const f of PROFILE_FIELDS) { const el = $(`#pf-${name}-${f}`); if (el) p[f] = el.value; }
    for (const s of ["api_key", "embed_api_key"]) { const v = $(`#pf-${name}-${s}`).value; if (v) p[s] = v; }
    profiles[name] = p;
  }
  const policy = {};
  for (const k of ["default_sensitivity", "public_ingest_profile", "chat_mode"]) policy[k] = $(`#p-${k}`).value;
  const rag = {};
  for (const k of ["graph", "doc_summary", "router", "corrective", "rerank"]) rag[k] = $(`#r-${k}`).checked;
  for (const k of ["top_k", "chunk_size", "max_graph_chunks"]) rag[k] = Number($(`#r-${k}`).value);
  const cfg = await api("/api/config", { profiles, policy, rag });
  state.config = cfg;
  if (!quiet) { $("#s-msg").textContent = "✅ 保存しました"; loadSettings(); }
  pollStatus();
}
$("#s-save").addEventListener("click", () => saveSettings(false));
$("#s-reindex").addEventListener("click", async () => {
  if (!confirm("全文書をテキスト抽出から処理し直します（LLM を多く呼びます）。よろしいですか？")) return;
  await saveSettings(true);
  await api("/api/docs/reindex-all", { stages: ["extract", "embed", "graph", "summary"] });
  $("#s-msg").textContent = "再処理を開始しました（文書タブで進捗を確認できます）";
  pollStatus();
});

// ---------------------------------------------------------------- init

(async function init() {
  state.config = await api("/api/config");
  if (state.config.policy) $("#up-sens").value = state.config.policy.default_sensitivity;
  await Promise.all([loadDocs(true), loadChats(), pollStatus()]);
  refreshChatHead();
  if (state.chats[0]) await openChat(state.chats[0].id);
  else renderMessages();
  setInterval(pollStatus, 2500);
})();
