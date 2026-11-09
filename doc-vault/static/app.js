/* Doc Vault — フロントエンド（依存なしの素の JS） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = {
  docs: [], folders: [], tags: [], supported: [], status: null, config: null,
  chats: [], chat: null, messages: [], sending: false, pendingScope: {},
  folderFilter: "", tagFilter: "", viewerDoc: null, selected: new Set(), collapsed: new Set(),
};
const ROUTE_LABEL = { fact: "事実の確認", relation: "関係・経緯", summary: "要約・全体像",
  compare: "比較", chat: "雑談・一般" };
const STATUS_LABEL = { queued: "待機中", processing: "処理中", ready: "検索可", error: "エラー" };
const MODE_LABEL = { standard: "標準", fast: "高速", terms: "語の共起（LLM なし）" };
const READING_STATUS = { running: "処理中", done: "完了", partial: "一部", paused: "一時停止", error: "中止" };
const STAGE_NAME = { graph: "ナレッジグラフ", summary: "文書要約", embed: "埋め込み" };

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

const JUDGE_LABEL = { llm: "LLM", jev: "Jev", openrouter: "Jev（OpenRouter）", openai: "OpenAI",
  azure: "Azure OpenAI" };
const JUDGE_PROVIDERS = ["openai", "azure", "openrouter"];

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

function fmtScore(v) {
  if (v === undefined || v === null || v === "") return "";
  const n = Number(v);
  if (!isFinite(n)) return esc(v);
  return Math.abs(n) >= 10 ? n.toFixed(1) : Math.abs(n) >= 0.1 ? n.toFixed(3) : n.toPrecision(3);
}

function sensBadge(s) {
  return s === "public" ? '<span class="badge public">公開</span>'
    : '<span class="badge internal">社外秘</span>';
}

function store(key, value) {           // 表示の好み（フォルダの開閉）だけを保存。使えなくても動く
  try { if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null"); localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 保存できない環境 */ }
  return null;
}

let toastTimer = null;
function toast(msg) {
  let el = $("#toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; document.body.appendChild(el); }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
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
    .replace(/\[((?:S|D|C)\d+)\]/g, '<span class="cite" data-ref="$1" tabindex="0">$1</span>');
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
  const busy = st.indexer.busy ?? ((st.indexer.current ? 1 : 0) + st.indexer.pending.length);
  const lw = st.indexer.later_waiting || {};
  const busyTitle = busy ? `抽出待ち ${st.indexer.extract_waiting ?? 0} 件・順番待ち: 埋め込み ${lw.embed ?? 0} / `
    + `要約 ${lw.summary ?? 0} / グラフ抽出 ${lw.graph ?? 0}（抽出が済んだ文書は検索・相談に使えます）` : "";
  $("#conn-status").innerHTML = prof("local", "ローカル") + prof("cloud", "クラウド")
    + `<span title="${esc(busyTitle)}"><span class="dot ${busy ? "busy" : "ok"}"></span>`
    + `${busy ? `取り込み中 ${busy} 件` : `文書 ${st.docs} 件`}</span>`;
  if (busy && !$("#tab-docs").classList.contains("hidden")) loadDocs(true);
  if (state._wasBusy && !busy) { loadDocs(true); renderExamples(); }
  state._wasBusy = !!busy;
}

// ---------------------------------------------------------------- 相談先（相談の範囲）

function scopeFolders(scope) {
  const f = [...((scope && scope.folders) || [])];
  if (scope && scope.folder) f.push(scope.folder);
  return [...new Set(f.filter(Boolean))];
}

function inScope(d, scope) {
  const fs = scopeFolders(scope), ids = (scope && scope.doc_ids) || [];
  if (fs.length || ids.length) {
    if (!(ids.includes(d.id) || fs.some((f) => d.folder === f || d.folder.startsWith(f + "/")))) return false;
  }
  const tags = (scope && scope.tags) || [];
  return !tags.length || tags.some((t) => (d.tags || []).includes(t));
}

function describeScope(scope) {
  const fs = scopeFolders(scope), ids = (scope && scope.doc_ids) || [];
  const parts = [];
  if (fs.length) parts.push("📁 " + fs.slice(0, 3).join("、") + (fs.length > 3 ? ` ほか ${fs.length - 3}` : ""));
  if (ids.length) {
    const names = ids.map((id) => (state.docs.find((d) => d.id === id) || {}).title || "（削除された文書）");
    parts.push("📄 " + names.slice(0, 2).map((n) => n.slice(0, 20)).join("、") + (names.length > 2 ? ` ほか ${names.length - 2}` : ""));
  }
  const hits = state.docs.filter((d) => inScope(d, scope));
  return { label: parts.join(" ＋ ") || "📚 すべての文書", docs: hits.length, hits,
    internal: hits.filter((d) => d.sensitivity !== "public").length,
    public: hits.filter((d) => d.sensitivity === "public").length, all: !parts.length };
}

function currentScope() { return state.chat ? (state.chat.scope || {}) : (state.pendingScope || {}); }

function graphSummary(hits) {
  const counts = {};
  for (const d of hits) {
    const g = (d.readings || {}).graph;
    const key = g ? (MODE_LABEL[g.mode || "standard"] || g.mode) : "グラフなし";
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join("・");
}

const STRATEGY_NOTE = {
  auto: "自動: 事実・比較 → 🌲DecisionRAG＋🔎ハイブリッド ／ 関係 → 🕸GraphRAG＋🔎ハイブリッド ／ 要約 → 📑文書要約＋🧩コミュニティ要約",
  fast: "速さ優先: 🔤全文＋🔎埋め込み＋🕸GraphRAG を毎回使い、LLM は回答の 1 回だけ（ルーター・根拠チェックを省く）",
  custom: "選んで使う: 「使う RAG」でチェックした方式を、質問の種類にかかわらず毎回使う",
  decision: "DecisionRAG のみ: 検索器を使わず、フォルダ→文書→節→段落を判定モデルでたどる",
  classic: "従来型: 🔎全文（BM25）＋埋め込み＋🕸GraphRAG（関係の質問）を RRF で統合（DecisionRAG なし）",
};
// 自動・従来型で、方式を使う質問の種類
const AUTO_WHEN = { decision: "事実・比較の質問", graph: "関係・比較・要約の質問か、固有の対象を含む質問",
  summary: "要約の質問", community: "要約の質問（文書 4 件以上）", multi: "比較の質問",
  bm25: "毎回", vector: "毎回（埋め込みモデルがあれば）", router: "毎回", crag: "事実・関係・比較の質問", rerank: "毎回" };
const METHOD_ICON2 = { bm25: "🔤", vector: "🔎", graph: "🕸", decision: "🌲", summary: "📑", community: "🧩",
  multi: "🔀", router: "🧭", crag: "↻", rerank: "⇅" };

function ragCatalog() { return (state.config && state.config.rag_methods) || null; }

function effectiveMethods(strategy, chat) {
  const cat = ragCatalog();
  if (!cat) return [];
  if (strategy === "custom") {
    const m = (chat && chat.methods && chat.methods.length) ? chat.methods
      : ((state.config.rag && state.config.rag.custom_methods) || []);
    return cat.order.filter((x) => m.includes(x));
  }
  return (cat.strategies[strategy] || cat.strategies.auto).methods;
}

function estimate(methods, dynamic) {
  // 1 問あたりの LLM 呼び出しの目安。自動・従来型は質問の種類で使う方式が変わるので、
  // DecisionRAG（事実・比較）とコミュニティ要約（要約）は同じ質問では重ならないものとして最大を数える
  const cat = ragCatalog();
  const rag = (state.config && state.config.rag) || {};
  const budget = (cat ? cat.decision_budget : 8) + (rag.decision_choice === false ? 0 : 1);
  const parts = ["回答 1"];
  let total = 1, embed = 0;
  const cost = {};
  for (const m of cat ? cat.order : []) {
    if (!methods.includes(m)) continue;
    const info = cat.methods[m];
    const n = info.llm === "budget" ? budget : Number(info.llm) || 0;
    cost[m] = n;
    if (n) parts.push(`${info.label} ${m === "decision" || m === "community" ? "最大 " : ""}${n}`);
    if (info.embed === 1) embed += 1;
  }
  if (dynamic) {
    const c = (m) => cost[m] || 0;
    total += c("router") + c("rerank") + Math.max(c("decision") + c("crag"), c("community"), c("crag"));
  } else {
    total += Object.values(cost).reduce((a, b) => a + b, 0);
  }
  return { total, embed, parts };
}

function costText(m, info, cat) {
  if (info.llm === "budget") return `LLM 最大 ${cat.decision_budget + 1} 回`;
  if (info.llm) return m === "community" ? "LLM 最大 5 回（初回）" : `LLM ${info.llm} 回`;
  if (info.embed === 1) return "埋め込み 1 回";
  if (info.embed === "targets") return "埋め込み（対象ごと）";
  return "LLM 0 回";
}

function renderRagPanel() {
  const cat = ragCatalog();
  const box = $("#rag-list");
  if (!cat || !box) return;
  const strategy = $("#chat-strategy").value;
  const dynamic = cat.strategies[strategy] ? cat.strategies[strategy].dynamic : false;
  const methods = effectiveMethods(strategy, state.chat);
  const row = (m) => {
    const info = cat.methods[m];
    const on = methods.includes(m);
    return `<label class="rag-item ${on ? "on" : ""} ${dynamic ? "locked" : ""}" title="${esc(info.desc)}">
      <input type="checkbox" data-method="${m}" ${on ? "checked" : ""} ${dynamic ? "disabled" : ""}>
      <span class="rag-name">${METHOD_ICON2[m] || ""} ${esc(info.label)}</span>
      <span class="rag-cost ${info.llm ? "llm" : ""}">${esc(costText(m, info, cat))}</span>
      <span class="rag-desc">${esc(info.desc)}${dynamic && on ? `<span class="rag-when">自動で使うとき: ${esc(AUTO_WHEN[m] || "")}</span>` : ""}</span></label>`;
  };
  const lockNote = dynamic ? `<div class="rag-lock">「${esc((cat.strategies[strategy] || {}).label || "")}」は質問の種類で方式を使い分けます（下の表示は使うことがある方式）。
      自分で選ぶには <button class="btn small" id="rag-to-custom">選んで使うに切り替える</button></div>` : "";
  box.innerHTML = `${lockNote}<div class="rag-group-title">${dynamic ? "検索の方式（質問の種類で使い分け、RRF で統合）" : "検索の方式（選んだものを毎回使い、RRF で統合）"}</div>${cat.retrievers.map(row).join("")}
    <div class="rag-group-title">補助の LLM 処理</div>${cat.helpers.map(row).join("")}`;
  const est = estimate(methods, dynamic);
  $("#rag-est").textContent = `1 問の LLM 呼び出し: ${dynamic ? "質問の種類により最大 " : ""}${est.total} 回（${est.parts.join("・")}）`
    + (est.embed ? `・埋め込み ${est.embed} 回` : "");
  const labels = methods.filter((m) => cat.retrievers.includes(m)).map((m) => cat.methods[m].label);
  $("#rag-summary").textContent = `${(cat.strategies[strategy] || {}).label || strategy}: ${labels.join("＋") || "（なし）"}`
    + `　LLM ${dynamic ? "最大 " : ""}${est.total} 回`;
  $("#rag-presets").innerHTML = [["fast", "速さ優先"], ["hybrid", "全文＋埋め込みだけ"], ["graph", "GraphRAG 重視"], ["all", "全部"]]
    .map(([k, l]) => `<button class="btn small" data-preset="${k}">${l}</button>`).join("");
  const toCustom = $("#rag-to-custom");
  if (toCustom) toCustom.addEventListener("click", () => { $("#chat-strategy").value = "custom"; updateChat({ strategy: "custom" }); });
  $$("#rag-list [data-method]").forEach((el) => el.addEventListener("change", () => {
    const cur = new Set(effectiveMethods($("#chat-strategy").value, state.chat));
    if (el.checked) cur.add(el.dataset.method); else cur.delete(el.dataset.method);
    setMethods([...cur]);
  }));
  $$("#rag-presets [data-preset]").forEach((el) => el.addEventListener("click", () => {
    const k = el.dataset.preset;
    if (k === "fast") { updateChat({ strategy: "fast" }); return; }
    setMethods({ hybrid: ["bm25", "vector"], graph: ["bm25", "vector", "graph", "crag"],
      all: cat.order.slice() }[k]);
  }));
}

function setMethods(methods) {
  const cat = ragCatalog();
  if (cat && !methods.some((m) => cat.retrievers.includes(m))) {
    $("#chat-hint").textContent = "検索の方式を 1 つ以上選んでください（全文・埋め込み・GraphRAG など）";
    renderRagPanel();
    return;
  }
  $("#chat-hint").textContent = "";
  updateChat({ strategy: "custom", methods });
}

$("#rag-default").addEventListener("click", async () => {
  const strategy = $("#chat-strategy").value;
  const rag = { default_strategy: strategy };
  if (strategy === "custom") rag.custom_methods = effectiveMethods("custom", state.chat);
  state.config = await api("/api/config", { rag });
  toast(`新しい相談の既定を「${(ragCatalog().strategies[strategy] || {}).label || strategy}」にしました`);
  renderRagPanel();
});

function refreshChatHead(flash) {
  const c = state.chat;
  const scope = currentScope();
  const info = describeScope(scope);
  $("#chat-title").value = c ? c.title : "";
  $("#chat-mode").value = c ? c.mode : ((state.config && state.config.policy && state.config.policy.chat_mode) || "auto");
  $("#chat-strategy").value = c ? (c.strategy || "auto")
    : ((state.config && state.config.rag && state.config.rag.default_strategy) || "auto");
  $("#chat-beam").value = c && c.decision_beam ? c.decision_beam : "";
  const defBeam = state.config && state.config.rag ? state.config.rag.decision_beam : 4;
  $("#chat-beam").placeholder = `既定 ${defBeam}`;
  $("#chat-beam-box").classList.toggle("hidden", $("#chat-strategy").value === "classic");
  $("#scope-label").textContent = info.label;
  const cloud = $("#chat-mode").value === "cloud";
  $("#scope-count").textContent = `${info.docs} 文書（社外秘 ${info.internal}・公開 ${info.public}）`
    + (cloud ? ` → クラウドモードのため公開 ${info.public} 文書だけを検索` : "");
  $("#scope-bar").classList.toggle("narrow", !info.all);
  $("#q").placeholder = `${info.label}（${cloud ? info.public : info.docs} 文書）に質問 — Enter で送信、Shift+Enter で改行`;
  const gs = graphSummary(info.hits);
  $("#strategy-note").textContent = (STRATEGY_NOTE[$("#chat-strategy").value] || "")
    + (gs ? ` ／ この相談先のグラフ: ${gs}` : "");
  renderRagPanel();
  if (flash) {
    const bar = $("#scope-bar");
    bar.classList.remove("flash"); void bar.offsetWidth; bar.classList.add("flash");
  }
}

async function setScope(scope, { asNew = false } = {}) {
  if (asNew || !state.chat) {
    await newChat(scope);
  } else {
    await updateChat({ scope });
    refreshChatHead(true);
  }
}

// 相談先の選択ダイアログ
const scopeDraft = { folders: new Set(), docs: new Set() };
function openScopeModal() {
  const scope = currentScope();
  scopeDraft.folders = new Set(scopeFolders(scope));
  scopeDraft.docs = new Set(scope.doc_ids || []);
  $("#sp-doc-q").value = "";
  renderScopeModal();
  $("#sp-new").classList.toggle("hidden", !(state.chat && state.messages.length));
  $("#sp-apply").textContent = state.chat && state.messages.length ? "この相談の相談先を変える" : "この相談先にする";
  $("#scope-modal").classList.remove("hidden");
}

function renderScopeModal() {
  $("#sp-folders").innerHTML = state.folders.filter((f) => f.path).map((f) => `
    <label class="scope-item" style="padding-left:${4 + (f.depth - 1) * 16}px">
      <input type="checkbox" data-folder="${esc(f.path)}" ${scopeDraft.folders.has(f.path) ? "checked" : ""}>
      📁 ${esc(f.name)} <span class="cnt">${f.count}</span></label>`).join("")
    || '<p class="hint">フォルダはまだありません（文書タブで作れます）。</p>';
  const q = $("#sp-doc-q").value.trim().toLowerCase();
  const docs = state.docs.filter((d) => !q || (d.title + " " + d.folder + " " + d.filename).toLowerCase().includes(q));
  $("#sp-docs").innerHTML = docs.map((d) => `
    <label class="scope-item"><input type="checkbox" data-doc="${d.id}" ${scopeDraft.docs.has(d.id) ? "checked" : ""}>
      📄 ${esc(d.title)} ${d.sensitivity === "public" ? "" : '<span class="badge internal">秘</span>'}
      <span class="muted">${esc(d.folder || "（フォルダなし）")}</span></label>`).join("")
    || '<p class="hint">該当する文書はありません。</p>';
  $$("#sp-folders [data-folder]").forEach((el) => el.addEventListener("change", () => {
    el.checked ? scopeDraft.folders.add(el.dataset.folder) : scopeDraft.folders.delete(el.dataset.folder);
    updateScopePreview();
  }));
  $$("#sp-docs [data-doc]").forEach((el) => el.addEventListener("change", () => {
    el.checked ? scopeDraft.docs.add(el.dataset.doc) : scopeDraft.docs.delete(el.dataset.doc);
    updateScopePreview();
  }));
  updateScopePreview();
}

function draftScope() { return { folders: [...scopeDraft.folders], doc_ids: [...scopeDraft.docs], tags: [] }; }

function updateScopePreview() {
  const info = describeScope(draftScope());
  const list = info.hits.slice(0, 8).map((d) => esc(d.title.slice(0, 24))).join("、");
  $("#sp-preview").innerHTML = `<b>${esc(info.label)}</b> → ${info.docs} 文書（社外秘 ${info.internal}・公開 ${info.public}）`
    + (info.all ? "" : `<div class="muted">${list}${info.docs > 8 ? ` ほか ${info.docs - 8} 件` : ""}</div>`);
  $$("#sp-docs [data-doc]").forEach((el) => {     // フォルダで入っている文書は印を付ける
    const d = state.docs.find((x) => x.id === el.dataset.doc);
    const viaFolder = d && [...scopeDraft.folders].some((f) => d.folder === f || d.folder.startsWith(f + "/"));
    el.closest(".scope-item").classList.toggle("via-folder", !!viaFolder && !el.checked);
  });
}

$("#scope-change").addEventListener("click", openScopeModal);
$("#sp-doc-q").addEventListener("input", renderScopeModal);
$("#sp-close").addEventListener("click", () => $("#scope-modal").classList.add("hidden"));
$("#sp-all").addEventListener("click", () => { scopeDraft.folders.clear(); scopeDraft.docs.clear(); renderScopeModal(); });
$("#sp-apply").addEventListener("click", async () => {
  $("#scope-modal").classList.add("hidden");
  await setScope(draftScope());
});
$("#sp-new").addEventListener("click", async () => {
  $("#scope-modal").classList.add("hidden");
  await setScope(draftScope(), { asNew: true });
});

// ---------------------------------------------------------------- 相談

async function loadChats() {
  const r = await api("/api/chats");
  state.chats = r.chats || [];
  renderChatList();
}

function renderChatList() {
  $("#chat-items").innerHTML = state.chats.map((c) => {
    const info = c.scope_info || describeScope(c.scope || {});
    return `<div class="chat-item ${state.chat && state.chat.id === c.id ? "active" : ""}" data-id="${c.id}">
      <div class="t-wrap"><span class="t" title="${esc(c.title)}">${esc(c.title || "新しい相談")}</span>
        <span class="chat-scope" title="相談先: ${esc(info.label)}（${info.docs} 文書）">${esc(info.label)} ・ ${info.docs}</span></div>
      ${c.touched_internal ? '<span class="badge internal" title="社外秘資料を参照済み">秘</span>' : ""}
      <span class="x" data-del="${c.id}" title="削除">×</span>
    </div>`;
  }).join("") || '<p class="hint">まだ相談はありません。</p>';
  $$("#chat-items .chat-item").forEach((el) => el.addEventListener("click", (ev) => {
    if (ev.target.dataset.del) return;
    openChat(el.dataset.id);
  }));
  $$("#chat-items [data-del]").forEach((el) => el.addEventListener("click", async () => {
    if (!confirm("この相談を削除しますか？")) return;
    await api(`/api/chats/${el.dataset.del}/delete`, {});
    if (state.chat && state.chat.id === el.dataset.del) { state.chat = null; state.messages = []; renderMessages(); refreshChatHead(); }
    loadChats();
  }));
}

async function newChat(scope) {
  const r = await api("/api/chats", { scope: scope || state.pendingScope || {}, mode: $("#chat-mode").value });
  state.chat = r.chat; state.messages = [];
  state.pendingScope = {};
  await loadChats();
  refreshChatHead(true); renderMessages();
  $("#q").focus();
  return r.chat;
}

async function openChat(id) {
  const r = await api(`/api/chats/${id}`);
  if (r.error) return;
  const changed = !state.chat || state.chat.id !== id;
  state.chat = r.chat; state.messages = r.messages;
  renderChatList(); refreshChatHead(changed); renderMessages();
}

async function updateChat(fields) {
  if (!state.chat) await newChat(fields.scope);
  const r = await api(`/api/chats/${state.chat.id}/update`, fields);
  if (r.chat) { state.chat = r.chat; refreshChatHead(); loadChats(); }
}

$("#chat-mode").addEventListener("change", () => {
  if (state.chat) updateChat({ mode: $("#chat-mode").value }); else refreshChatHead();
});
$("#chat-strategy").addEventListener("change", () => {
  const strategy = $("#chat-strategy").value;
  if (strategy === "custom") $("#rag-panel").open = true;
  updateChat({ strategy });
});
$("#chat-beam").addEventListener("change", () => {
  const v = $("#chat-beam").value.trim();
  updateChat({ decision_beam: v ? Math.max(1, Math.min(10, Number(v) || 0)) : 0 });
});
$("#chat-title").addEventListener("change", () => updateChat({ title: $("#chat-title").value }));
$("#chat-new").addEventListener("click", () => {
  // 新しい相談は「すべての文書」から。相談先は空の画面のボタンか「相談先を変える」で選ぶ
  state.chat = null; state.messages = []; state.pendingScope = {};
  renderChatList(); refreshChatHead(true); renderMessages();
  $("#q").focus();
});

function renderExamples() {
  const box = $("#examples");
  if (!box) return;
  const ex = ["取り込んだ資料の全体像を要約して"];
  const docs = state.docs.filter((d) => d.status === "ready" && inScope(d, currentScope()));
  if (docs[0]) ex.push(`「${docs[0].title.slice(0, 24)}」の要点は？`);
  const kws = docs.flatMap((d) => d.keywords || []).filter((k, i, a) => a.indexOf(k) === i);
  if (kws[0]) ex.push(`${kws[0]}について教えて`);
  if (kws[1] && kws[2]) ex.push(`${kws[1]}と${kws[2]}の関係は？`);
  box.innerHTML = state.docs.length
    ? ex.map((q) => `<span class="example">${esc(q)}</span>`).join("")
    : '<p>まず「文書」タブで文書を取り込んでください（サンプルも用意しています）。</p>';
  $$("#examples .example").forEach((el) => el.addEventListener("click", () => {
    $("#q").value = el.textContent; send();
  }));
  const picks = $("#scope-picks");
  if (!picks) return;
  const scope = currentScope();
  const cur = JSON.stringify({ f: scopeFolders(scope), d: scope.doc_ids || [] });
  const opts = [{ label: "📚 すべての文書", scope: {} }]
    .concat(state.folders.filter((f) => f.depth === 1).map((f) => ({ label: `📁 ${f.name}（${f.count}）`, scope: { folders: [f.path] } })))
    .concat([...state.docs].sort((a, b) => b.updated_at - a.updated_at).slice(0, 3)
      .map((d) => ({ label: `📄 ${d.title.slice(0, 22)}`, scope: { doc_ids: [d.id] } })));
  picks.innerHTML = opts.map((o, i) => {
    const on = JSON.stringify({ f: scopeFolders(o.scope), d: o.scope.doc_ids || [] }) === cur;
    return `<button class="pick ${on ? "on" : ""}" data-i="${i}">${esc(o.label)}</button>`;
  }).join("") + '<button class="pick" data-more="1">その他を選ぶ…</button>';
  $$("#scope-picks .pick").forEach((el) => el.addEventListener("click", () => {
    if (el.dataset.more) { openScopeModal(); return; }
    const o = opts[Number(el.dataset.i)];
    if (state.chat) setScope(o.scope); else { state.pendingScope = o.scope; refreshChatHead(true); renderExamples(); }
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
  div.innerHTML = `<h2>どの文書に聞きますか？</h2>
    <p>相談先を選んでから質問してください。選んだ文書だけを検索して、出典付きで回答します（上の「相談先」にいつでも表示されます）。</p>
    <div class="scope-picks" id="scope-picks"></div>
    <div class="muted top-space">質問の例</div>
    <div class="examples" id="examples"></div>`;
  return div;
}

const METHOD_ICON = { decision: "🌲", hybrid: "🔎", bm25: "🔤", graph: "🕸", multi: "🔀", summary: "📑",
  community: "🧩", crag: "↻", rerank: "⇅" };

function methodsHtml(meta) {
  let ms = meta.methods || [];
  if (!ms.length && (meta.via || []).length) ms = meta.via.map((v) => ({ id: "", label: v, detail: v }));   // 以前の回答
  if (!ms.length && !meta.profile) return "";
  const chips = ms.map((m) => `<span class="method m-${esc(m.id)}" title="${esc(m.detail)}">${METHOD_ICON[m.id] || "•"} ${esc(m.label)}`
    + `${m.hits ? `<span class="cnt">${m.hits}</span>` : ""}</span>`);
  const ans = meta.profile ? `<span class="method m-answer" title="回答を書いた LLM">✍ ${esc(meta.answer_model || "")}`
    + `（${meta.profile === "local" ? "ローカル" : "クラウド"}）</span>` : "";
  return `<div class="rag-flow"><span class="flow-k">使った RAG</span>${chips.join('<span class="arrow">＋</span>')}`
    + `${ans ? (chips.length ? '<span class="arrow">→</span>' : "") + ans : ""}</div>`;
}

function messageHtml(m, idx) {
  if (m.role === "user") {
    return `<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>`;
  }
  const meta = m.meta || {};
  const route = meta.route || {};
  const badges = [];
  if (route.scope_label) badges.push(`<span class="badge scope" title="この回答で検索した文書（相談先）">相談先: ${esc(route.scope_label)}（${route.docs_in_scope ?? "-"} 文書）</span>`);
  if (route.type) badges.push(`<span class="badge">${ROUTE_LABEL[route.type] || route.type}</span>`);
  if (route.strategy_label) {
    const cat = ragCatalog();
    const sel = (route.selected || []).map((m) => (cat && cat.methods[m] ? cat.methods[m].label : m)).join("・");
    badges.push(`<span class="badge" title="この回答の検索方式${sel ? `（選んでいた方式: ${esc(sel)}）` : ""}">検索方式: ${esc(route.strategy_label)}</span>`);
  }
  if (meta.profile) badges.push(`<span class="badge ${meta.profile}">${meta.profile === "local" ? "ローカル LLM" : "クラウド LLM"}</span>`);
  else if (route.type) badges.push('<span class="badge ng">LLM なし（抜粋）</span>');
  if (route.retrieval) badges.push(meta.sensitivity === "internal"
    ? '<span class="badge internal">社外秘資料を含む</span>' : '<span class="badge public">公開資料のみ</span>');
  if (meta.decision && meta.decision.hits) {
    const j = (meta.decision.judges || []).map((x) => JUDGE_LABEL[x] || x).join("・");
    badges.push(`<span class="badge decision" title="文書の階層を判定モデルでたどって探しました（判定 ${meta.decision.calls} 回）">DecisionRAG${j ? "・" + esc(j) : ""}</span>`);
  }
  if (meta.elapsed) badges.push(`<span class="muted">${meta.elapsed}秒</span>`);
  const sources = (meta.sources || []).map((s) => sourceHtml(s, idx)).join("");
  const trace = [];
  if (route.standalone && route.standalone !== (state.messages[idx - 1] || {}).content) {
    trace.push(`検索した質問: ${esc(route.standalone)}`);
  }
  if ((route.keywords || []).length) trace.push(`キーワード: ${esc(route.keywords.join("、"))}`);
  if ((meta.via || []).length) trace.push(`検索方法: ${esc(meta.via.join(" → "))}`);
  for (const s of meta.steps || []) trace.push(`${esc(s.t)}秒 ${esc(s.name)}: ${esc(s.detail)}`);
  if (meta.decision && meta.decision.path) {
    const lv = meta.decision.path.filter((p) => p.candidates).map((p) =>
      `${p.level + 1}回目: 候補 ${p.candidates} 件${p.trimmed ? `（判定回数の上限で ${p.trimmed} 件は未判定）` : ""} → `
      + (p.kept.length ? p.kept.map((k) => `${esc(k.title)}（確率 ${k.score}・経路 ${k.path ?? k.score}）`).join("、")
        : "展開なし")
      + (p.backtracked ? `　※前の段の次点へ ${p.backtracked} 本戻った` : ""));
    if (lv.length) trace.push(`DecisionRAG（ビーム幅 ${meta.decision.beam ?? "-"}）の経路: ${lv.join(" ／ ")}`);
    const a = meta.decision.api;
    if (a && a.calls) {
      trace.push(`判定 API（${esc(a.label)}）: ${a.calls} 回${a.cost ? `・$${a.cost}` : ""}`
        + `${a.input_tokens ? `・入力 ${a.input_tokens.toLocaleString()} トークン` : ""}`
        + `${a.refusals ? `・判定を断られた候補 ${a.refusals} 件` : ""}`);
    } else if (meta.decision.jev_cost) trace.push(`Jev の費用: $${meta.decision.jev_cost}`);   // 以前の回答
  }
  if (meta.llm) trace.push(`LLM 呼び出し: ローカル ${meta.llm.calls.local} 回 / クラウド ${meta.llm.calls.cloud} 回`);
  const warns = (meta.warnings || []).map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("");
  return `<div class="msg assistant" data-idx="${idx}"><div class="bubble">
    <div class="msg-meta">${badges.join("")}</div>
    ${methodsHtml(meta)}
    <div class="answer">${renderMd(m.content)}</div>
    ${warns}
    ${sources ? `<div class="sources-head muted">出典（マウスを載せると採用の根拠・スコアを表示）</div><div class="sources">${sources}</div>` : ""}
    ${trace.length ? `<details class="trace"><summary>検索の流れ</summary>${trace.map((t) => `<div>${t}</div>`).join("")}</details>` : ""}
  </div></div>`;
}

function sourceHtml(s, idx) {
  const icon = s.kind === "community" ? "🧩" : s.kind === "doc" ? "📑" : "📄";
  const w = s.why || {};
  const top = (w.signals || []).filter((x) => !x.sub).map((x) => x.name.replace(/（.*$/, "")).slice(0, 3).join("・");
  return `<div class="source ${s.used === false ? "unused" : ""}" data-idx="${idx}" data-ref="${esc(s.ref)}" tabindex="0">
    <div><span class="ref">${esc(s.ref)}</span>${icon} ${esc(s.doc_title)} <span class="loc">${esc(s.locator || "")}</span>
    ${s.sensitivity === "public" ? "" : '<span class="badge internal">秘</span>'}</div>
    <div class="snip">${esc(s.snippet || "")}</div>
    ${top ? `<div class="why-line">${w.fused_rank ? `総合 ${w.fused_rank} 位・` : ""}${esc(top)}${s.used === false ? "・引用なし" : ""}</div>` : ""}</div>`;
}

/* 採用根拠（出典にマウスを載せると、どの検索で何位・スコアいくつかを表示） */
function whyHtml(s) {
  const w = s.why;
  const head = `<div class="why-title"><b>${esc(s.ref)}</b> ${esc(s.doc_title)} <span class="muted">${esc(s.locator || "")}</span></div>`;
  if (!w || !(w.signals || []).length) {
    return head + '<div class="muted">この出典には採用根拠の記録がありません（以前の回答）。</div>';
  }
  const rows = (w.signals || []).map((x) => `<tr class="${x.sub ? "sub" : ""}">
      <td>${x.sub ? "└ " : ""}${esc(x.name)}${x.weight && x.weight !== 1 ? `<span class="muted"> ×${x.weight}</span>` : ""}</td>
      <td class="num">${x.rank ? `${x.rank} 位` : ""}</td><td class="num">${fmtScore(x.score)}</td></tr>`
    + (x.note && !x.sub ? `<tr class="note"><td colspan="3">${esc(x.note)}</td></tr>` : "")).join("");
  const lines = [];
  if (w.position) lines.push(`回答に渡した資料の ${w.position} 番目`);
  if (w.fused_rank) lines.push(`統合スコア（RRF）${fmtScore(w.fused)}・総合 ${w.fused_rank} 位`);
  if (w.rerank) lines.push(`再順位付け（LLM）: ${w.rerank.rank} 位（元 ${w.rerank.before} 位）`);
  if (w.crag) lines.push(`根拠不足の再検索（CRAG）「${esc(w.crag)}」で追加`);
  lines.push(s.used === false ? "回答では引用されなかった候補" : "回答で引用");
  return head + `<table class="why-table"><tr><th>検索</th><th>順位</th><th>スコア</th></tr>${rows}</table>`
    + `<div class="why-lines">${lines.map((l) => `<div>${l}</div>`).join("")}</div>`
    + '<div class="why-foot">RRF: 各検索の順位 r から 重み÷(60＋r) を足した値（大きいほど上位）。BM25: 語の一致の強さ。'
    + '類似度: 1 に近いほど意味が近い。経路スコア: DecisionRAG がたどった各段の判定確率の幾何平均。</div>';
}

function sourceOf(el) {
  const msgEl = el.closest(".msg");
  const m = msgEl && state.messages[Number(msgEl.dataset.idx)];
  return m && m.meta && (m.meta.sources || []).find((x) => x.ref === el.dataset.ref);
}

function showWhy(el) {
  const s = sourceOf(el);
  if (!s) return;
  const tip = $("#why-tip");
  tip.innerHTML = whyHtml(s);
  tip.classList.remove("hidden");
  const r = el.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = Math.min(window.innerWidth - tw - 8, Math.max(8, r.left));
  let y = r.bottom + 6;
  if (y + th > window.innerHeight - 8) y = Math.max(8, r.top - th - 6);
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
function hideWhy() { $("#why-tip").classList.add("hidden"); }

function bindMessageEvents(root) {
  $$(".cite, .source", root).forEach((el) => {
    el.addEventListener("click", () => {
      const s = sourceOf(el);
      hideWhy();
      if (s) openSource(s);
    });
    el.addEventListener("mouseenter", () => showWhy(el));
    el.addEventListener("mouseleave", hideWhy);
    el.addEventListener("focus", () => showWhy(el));
    el.addEventListener("blur", hideWhy);
  });
}

async function send() {
  const question = $("#q").value.trim();
  if (!question || state.sending) return;
  if (!state.chat) await newChat(state.pendingScope);
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

// ---------------------------------------------------------------- 音声入力

const speech = { active: null };

function speechConfig() { return (state.config && state.config.speech) || { engine: "auto" }; }

function browserRecognizer() { return window.SpeechRecognition || window.webkitSpeechRecognition || null; }

function browserOnDevice() {
  // 「端末内だけで認識する」指定（processLocally）に対応したブラウザか。
  // SpeechRecognition.available() は環境によってはタブごと落ちるため呼ばない。言語パックが無ければ
  // 認識の開始時にエラーになるので、そのときに案内する（音声は外部へ送られない）
  const SR = browserRecognizer();
  return !!SR && "processLocally" in SR.prototype;
}

async function pickSpeechEngine() {
  const sp = speechConfig();
  const lang = (sp.language || "ja") === "ja" ? "ja-JP" : sp.language;
  const w = sp.whisper || {};
  const whisperOk = !!w.url && (w.location !== "cloud" || sp.allow_cloud);
  if (sp.engine === "off") return { error: "音声入力は OFF です（設定タブ → 音声入力）" };
  if (sp.engine === "whisper" || (sp.engine === "auto" && whisperOk)) {
    if (!w.url) return { error: "Whisper の接続先が未設定です（設定タブ → 音声入力）" };
    if (!whisperOk) return { error: "Whisper の接続先がクラウドです。「音声をクラウドへ送ってよい」を ON にしてください" };
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return { error: "このブラウザ・接続ではマイクを使えません（http://127.0.0.1 で開くか、HTTPS が必要です）" };
    }
    return { kind: "whisper", label: `Whisper（${w.location === "cloud" ? "クラウド" : "ローカル"}）`, lang };
  }
  if (!browserRecognizer()) return { error: "このブラウザには音声認識がありません。Whisper を設定するか、Chrome / Edge を使ってください" };
  if (browserOnDevice()) return { kind: "browser", local: true, label: "ブラウザ（端末内）", lang };
  if (!sp.allow_cloud) {
    return { error: "ブラウザの音声認識は音声を Google / Microsoft のサーバへ送ります。使う場合は設定の"
      + "「音声をクラウドへ送ってよい」を ON にするか、社内の Whisper を設定してください" };
  }
  return { kind: "browser", local: false, label: "ブラウザ（クラウドの音声認識）", lang };
}

function insertText(text) {
  const q = $("#q");
  const start = q.selectionStart ?? q.value.length, end = q.selectionEnd ?? q.value.length;
  q.value = q.value.slice(0, start) + text + q.value.slice(end);
  q.selectionStart = q.selectionEnd = start + text.length;
  q.focus();
}

function recUi(on, text, engine) {
  $("#rec-bar").classList.toggle("hidden", !on);
  $("#mic").classList.toggle("recording", on);
  $("#mic").textContent = on ? "■" : "🎤";
  if (text !== undefined) $("#rec-text").textContent = text;
  if (engine !== undefined) $("#rec-engine").textContent = engine;
}

async function startSpeech() {
  const eng = await pickSpeechEngine();
  if (eng.error) { $("#chat-hint").textContent = `🎤 ${eng.error}`; return; }
  $("#chat-hint").textContent = "";
  if (eng.kind === "browser") return startBrowserSpeech(eng);
  return startWhisper(eng);
}

function startBrowserSpeech(eng) {
  const SR = browserRecognizer();
  const rec = new SR();
  rec.lang = eng.lang; rec.interimResults = true; rec.continuous = true;
  if (eng.local) { try { rec.processLocally = true; } catch (e) { /* 古いブラウザ */ } }
  const q = $("#q");
  const base = q.value + (q.value && !q.value.endsWith(" ") ? " " : "");
  let finalText = "";
  rec.onresult = (ev) => {
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      if (ev.results[i].isFinal) finalText += ev.results[i][0].transcript;
      else interim += ev.results[i][0].transcript;
    }
    q.value = base + finalText + interim;
  };
  rec.onerror = (ev) => {
    const local = eng.local && ["language-not-supported", "service-not-allowed", "network", "not-allowed"].includes(ev.error);
    $("#chat-hint").textContent = local && ev.error !== "not-allowed"
      ? `🎤 この PC では端末内の音声認識（${eng.lang}）を使えませんでした（${ev.error}）。Chrome の音声認識の言語パックを入れるか、社内の Whisper を設定してください`
      : `🎤 音声認識のエラー: ${ev.error}${ev.error === "not-allowed" ? "（マイクの使用が許可されていません）" : ""}`;
  };
  rec.onend = () => { speech.active = null; recUi(false); q.value = base + finalText; q.focus(); };
  const started = Date.now();
  const timer = setInterval(() => {
    const s = Math.floor((Date.now() - started) / 1000);
    $("#rec-text").textContent = `聞き取り中 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}（話し終えたら ■）`;
  }, 500);
  speech.active = { stop: () => { clearInterval(timer); rec.stop(); }, cancel: () => { clearInterval(timer); rec.abort(); q.value = base.trimEnd(); } };
  rec.start();
  recUi(true, "聞き取り中 0:00（話し終えたら ■）", eng.label);
}

async function startWhisper(eng) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    $("#chat-hint").textContent = `🎤 マイクを使えません: ${e.message}`;
    return;
  }
  const chunks = [];
  const recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (ev) => { if (ev.data.size) chunks.push(ev.data); };
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  const started = Date.now();
  let cancelled = false;
  const timer = setInterval(() => {
    analyser.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    $("#rec-level").style.width = `${Math.min(100, peak / 1.28)}%`;
    const s = Math.floor((Date.now() - started) / 1000);
    $("#rec-text").textContent = `録音中 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}（話し終えたら ■）`;
    if (s >= 300) speech.active && speech.active.stop();
  }, 100);
  const finish = async () => {
    clearInterval(timer);
    stream.getTracks().forEach((t) => t.stop());
    speech.active = null;
    if (cancelled) { ctx.close(); recUi(false); return; }
    recUi(true, "文字起こし中…", eng.label);
    try {
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      const wav = await toWav16k(blob, ctx);
      const res = await fetch(`/api/transcribe?lang=${encodeURIComponent(eng.lang)}`, {
        method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav,
      });
      const r = await res.json();
      if (r.error) throw new Error(r.error);
      if (r.text) insertText(r.text);
      else $("#chat-hint").textContent = "🎤 音声を聞き取れませんでした";
    } catch (e) {
      $("#chat-hint").textContent = `🎤 文字起こしに失敗しました: ${e.message}`;
    } finally {
      ctx.close();
      recUi(false);
    }
  };
  recorder.onstop = finish;
  speech.active = { stop: () => recorder.state !== "inactive" && recorder.stop(),
    cancel: () => { cancelled = true; if (recorder.state !== "inactive") recorder.stop(); } };
  recorder.start(250);
  recUi(true, "録音中 0:00（話し終えたら ■）", eng.label);
}

/* 録音（webm / ogg）→ 16kHz・モノラルの WAV（どの Whisper サーバでも読める形） */
async function toWav16k(blob, ctx) {
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  const rate = 16000;
  const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const node = off.createBufferSource();
  node.buffer = decoded;
  node.connect(off.destination);
  node.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const str = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); out.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
  out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true);
  out.setUint16(34, 16, true); str(36, "data"); out.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const v = Math.max(-1, Math.min(1, pcm[i]));
    out.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([out], { type: "audio/wav" });
}

$("#mic").addEventListener("click", () => {
  if (speech.active) speech.active.stop(); else startSpeech();
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
    const by = (d.flags || {}).summary_label;
    body.innerHTML = (d.summary ? `<div class="doc-summary"><b>要約</b>${by ? `<span class="muted">（${esc(by)} で作成）</span>` : ""}<br>${esc(d.summary)}
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
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    if (speech.active) { speech.active.cancel(); return; }
    $("#viewer").classList.add("hidden"); $("#doc-modal").classList.add("hidden");
    $("#scope-modal").classList.add("hidden"); hideWhy();
  }
});

// ---------------------------------------------------------------- 文書

async function loadDocs(quiet) {
  const r = await api("/api/docs");
  if (r.error) return;
  state.docs = r.docs; state.folders = r.folders; state.tags = r.tags; state.supported = r.supported || [];
  for (const id of [...state.selected]) if (!state.docs.some((d) => d.id === id)) state.selected.delete(id);
  renderFolders(); renderDocTable();
  if (!quiet) refreshChatHead();
}

const DND_DOCS = "application/x-docvault-docs";
const DND_FOLDER = "application/x-docvault-folder";

function folderVisible(f) {
  if (!f.path) return true;
  const parts = f.path.split("/");
  for (let i = 1; i < parts.length; i++) if (state.collapsed.has(parts.slice(0, i).join("/"))) return false;
  return true;
}

function renderFolders() {
  const hasKids = (p) => state.folders.some((f) => f.path.startsWith(p ? p + "/" : "") && f.path !== p && f.path);
  $("#folder-tree").innerHTML = state.folders.filter(folderVisible).map((f) => {
    const kids = hasKids(f.path);
    const open = !state.collapsed.has(f.path);
    return `<div class="folder ${state.folderFilter === f.path ? "active" : ""}" data-path="${esc(f.path)}"
        ${f.path ? 'draggable="true"' : ""} style="padding-left:${6 + f.depth * 14}px"
        title="${f.path ? esc(f.path) + "（" + f.count + " 文書・直下 " + f.direct + "）" : "すべての文書"}">
      <span class="tw">${f.path && kids ? `<span class="toggle" data-toggle="${esc(f.path)}">${open ? "▾" : "▸"}</span>` : '<span class="toggle"></span>'}`
      + `<span class="fname">${f.path ? "📁" : "📚"} ${esc(f.name)}</span></span>
      <span class="cnt">${f.count}</span>
      ${f.path ? `<span class="f-acts"><span data-sub="${esc(f.path)}" title="中にフォルダを作る">＋</span>`
        + `<span data-rename="${esc(f.path)}" title="名前を変える">✎</span><span data-rmf="${esc(f.path)}" title="フォルダを削除（文書は1つ上へ）">🗑</span></span>` : ""}
    </div>`;
  }).join("");
  $$("#folder-tree .folder").forEach((el) => {
    el.addEventListener("click", (ev) => {
      if (ev.target.dataset.toggle) {
        const p = ev.target.dataset.toggle;
        state.collapsed.has(p) ? state.collapsed.delete(p) : state.collapsed.add(p);
        store("docvault.collapsed", [...state.collapsed]);
        renderFolders();
        return;
      }
      if (ev.target.dataset.sub !== undefined) { newFolder(ev.target.dataset.sub); return; }
      if (ev.target.dataset.rename !== undefined) { renameFolder(ev.target.dataset.rename); return; }
      if (ev.target.dataset.rmf !== undefined) { deleteFolder(ev.target.dataset.rmf); return; }
      state.folderFilter = el.dataset.path; renderFolders(); renderDocTable();
      $("#up-folder").value = el.dataset.path;
    });
    bindFolderDrop(el);
    if (el.dataset.path) {
      el.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData(DND_FOLDER, el.dataset.path);
        ev.dataTransfer.setData("text/plain", el.dataset.path);
        ev.dataTransfer.effectAllowed = "move";
      });
    }
  });
  $("#tag-list").innerHTML = state.tags.map((t) =>
    `<span class="badge tag ${state.tagFilter === t ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`).join("")
    || '<span class="muted">なし</span>';
  $$("#tag-list .tag").forEach((el) => el.addEventListener("click", () => {
    state.tagFilter = state.tagFilter === el.dataset.tag ? "" : el.dataset.tag;
    renderFolders(); renderDocTable();
  }));
}

function bindFolderDrop(el) {
  const target = el.dataset.path;
  const accepts = (ev) => {
    const types = [...ev.dataTransfer.types];
    return types.includes(DND_DOCS) || types.includes(DND_FOLDER) || types.includes("Files");
  };
  el.addEventListener("dragover", (ev) => {
    if (!accepts(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = [...ev.dataTransfer.types].includes("Files") ? "copy" : "move";
    el.classList.add("drop-target");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
  el.addEventListener("drop", async (ev) => {
    if (!accepts(ev)) return;
    ev.preventDefault(); ev.stopPropagation();
    el.classList.remove("drop-target");
    const docs = ev.dataTransfer.getData(DND_DOCS);
    const folder = ev.dataTransfer.getData(DND_FOLDER);
    if (docs) {
      const ids = JSON.parse(docs);
      const r = await api("/api/docs/move", { doc_ids: ids, folder: target });
      if (r.error) { alert(r.error); return; }
      toast(`${ids.length} 件を「${target || "フォルダなし"}」へ移動しました`);
      state.selected.clear();
      loadDocs();
    } else if (folder) {
      const name = folder.split("/").pop();
      const to = target ? `${target}/${name}` : name;
      if (to === folder) return;
      const r = await api("/api/folders/rename", { path: folder, to });
      if (r.error) { alert(r.error); return; }
      toast(`フォルダ「${name}」を「${target || "最上位"}」へ移動しました`);
      if (state.folderFilter === folder || state.folderFilter.startsWith(folder + "/")) state.folderFilter = to;
      loadDocs(); loadChats();
    } else if (ev.dataTransfer.files.length) {
      uploadEntries(await filesFromDrop(ev.dataTransfer), target);
    }
  });
}

async function newFolder(parent) {
  const base = parent ?? state.folderFilter;
  const name = prompt(`新しいフォルダの名前${base ? `（「${base}」の中に作ります）` : ""}`);
  if (!name || !name.trim()) return;
  const r = await api("/api/folders", { path: base ? `${base}/${name.trim()}` : name.trim() });
  if (r.error) { alert(r.error); return; }
  state.collapsed.delete(base);
  await loadDocs();
}

async function renameFolder(path) {
  const parts = path.split("/");
  const name = prompt("フォルダの新しい名前（/ で区切ると別の場所へ移動します）", parts[parts.length - 1]);
  if (!name || !name.trim() || name.trim() === parts[parts.length - 1]) return;
  const to = name.includes("/") ? name.trim() : [...parts.slice(0, -1), name.trim()].join("/");
  const r = await api("/api/folders/rename", { path, to });
  if (r.error) { alert(r.error); return; }
  if (state.folderFilter === path || state.folderFilter.startsWith(path + "/")) state.folderFilter = to + state.folderFilter.slice(path.length);
  toast(`「${path}」→「${to}」（相談先も書き換えました）`);
  loadDocs(); loadChats();
}

async function deleteFolder(path) {
  const f = state.folders.find((x) => x.path === path);
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  if (!confirm(`フォルダ「${path}」を削除します。\n中の文書 ${f ? f.count : 0} 件とサブフォルダは「${parent || "フォルダなし"}」へ移ります（文書は消えません）。`)) return;
  const r = await api("/api/folders/delete", { path });
  if (r.error) { alert(r.error); return; }
  if (state.folderFilter === path || state.folderFilter.startsWith(path + "/")) state.folderFilter = parent;
  loadDocs(); loadChats();
}
$("#folder-new").addEventListener("click", () => newFolder(state.folderFilter));

function flagHtml(d) {
  const f = d.flags || {};
  const rd = d.readings || {};
  const item = (on, label, title, warn) => `<span class="flag ${on ? "on" : ""} ${warn ? "warn" : ""}" title="${esc(title)}">${label}</span>`;
  const g = rd.graph;
  const gLabel = g ? (g.mode === "fast" ? "グラフ(高速)" : g.mode === "terms" ? "グラフ(語)" : "グラフ") : "グラフ";
  const gWarn = g && g.status !== "done";
  return `<div class="flags">${item(f.bm25, "全文", "全文検索（BM25）")}${item(f.vector, "埋込", f.vector ? `埋め込み: ${f.vector}` : "埋め込みなし")}`
    + `${item(f.graph, gLabel, g ? `ナレッジグラフ: ${g.label}（${READING_STATUS[g.status] || g.status}）` : "グラフなし", gWarn)}`
    + `${item(f.summary, "要約", rd.summary ? `文書要約: ${rd.summary.label}` : "要約なし")}</div>`;
}

function llmLine(d) {
  const rd = d.readings || {};
  const labels = [...new Set([rd.graph && rd.graph.mode !== "terms" && rd.graph.label,
    rd.summary && rd.summary.label].filter(Boolean).map((l) => l.replace(/（.*$/, "")))];
  const active = ["graph", "summary", "embed"].filter((s) => rd[s]).length;
  const more = (rd.count || 0) - active;
  if (!labels.length && !more) return "";
  return `<div class="doc-llm" title="要約・グラフを作った LLM（文書の情報で切り替え・別の LLM で読み込み）">🤖 ${esc(labels.join(" / ") || "LLM なし")}`
    + `${more > 0 ? `<span class="muted"> ・他 ${more} 件の読み込み結果</span>` : ""}</div>`;
}

function visibleDocs() {
  return state.docs.filter((d) =>
    (!state.folderFilter || d.folder === state.folderFilter || d.folder.startsWith(state.folderFilter + "/"))
    && (!state.tagFilter || d.tags.includes(state.tagFilter)));
}

function renderDocTable() {
  const docs = visibleDocs();
  $("#docs-heading").innerHTML = `文書 <span class="muted">${docs.length} 件${state.folderFilter ? ` ・ 📁 ${esc(state.folderFilter)}` : ""}</span>`;
  renderBulkBar();
  if (!docs.length) {
    $("#doc-table").innerHTML = '<p class="hint">文書がありません。上の枠にファイルやフォルダをドロップするか、サンプルを読み込んでください。</p>';
    return;
  }
  const allOn = docs.every((d) => state.selected.has(d.id));
  $("#doc-table").innerHTML = `<table class="doc-table"><thead><tr>
      <th class="sel"><input type="checkbox" id="sel-all" ${allOn ? "checked" : ""} title="表示中をすべて選択"></th>
      <th>タイトル</th><th>区分</th><th>状態</th><th class="hide-sm">インデックス・読み込んだ LLM</th><th class="hide-sm">更新</th><th></th>
    </tr></thead><tbody>${docs.map((d) => {
      const ext = d.ext.replace(".", "");
      let st = `<span class="badge ${d.status === "ready" ? "ok" : d.status === "error" ? "ng" : "busy"}">${STATUS_LABEL[d.status] || d.status}</span>`;
      if (d.stage) st += `<div class="doc-sub">${esc(d.stage)}</div><div class="progress"><div style="width:${Math.round((d.progress || 0) * 100)}%"></div></div>`;
      if (d.error) st += `<div class="doc-sub" style="color:var(--ng)">${esc(d.error)}</div>`;
      if ((d.warnings || []).length) st += `<div class="doc-sub warn-link" data-edit="${d.id}" title="${esc(d.warnings.join("\n"))}">⚠ 警告 ${d.warnings.length} 件</div>`;
      return `<tr draggable="true" data-row="${d.id}" class="${state.selected.has(d.id) ? "selected" : ""}">
        <td class="sel"><input type="checkbox" data-sel="${d.id}" ${state.selected.has(d.id) ? "checked" : ""}></td>
        <td><span class="drag-handle" title="フォルダへドラッグして移動">⋮⋮</span><span class="ext ${ext}">${esc(ext.toUpperCase())}</span><span class="doc-title" data-open="${d.id}">${esc(d.title)}</span>
          <div class="doc-sub">📁 ${esc(d.folder || "（フォルダなし）")} ・ ${esc(d.filename)} ・ ${fmtSize(d.size)}${d.pages ? ` ・ ${d.pages}${ext === "pptx" ? "枚" : "p"}` : ""}
          ${(d.tags || []).map((t) => `<span class="badge">${esc(t)}</span>`).join(" ")}</div></td>
        <td>${sensBadge(d.sensitivity)}</td>
        <td>${st}</td>
        <td class="hide-sm">${flagHtml(d)}${llmLine(d)}</td>
        <td class="hide-sm doc-sub">${fmtDate(d.updated_at)}</td>
        <td class="actions"><button class="btn small" data-ask="${d.id}">質問</button>
          <button class="btn small" data-edit="${d.id}">情報</button></td>
      </tr>`;
    }).join("")}</tbody></table>`;
  $$("#doc-table [data-open]").forEach((el) => el.addEventListener("click", () => openDoc(el.dataset.open)));
  $$("#doc-table [data-edit]").forEach((el) => el.addEventListener("click", () => editDoc(el.dataset.edit)));
  $$("#doc-table [data-ask]").forEach((el) => el.addEventListener("click", async () => {
    showTab("chat");
    await newChat({ doc_ids: [el.dataset.ask] });
  }));
  $$("#doc-table [data-sel]").forEach((el) => el.addEventListener("change", () => {
    el.checked ? state.selected.add(el.dataset.sel) : state.selected.delete(el.dataset.sel);
    el.closest("tr").classList.toggle("selected", el.checked);
    renderBulkBar();
    $("#sel-all").checked = visibleDocs().every((d) => state.selected.has(d.id));
  }));
  $("#sel-all").addEventListener("change", (ev) => {
    for (const d of docs) ev.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id);
    renderDocTable();
  });
  $$("#doc-table tr[data-row]").forEach((tr) => tr.addEventListener("dragstart", (ev) => {
    const id = tr.dataset.row;
    const ids = state.selected.has(id) ? [...state.selected] : [id];
    ev.dataTransfer.setData(DND_DOCS, JSON.stringify(ids));
    ev.dataTransfer.setData("text/plain", ids.length > 1 ? `${ids.length} 件の文書` : (state.docs.find((d) => d.id === id) || {}).title || "");
    ev.dataTransfer.effectAllowed = "move";
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.textContent = ids.length > 1 ? `📄 ${ids.length} 件の文書` : `📄 ${(state.docs.find((d) => d.id === id) || {}).title || ""}`;
    document.body.appendChild(ghost);
    ev.dataTransfer.setDragImage(ghost, 10, 10);
    setTimeout(() => ghost.remove(), 0);
    document.body.classList.add("dragging-docs");
  }));
  $$("#doc-table tr[data-row]").forEach((tr) => tr.addEventListener("dragend", () => document.body.classList.remove("dragging-docs")));
}

function folderOptions(selected) {
  return state.folders.map((f) => `<option value="${esc(f.path)}" ${f.path === selected ? "selected" : ""}>`
    + `${f.path ? "　".repeat(f.depth - 1) + "📁 " + esc(f.name) : "（フォルダなし）"}</option>`).join("");
}

function renderBulkBar() {
  const n = state.selected.size;
  const bar = $("#bulk-bar");
  bar.classList.toggle("hidden", !n);
  if (!n) { bar.innerHTML = ""; return; }
  bar.innerHTML = `<b>${n} 件を選択中</b>
    <select id="bulk-folder" class="inline-select"><option value="__">移動先のフォルダ…</option>${folderOptions(null)}</select>
    <button class="btn small" data-bulk="move">移動</button>
    <select id="bulk-sens" class="inline-select"><option value="">区分を変える…</option><option value="internal">社外秘に</option><option value="public">公開に</option></select>
    <button class="btn small" data-bulk="ask">この ${n} 件に質問</button>
    <button class="btn small" data-bulk="reindex">再処理</button>
    <button class="btn small danger" data-bulk="delete">削除</button>
    <button class="btn small" data-bulk="clear">選択を解除</button>`;
  $$("#bulk-bar [data-bulk]").forEach((el) => el.addEventListener("click", () => bulk(el.dataset.bulk)));
  $("#bulk-sens").addEventListener("change", () => { if ($("#bulk-sens").value) bulk("sensitivity"); });
}

async function bulk(action) {
  const ids = [...state.selected];
  if (action === "clear") { state.selected.clear(); renderDocTable(); return; }
  if (action === "ask") { showTab("chat"); await newChat({ doc_ids: ids }); return; }
  const body = { doc_ids: ids, action };
  if (action === "move") {
    const v = $("#bulk-folder").value;
    if (v === "__") { alert("移動先のフォルダを選んでください（左のフォルダへドラッグしても移動できます）"); return; }
    body.folder = v;
  }
  if (action === "sensitivity") body.sensitivity = $("#bulk-sens").value;
  if (action === "delete" && !confirm(`${ids.length} 件の文書を削除しますか？（原本と索引を削除します）`)) return;
  if (action === "reindex" && !confirm(`${ids.length} 件をテキスト抽出から処理し直します。よろしいですか？`)) return;
  const r = await api("/api/docs/bulk", body);
  if (r.error) { alert(r.error); return; }
  if (action === "delete" || action === "move") state.selected.clear();
  toast({ move: `${ids.length} 件を移動しました`, sensitivity: "区分を変えました（埋め込みを作り直します）",
    reindex: "再処理を始めました", delete: `${ids.length} 件を削除しました` }[action]);
  loadDocs(); pollStatus();
}

// アップロード（ファイル・フォルダごと）
async function filesFromDrop(dt) {
  const entries = [...(dt.items || [])].map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files].map((file) => ({ file, rel: "" }));
  const out = [];
  async function walk(entry, prefix) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, rel: prefix });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const dir = prefix ? `${prefix}/${entry.name}` : entry.name;
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e, dir);
      }
    }
  }
  for (const e of entries) await walk(e, "");
  return out;
}

async function uploadEntries(entries, baseFolder) {
  const log = $("#upload-log");
  const base = (baseFolder ?? $("#up-folder").value).trim().replace(/^\/+|\/+$/g, "");
  const sens = $("#up-sens").value;
  const tags = $("#up-tags").value.trim();
  const ok = entries.filter(({ file }) => !state.supported.length
    || state.supported.includes((file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase()));
  const skipped = entries.length - ok.length;
  if (skipped) {
    const line = document.createElement("div");
    line.textContent = `ℹ 未対応の形式・隠しファイル ${skipped} 件は取り込みませんでした`;
    log.prepend(line);
  }
  for (const { file, rel } of ok) {
    if (file.name.startsWith(".") || file.name.startsWith("~$")) continue;
    const folder = [base, rel].filter(Boolean).join("/");
    const line = document.createElement("div");
    line.textContent = `⏫ ${folder ? folder + "/" : ""}${file.name} を送信中…`;
    log.prepend(line);
    const qs = new URLSearchParams({ name: file.name, folder, sensitivity: sens, tags });
    try {
      const res = await fetch(`/api/docs/upload?${qs}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
      });
      const r = await res.json();
      line.textContent = r.error ? `❌ ${file.name}: ${r.error}`
        : r.created ? `✅ ${folder ? "📁 " + folder + " / " : ""}${file.name}: 登録しました（取り込み処理を開始）`
          : `ℹ ${file.name}: 同じ内容の文書が登録済みです（${r.doc.title}）`;
    } catch (e) {
      line.textContent = `❌ ${file.name}: ${e.message}`;
    }
  }
  loadDocs(); pollStatus();
}

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (ev) => {
  if (![...ev.dataTransfer.types].includes("Files")) return;
  ev.preventDefault(); dz.classList.add("drag");
}));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (ev) => { ev.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", async (ev) => {
  if (!ev.dataTransfer.files.length) return;
  uploadEntries(await filesFromDrop(ev.dataTransfer));
});
$("#file-input").addEventListener("change", (ev) => {
  uploadEntries([...ev.target.files].map((file) => ({ file, rel: "" }))); ev.target.value = "";
});
$("#dir-input").addEventListener("change", (ev) => {
  const entries = [...ev.target.files].map((file) => {
    const parts = (file.webkitRelativePath || file.name).split("/");
    return { file, rel: parts.slice(0, -1).join("/") };
  });
  uploadEntries(entries); ev.target.value = "";
});
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

// 文書の情報（編集・読み込み結果）
let editing = null;
let readingsTimer = null;
function editDoc(id) {
  const d = state.docs.find((x) => x.id === id);
  if (!d) return;
  editing = d;
  $("#doc-modal-body").innerHTML = `
    <label class="field-label">タイトル</label><input type="text" id="dm-title" value="${esc(d.title)}">
    <div class="two">
      <div><label class="field-label">フォルダ</label><select id="dm-folder">${folderOptions(d.folder)}</select></div>
      <div><label class="field-label">タグ（カンマ区切り）</label><input type="text" id="dm-tags" value="${esc((d.tags || []).join(", "))}"></div>
    </div>
    <label class="field-label">区分</label>
    <select id="dm-sens"><option value="internal">社外秘（ローカル LLM のみで処理）</option>
      <option value="public">公開（クラウド LLM でも処理可）</option></select>
    <div class="muted top-space">${esc(d.filename)} ・ ${fmtSize(d.size)} ・ ${d.chars.toLocaleString()} 文字 ・ 登録 ${fmtDate(d.created_at)}</div>
    ${(d.warnings || []).length ? `<label class="field-label">警告</label>${d.warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}` : ""}
    ${d.error ? `<div class="warn-line" style="color:var(--ng)">❌ ${esc(d.error)}</div>` : ""}
    <h3 class="sec-title">読み込み結果 <span class="muted">要約・グラフ・埋め込みを、どの LLM で作ったか。LLM ごとに残り、使うものを選べます</span></h3>
    <div id="dm-readings"><p class="hint">読み込み中…</p></div>
    <h3 class="sec-title">別の LLM・方式で読み込む <span class="muted">今の結果は残り、読み込み終わると新しい結果に切り替わります（上の一覧で戻せます）</span></h3>
    <div class="read-form">
      <label class="mini">LLM <select id="rd-profile" class="inline-select"></select></label>
      <label class="mini">モデル <input type="text" id="rd-model" class="inline-input" placeholder="空なら設定どおり"></label>
      <label class="mini">グラフの作り方 <select id="rd-mode" class="inline-select">
        <option value="standard">標準</option><option value="fast">高速（説明なし）</option><option value="terms">語の共起（LLM なし）</option></select></label>
      <label class="mini"><input type="checkbox" id="rd-summary" checked> 要約</label>
      <label class="mini"><input type="checkbox" id="rd-graph" checked> グラフ</label>
      <button class="btn small primary" id="rd-run">読み込む</button>
      <span class="hint" id="rd-msg"></span>
    </div>`;
  $("#dm-sens").value = d.sensitivity;
  const prof = $("#rd-profile");
  const st = state.status ? state.status.profiles : {};
  prof.innerHTML = `<option value="local">ローカル（${esc((st.local && st.local.model) || "未設定")}）</option>`
    + (d.sensitivity === "public" ? `<option value="cloud">クラウド（${esc((st.cloud && st.cloud.model) || "未設定")}）</option>` : "");
  $("#rd-mode").value = (state.config && state.config.rag && state.config.rag.graph_mode) || "standard";
  $("#rd-run").addEventListener("click", readWithLlm);
  $("#doc-modal").classList.remove("hidden");
  loadReadings();
  clearInterval(readingsTimer);
  readingsTimer = setInterval(() => {
    if ($("#doc-modal").classList.contains("hidden")) { clearInterval(readingsTimer); return; }
    const doc = state.docs.find((x) => editing && x.id === editing.id);
    if (doc && (doc.stage || doc.status !== "ready")) loadReadings();
  }, 3000);
}

async function readWithLlm() {
  const stages = [];
  if ($("#rd-summary").checked) stages.push("summary");
  if ($("#rd-graph").checked) stages.push("graph");
  const r = await api(`/api/docs/${editing.id}/read`, {
    profile: $("#rd-profile").value, model: $("#rd-model").value.trim(), graph_mode: $("#rd-mode").value, stages,
  });
  $("#rd-msg").textContent = r.error ? `❌ ${r.error}` : "✅ 読み込みを始めました（文書一覧で進み具合が見えます）";
  if (!r.error) { pollStatus(); setTimeout(() => loadReadings(), 800); }
}

async function loadReadings() {
  if (!editing) return;
  const box = $("#dm-readings");
  const r = await api(`/api/docs/${editing.id}/readings`);
  if (r.error) { box.innerHTML = `<p class="hint">${esc(r.error)}</p>`; return; }
  const embedCur = Object.values(r.embed_spaces || {});
  const groups = ["graph", "summary", "embed"].map((stage) => {
    const items = r.readings.filter((x) => x.stage === stage);
    if (!items.length) return `<div class="rd-group"><div class="rd-stage">${STAGE_NAME[stage]}</div><p class="hint">まだありません</p></div>`;
    return `<div class="rd-group"><div class="rd-stage">${STAGE_NAME[stage]}</div>${items.map((x) => {
      const c = x.chunks || {};
      const total = (x.data && x.data.chunks_total) || 0;
      let stats = "";
      if (stage === "graph") {
        stats = `エンティティ ${x.entities ?? 0}・関係 ${x.relations ?? 0} ・ チャンク ${c.done || 0}/${total || "?"}`
          + `${(c.failed || 0) + (c.error || 0) ? `（失敗 ${(c.failed || 0) + (c.error || 0)}）` : ""}`;
      } else if (stage === "summary") {
        stats = esc(((x.data && x.data.summary) || "").slice(0, 60)) + "…";
      } else {
        stats = `${(x.data && x.data.dims) || "?"} 次元`
          + (embedCur.includes(x.llm_key) ? "" : "・<span class=\"warn-text\">設定中の埋め込みモデルと違うため検索には使いません</span>");
      }
      const canUse = !x.active && stage !== "embed" && ["done", "partial", "paused"].includes(x.status);
      const canResume = stage === "graph" && ["paused", "partial", "error"].includes(x.status);
      const canRetry = stage === "graph" && ((c.failed || 0) + (c.error || 0)) > 0 && x.status !== "running";
      return `<div class="rd-item ${x.active ? "active" : ""}">
        <div class="rd-main"><span class="rd-mark">${x.active ? "● 使用中" : "○"}</span>
          <b>${esc(x.label)}</b> <span class="badge ${x.status === "done" ? "ok" : x.status === "running" ? "busy" : "ng"}">${READING_STATUS[x.status] || x.status}</span>
          ${stage === "graph" && x.mode ? `<span class="badge">${MODE_LABEL[x.mode] || x.mode}</span>` : ""}
          <span class="muted">${fmtDate(x.updated_at)}${x.seconds ? `・${Math.round(x.seconds)} 秒` : ""}</span></div>
        <div class="rd-sub">${stats}</div>
        ${(x.warnings || []).map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}
        <div class="rd-acts">
          ${canUse ? `<button class="btn small" data-act="activate" data-rid="${x.id}">これを使う</button>` : ""}
          ${canResume ? `<button class="btn small" data-act="resume" data-rid="${x.id}">続きから再開</button>` : ""}
          ${canRetry ? `<button class="btn small" data-act="retry" data-rid="${x.id}">失敗したチャンクをやり直す</button>` : ""}
          ${stage === "graph" ? `<button class="btn small" data-act="log" data-rid="${x.id}">チャンクごとの記録</button>` : ""}
          <button class="btn small danger" data-act="delete" data-rid="${x.id}">削除</button>
        </div>
        <div class="rd-log hidden" id="log-${x.id}"></div>
      </div>`;
    }).join("")}</div>`;
  }).join("");
  const open = $$(".rd-log:not(.hidden)", box).map((el) => el.id);
  box.innerHTML = groups;
  $$("[data-act]", box).forEach((el) => el.addEventListener("click", () => readingAction(el.dataset.act, el.dataset.rid)));
  for (const id of open) { const el = document.getElementById(id); if (el) showChunkLog(id.slice(4)); }
}

async function showChunkLog(rid) {
  const el = $(`#log-${rid}`);
  const r = await api(`/api/docs/${editing.id}/readings?log=${rid}`);
  const rows = (r.log || []).map((l) => `<tr class="st-${l.status}"><td>${esc(l.locator || l.chunk_id)}</td>
      <td>${{ done: "✅", failed: "❌", error: "⚠" }[l.status] || ""} ${esc(l.status)}</td>
      <td class="num">${l.entities}</td><td class="num">${l.relations}</td><td class="num">${l.seconds}</td>
      <td class="num">${l.attempts}</td><td class="reason">${esc(l.reason || l.finish || "")}</td></tr>`).join("");
  el.innerHTML = rows ? `<table class="log-table"><tr><th>箇所</th><th>結果</th><th class="num">実体</th><th class="num">関係</th><th class="num">秒</th><th class="num">試行</th><th>理由・終了</th></tr>${rows}</table>`
    : '<p class="hint">まだ記録がありません。</p>';
  el.classList.remove("hidden");
}

async function readingAction(act, rid) {
  if (act === "log") {
    const el = $(`#log-${rid}`);
    if (!el.classList.contains("hidden")) { el.classList.add("hidden"); return; }
    showChunkLog(rid);
    return;
  }
  if (act === "delete" && !confirm("この読み込み結果を削除しますか？（使用中なら、残りの新しい結果に切り替わります）")) return;
  let r;
  if (act === "activate" || act === "delete") {
    r = await api(`/api/docs/${editing.id}/readings/${rid}/${act}`, {});
  } else {
    r = await api(`/api/docs/${editing.id}/reindex`, { reading_id: rid, policy: act === "resume" ? "resume" : "retry_failed" });
  }
  if (r.error) { alert(r.error); return; }
  toast({ activate: "使う読み込み結果を切り替えました", delete: "読み込み結果を削除しました",
    resume: "続きから再開します", retry: "失敗したチャンクをやり直します" }[act]);
  await loadDocs(true);
  loadReadings();
  pollStatus();
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

const GRAPH_VIEW_TEXT = {
  entities: { subtitle: "取り込み時に文書から抽出したエンティティと関係", unit: "エンティティ", search: "エンティティを検索（Enter で移動）" },
  folders: { subtitle: "フォルダを丸（大きさ＝エンティティの数）、共通のエンティティを線（太さ＝共通の度合い）にした全体図", unit: "フォルダ", search: "フォルダを検索（Enter で移動）" },
  docs: { subtitle: "文書を丸（大きさ＝エンティティの数・色＝フォルダ）、共通のエンティティを線にした全体図", unit: "文書", search: "文書を検索（Enter で移動）" },
};
const COLOR_NAME = { community: "コミュニティ", type: "種別", folder: "フォルダ" };

function graphViewMode() { return state.graphView || "entities"; }

function syncGraphControls() {
  const view = graphViewMode();
  $$("#g-view [data-view]").forEach((b) => {
    b.classList.toggle("on", b.dataset.view === view);
    b.setAttribute("aria-checked", b.dataset.view === view ? "true" : "false");
  });
  $("#g-subtitle").textContent = GRAPH_VIEW_TEXT[view].subtitle;
  $("#g-search").placeholder = GRAPH_VIEW_TEXT[view].search;
  $("#g-doc").classList.toggle("hidden", view !== "entities");
  const color = $("#g-color");
  if (view !== "entities") { color.value = "folder"; color.disabled = true; } else { color.disabled = false; }
  $("#g-isolated").closest("label").classList.toggle("hidden", view !== "entities");
}

async function loadGraph() {
  if (!state.docs.length) await loadDocs(true);
  const view = graphViewMode();
  syncGraphControls();
  const fsel = $("#g-folder");
  const fcur = fsel.value;
  fsel.innerHTML = '<option value="">すべてのフォルダ</option>' + state.folders.filter((f) => f.path)
    .map((f) => `<option value="${esc(f.path)}">${"　".repeat(f.depth - 1)}📁 ${esc(f.name)}（${f.count}）</option>`).join("");
  fsel.value = [...fsel.options].some((o) => o.value === fcur) ? fcur : "";
  const sel = $("#g-doc");
  const cur = sel.value;
  const folder = fsel.value;
  sel.innerHTML = '<option value="">すべての文書</option>' + state.docs
    .filter((d) => d.flags && d.flags.graph)
    .filter((d) => !folder || d.folder === folder || d.folder.startsWith(folder + "/"))
    .map((d) => `<option value="${d.id}">${esc(d.title.slice(0, 40))}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";
  const gv = graphView();
  gv.resize();
  $("#g-hint").textContent = "読み込み中…";
  const qs = new URLSearchParams({ limit: $("#g-limit").value, folder, view, depth: $("#g-depth").value,
    doc: view === "entities" ? sel.value : "" });
  const data = await api(`/api/graph?${qs}`);
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
  gv.setColorMode(view === "entities" ? $("#g-color").value : "folder");
  gv.setShowIsolated(view !== "entities" || $("#g-isolated").checked);
  gv.setData(data);
  updateGraphHint(); renderLegend();
}

function updateGraphHint() {
  if (!gdata) return;
  const st = graphView().stats();
  const view = graphViewMode();
  let note;
  if (view === "entities") {
    note = `エンティティ ${gdata.total_nodes} / 関係 ${gdata.total_edges} ・ 表示 ${st.shown} ノード`;
    if (gdata.truncated) note += "（関係の多い順に上限まで）";
    if (st.isolated && !$("#g-isolated").checked) note += ` ・ 孤立ノード ${st.isolated} 件は非表示`;
    if ($("#g-color").value === "folder") note += " ・ 点線の輪は複数のフォルダに出てくるエンティティ";
    note += "。ホイールで拡大縮小、背景ドラッグで移動、クリックで詳細。";
  } else {
    const unit = GRAPH_VIEW_TEXT[view].unit;
    note = `${unit} ${gdata.total_nodes} ・ 共通のエンティティでつながる組 ${gdata.total_edges}（各${unit}の強い順に 8 本まで表示）`
      + `${gdata.truncated ? `・エンティティの多い順に ${st.shown} 件` : ""}。`
      + (view === "folders" ? "丸をクリックすると共通のエンティティ、「中を見る」でそのフォルダのエンティティのグラフへ。"
        : "丸をクリックすると共通のエンティティ、「この文書のグラフ」でその文書のエンティティへ。");
  }
  $("#g-hint").textContent = note;
}

function renderLegend() {
  const items = graphView().legend();
  const mode = graphViewMode() === "entities" ? $("#g-color").value : "folder";
  $("#g-legend").innerHTML = `<div class="muted">凡例（${COLOR_NAME[mode] || mode}・クリックで強調）</div>`
    + items.map((it) => `<div class="legend-row" data-key="${esc(it.key)}">
      <span class="sw" style="background:${it.color}"></span>${esc(it.label.slice(0, 30))}<span class="cnt">${it.count}</span></div>`).join("");
  $$("#g-legend [data-key]").forEach((el) => el.addEventListener("click", () => {
    if (mode === "community") graphView().highlightCommunity(el.dataset.key);
    else graphView().highlightKey(el.dataset.key);
  }));
}

function drillInto(view, folder, docId) {
  state.graphView = view;
  if (folder !== undefined) {
    const fsel = $("#g-folder");
    if (![...fsel.options].some((o) => o.value === folder)) {
      fsel.insertAdjacentHTML("beforeend", `<option value="${esc(folder)}">📁 ${esc(folder)}</option>`);
    }
    fsel.value = folder;
  }
  loadGraph().then(() => { if (docId) { $("#g-doc").value = docId; loadGraph(); } });
}

function renderGraphDetail(node) {
  const box = $("#g-detail");
  if (!node) {
    box.innerHTML = graphViewMode() === "entities"
      ? '<p class="hint">ノードをクリックすると、説明・関係・出典文書が表示されます。</p>'
      : `<p class="hint">${GRAPH_VIEW_TEXT[graphViewMode()].unit}をクリックすると、主なエンティティ・中の文書・つながる${GRAPH_VIEW_TEXT[graphViewMode()].unit}（共通のエンティティ）が表示されます。</p>`;
    return;
  }
  const rels = graphView().neighbors(node.id);
  const docs = (node.doc_ids || []).map((id, i) =>
    `<li><a href="#" data-doc="${esc(id)}">${esc((node.docs || [])[i] || id)}</a></li>`).join("");
  if (node.kind === "folder" || node.kind === "doc") {
    const isFolder = node.kind === "folder";
    const unit = isFolder ? "フォルダ" : "文書";
    box.innerHTML = `<div class="detail-title">${isFolder ? "📁" : "📄"} ${esc(node.name)}</div>
      <div class="muted">${isFolder ? `文書 ${node.doc_count} 件 ・ ` : `📁 ${esc(node.folder || "")} ・ `}エンティティ ${node.size} ・ つながる${unit} ${rels.length}</div>
      ${(node.entities || []).length ? `<div class="muted top-space">主なエンティティ</div><div class="ent-chips">${node.entities.map((e) => `<span class="chip">${esc(e)}</span>`).join("")}</div>` : ""}
      ${isFolder && docs ? `<div class="muted top-space">文書${node.doc_count > 8 ? `（${node.doc_count} 件中 8 件）` : ""}</div><ul>${docs}</ul>` : ""}
      <div class="row wrap gap-s top-space">
        ${isFolder ? `<button class="btn small primary" id="g-into">中を見る（エンティティのグラフ）</button>
          <button class="btn small" id="g-sub">中のフォルダの全体図</button>`
          : `<button class="btn small primary" id="g-into">この文書のグラフ</button><button class="btn small" id="g-open">文書を開く</button>`}
        <button class="btn small" id="g-ask">ここに質問</button>
      </div>
      <div class="muted top-space">つながる${unit}（共通のエンティティの多い順）</div>
      ${rels.slice(0, 40).map((r) => `<div class="detail-rel" data-id="${esc(r.node.id)}">↔ <b>${esc(r.node.name)}</b>
        <span class="muted">共通 ${r.edge.shared ?? ""} 件</span><br><span class="muted">${esc((r.edge.description || "").slice(0, 160))}</span></div>`).join("")
        || `<p class="hint">共通のエンティティでつながる${unit}はありません。</p>`}`;
    $$("[data-id]", box).forEach((el) => el.addEventListener("click", () => graphView().focus(el.dataset.id)));
    $$("[data-doc]", box).forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); openDoc(el.dataset.doc); }));
    $("#g-into").addEventListener("click", () => (isFolder ? drillInto("entities", node.key) : drillInto("entities", undefined, node.key)));
    if (isFolder) {
      $("#g-sub").addEventListener("click", () => { $("#g-depth").value = "1"; drillInto("folders", node.key); });
    } else {
      $("#g-open").addEventListener("click", () => openDoc(node.key));
    }
    $("#g-ask").addEventListener("click", async () => {
      showTab("chat");
      await newChat(isFolder ? { folders: [node.key] } : { doc_ids: [node.key] });
      $("#q").focus();
    });
    return;
  }
  const folders = (node.folders || []).length ? `<div class="muted">📁 ${esc(node.folders.slice(0, 4).join("・"))}${node.folders.length > 4 ? ` ほか ${node.folders.length - 4}` : ""}</div>` : "";
  box.innerHTML = `<div class="detail-title">${esc(node.name)}</div>
    <div class="muted">${esc(node.type || "")}${node.community ? " ・ " + esc(node.community) : ""} ・ 関係 ${rels.length}</div>
    ${folders}
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
    await newChat({ doc_ids: node.doc_ids || [] });
    $("#q").value = `${node.name}について、関係する人・組織・経緯も含めて教えて`;
    $("#q").focus();
  });
}

$$("#g-view [data-view]").forEach((b) => b.addEventListener("click", () => {
  if (graphViewMode() === b.dataset.view) return;
  state.graphView = b.dataset.view;
  loadGraph();
}));
$("#g-reload").addEventListener("click", loadGraph);
$("#g-folder").addEventListener("change", () => { $("#g-doc").value = ""; loadGraph(); });
$("#g-doc").addEventListener("change", loadGraph);
$("#g-depth").addEventListener("change", loadGraph);
$("#g-limit").addEventListener("change", loadGraph);
$("#g-isolated").addEventListener("change", () => {
  graphView().setShowIsolated($("#g-isolated").checked); updateGraphHint(); renderLegend();
});
$("#g-color").addEventListener("change", () => { graphView().setColorMode($("#g-color").value); updateGraphHint(); renderLegend(); });
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
      <p class="hint">取り込みでは、タイムアウト・接続断・HTTP 5xx のときに待ち時間を延ばして同じチャンクを最大 3 回やり直します。</p>
      <label class="check"><input type="checkbox" id="${key("use_proxy")}"> プロキシを使う（OFF なら環境変数のプロキシも無視して直結）</label>
      <label class="field-label">Proxy URL（空なら環境変数 HTTPS_PROXY）</label><input type="text" id="${key("proxy_url")}">
    </details>
    <div class="row gap top-space wrap"><button class="btn" data-test="${name}">接続テスト</button>
      <span class="badge ${p.chat_ready ? "ok" : "ng"}">${p.chat_ready ? "チャット設定済み" : "チャット未設定"}</span>
      <span class="badge ${p.embed_ready ? "ok" : ""}">${p.embed_ready ? "埋め込みあり" : "埋め込みなし"}</span></div>
    <div class="test-result" id="test-${name}"></div>`;
}

const RAG_BOOLS = ["graph", "doc_summary", "router", "corrective", "rerank", "decision", "decision_choice"];
const RAG_NUMS = ["top_k", "chunk_size", "max_graph_chunks", "decision_beam", "decision_budget"];
const PROFILE_FIELDS = ["provider", "base_url", "model", "ingest_model", "api_version", "effort", "embed_model",
  "embed_provider", "embed_base_url", "max_tokens", "context_window", "timeout", "proxy_url"];
const WHISPER_FIELDS = ["provider", "location", "url", "model", "api_version", "timeout", "proxy_url"];

function syncProviderUi(name) {
  const prov = $(`#pf-${name}-provider`).value;
  $$(`[data-only="${name}-azure"]`).forEach((el) => el.classList.toggle("hidden", prov !== "azure"));
  $$(`[data-only="${name}-anthropic"]`).forEach((el) => el.classList.toggle("hidden", prov !== "anthropic"));
  const hint = { openai: "例: http://127.0.0.1:11434/v1", azure: "例: https://<リソース名>.openai.azure.com",
    anthropic: "空で既定（API キーは環境変数 ANTHROPIC_API_KEY でも可）" }[prov];
  $(`[data-hint="${name}-base"]`).textContent = hint;
  $(`[data-label="${name}-model"]`).textContent = prov === "azure" ? "デプロイ名" : "モデル";
}

function browserSpeechNote() {
  const SR = browserRecognizer();
  if (!SR) return "このブラウザには音声認識がありません（Chrome / Edge で使えます）。Whisper なら他のブラウザでも使えます。";
  return browserOnDevice()
    ? "このブラウザは「端末内だけで認識」を指定できます（音声は外部へ送られません。言語パックが無い PC では開始時にエラーになります）。"
    : "このブラウザの音声認識は、音声を Google / Microsoft のサーバへ送ります（「音声をクラウドへ送ってよい」が必要）。";
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
  for (const k of RAG_BOOLS) $(`#r-${k}`).checked = !!cfg.rag[k];
  for (const k of RAG_NUMS) $(`#r-${k}`).value = cfg.rag[k];
  $("#r-default_strategy").value = cfg.rag.default_strategy || "auto";
  const cat = cfg.rag_methods;
  if (cat) {
    $("#r-default-methods").textContent = "「選んで使う」の既定の組み合わせ: "
      + (cfg.rag.custom_methods || []).map((m) => (cat.methods[m] || {}).label || m).join("・")
      + "（相談画面の「この方式を既定にする」で変えられます）";
  }
  const gm = cfg.rag.graph_mode || "standard";
  $$('#graph-mode-cards input[name="graph_mode"]').forEach((el) => { el.checked = el.value === gm; });
  syncModeCards();
  $("#r-decision_judge").value = cfg.rag.decision_judge || "llm";
  const da = cfg.decision_api;
  $("#da-provider").value = da.provider;
  $("#da-timeout").value = da.timeout;
  for (const prov of JUDGE_PROVIDERS) {
    const sub = da[prov] || {};
    for (const k of ["endpoint", "model", "proxy_url", "api_version"]) {
      const el = $(`#da-${prov}-${k}`);
      if (el) el.value = sub[k] || "";
    }
    $(`#da-${prov}-use_proxy`).checked = !!sub.use_proxy;
    const allow = $(`#da-${prov}-allow_internal`);
    if (allow) allow.checked = !!sub.allow_internal;
    $(`#da-${prov}-api_key`).value = "";
    $(`#da-${prov}-api_key`).placeholder = sub.has_api_key ? "（設定済み。変更時のみ入力）" : "（未設定）";
  }
  showJudgeProvider();
  $("#da-test-result").textContent = "";
  $("#judge-box").open = cfg.rag.decision_judge === "api";
  const sp = cfg.speech || { whisper: {} };
  $("#sp-engine").value = sp.engine || "auto";
  $("#sp-language").value = sp.language || "ja";
  $("#sp-allow_cloud").checked = !!sp.allow_cloud;
  for (const k of WHISPER_FIELDS) { const el = $(`#sp-w-${k}`); if (el) el.value = sp.whisper[k] ?? ""; }
  $("#sp-w-use_proxy").checked = !!sp.whisper.use_proxy;
  $("#sp-w-api_key").value = "";
  $("#sp-w-api_key").placeholder = sp.whisper.has_api_key ? "（設定済み。変更時のみ入力）" : "（不要なら空）";
  $("#sp-whisper-box").open = sp.engine === "whisper" || !!sp.whisper.url;
  $("#sp-test-result").textContent = "";
  $("#sp-browser-note").textContent = browserSpeechNote();
  $("#up-sens").value = cfg.policy.default_sensitivity;
}

function syncModeCards() {
  $$("#graph-mode-cards .mode-card").forEach((el) => el.classList.toggle("on", el.querySelector("input").checked));
}
$$('#graph-mode-cards input[name="graph_mode"]').forEach((el) => el.addEventListener("change", syncModeCards));

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
  const rag = { decision_judge: $("#r-decision_judge").value, default_strategy: $("#r-default_strategy").value };
  for (const k of RAG_BOOLS) rag[k] = $(`#r-${k}`).checked;
  for (const k of RAG_NUMS) rag[k] = Number($(`#r-${k}`).value);
  const gm = $('#graph-mode-cards input[name="graph_mode"]:checked');
  if (gm) rag.graph_mode = gm.value;
  const decision_api = { provider: $("#da-provider").value, timeout: Number($("#da-timeout").value) || 60 };
  for (const prov of JUDGE_PROVIDERS) {
    const sub = { use_proxy: $(`#da-${prov}-use_proxy`).checked };
    for (const k of ["endpoint", "model", "proxy_url", "api_version"]) {
      const el = $(`#da-${prov}-${k}`);
      if (el) sub[k] = el.value;
    }
    const allow = $(`#da-${prov}-allow_internal`);
    if (allow) sub.allow_internal = allow.checked;
    if ($(`#da-${prov}-api_key`).value) sub.api_key = $(`#da-${prov}-api_key`).value;
    decision_api[prov] = sub;
  }
  const whisper = { use_proxy: $("#sp-w-use_proxy").checked };
  for (const k of WHISPER_FIELDS) whisper[k] = k === "timeout" ? Number($(`#sp-w-${k}`).value) || 120 : $(`#sp-w-${k}`).value;
  if ($("#sp-w-api_key").value) whisper.api_key = $("#sp-w-api_key").value;
  const speechCfg = { engine: $("#sp-engine").value, language: $("#sp-language").value || "ja",
    allow_cloud: $("#sp-allow_cloud").checked, whisper };
  const cfg = await api("/api/config", { profiles, policy, rag, decision_api, speech: speechCfg });
  state.config = cfg;
  if (!quiet) { $("#s-msg").textContent = "✅ 保存しました"; loadSettings(); }
  pollStatus();
}
$("#s-save").addEventListener("click", () => saveSettings(false));

// 判定 API（Decisions API）: 接続先ごとの入力欄を切り替え、保存してから接続テスト
function showJudgeProvider() {
  for (const el of document.querySelectorAll(".judge-prov")) el.hidden = el.dataset.prov !== $("#da-provider").value;
}
$("#da-provider").addEventListener("change", showJudgeProvider);
$("#da-test").addEventListener("click", async () => {
  const out = $("#da-test-result");
  out.textContent = "保存して判定中…";
  await saveSettings(true);
  const r = await api("/api/config/test", { judge_api: true });
  out.textContent = `${r.ok ? "✅" : "❌"} ${r.message || r.error || ""}${r.url ? `（送り先: ${r.url}）` : ""}`;
});
$("#sp-test").addEventListener("click", async () => {
  const out = $("#sp-test-result");
  out.textContent = "保存してテスト音を送信中…";
  await saveSettings(true);
  const r = await api("/api/config/test", { speech: true });
  out.textContent = `${r.ok ? "✅" : "❌"} ${r.message || r.error || ""}${r.url ? `（送り先: ${r.url}）` : ""}`;
});
$("#s-reindex").addEventListener("click", async () => {
  if (!confirm("全文書をテキスト抽出から処理し直します（LLM を多く呼びます）。よろしいですか？")) return;
  await saveSettings(true);
  await api("/api/docs/reindex-all", { stages: ["extract", "embed", "graph", "summary"] });
  $("#s-msg").textContent = "再処理を開始しました（文書タブで進捗を確認できます）";
  pollStatus();
});

// ---------------------------------------------------------------- init

(async function init() {
  state.collapsed = new Set(store("docvault.collapsed") || []);
  state.config = await api("/api/config");
  if (state.config.policy) $("#up-sens").value = state.config.policy.default_sensitivity;
  await Promise.all([loadDocs(true), loadChats(), pollStatus()]);
  refreshChatHead();
  if (state.chats[0]) await openChat(state.chats[0].id);
  else renderMessages();
  setInterval(pollStatus, 2500);
})();
