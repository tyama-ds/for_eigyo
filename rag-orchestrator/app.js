/* RAG Orchestrator — フロントエンド（依存なしの素の JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const ENGINE_COLORS = {
  graphrag: "#a78bfa", vector: "#5aa2ff", bm25: "#34d399", hybrid: "#f59e0b",
  decision: "#f9a8d4", combo: "#a3e635",
  "nano-graphrag": "#f472b6", lightrag: "#f472b6", minirag: "#fb7185",
  hipporag: "#22d3ee", "rag-anything": "#c084fc",
};
const state = {
  engines: [],
  corpusChunks: 0,    // チャンク数（GraphRAG の構築方式ごとの LLM 呼び出し回数の目安）
  corpus: { rev: 0, docs: [] },
  queryJob: null,     // ポーリング中の query ジョブ ID
  ingestJob: null,
  pollTimer: null,
};

// ---------------------------------------------------------------- utils

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* 最小限の Markdown 風レンダリング（見出し・箇条書き・太字・段落のみ。入力は必ずエスケープ） */
function renderText(text) {
  const lines = esc(text).split("\n");
  const out = [];
  let inList = false;
  for (const line of lines) {
    const m = line.match(/^(#{1,3})\s+(.*)/);
    if (m) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h3>${m[2]}</h3>`);
      continue;
    }
    if (/^\s*[-・*]\s+/.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${line.replace(/^\s*[-・*]\s+/, "")}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("\n")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[(S\d+|C\d+)\]/g, '<span class="cite">$1</span>');
}

function engineColor(id) { return ENGINE_COLORS[id] || "#8b98b8"; }
function engineName(id) {
  const eng = state.engines.find((e) => e.id === id);
  return eng ? eng.name : id;
}
function fmtSec(s) { return s == null ? "" : `${Number(s).toFixed(1)}秒`; }

/* 方式の違い（GraphRAG の 標準 / 高速 / 語の共起（LLM なし））のバッジ。v は {kind, label, title} */
function variantBadge(v) {
  if (!v || !v.label) return "";
  return `<span class="badge variant v-${esc(v.kind)}" title="${esc(v.title || "")}">${esc(v.label)}</span>`;
}
function variantText(v) { return v && v.label ? `・${v.label}` : ""; }

/* インデックス・結果の統計の表示名（無いキーはそのまま出す） */
const STAT_LABELS = {
  extract_mode: "抽出方式", summary_mode: "コミュニティ要約", llm_calls: "LLM 呼び出し", seconds: "所要",
  chunks: "チャンク", docs: "文書", entities: "エンティティ", relations: "関係",
  inferred_entities: "補ったエンティティ", communities: "コミュニティ",
  summarized_communities: "LLM 要約済みコミュニティ", extract_parse_failures: "抽出の解析失敗",
  extract_retries: "抽出の再試行", extract_salvaged: "途中で切れた出力", has_embeddings: "埋め込み",
  dim: "埋め込み次元", retrieved: "取得", mapped_communities: "map したコミュニティ", points: "ポイント",
  map_parse_failures: "map の解析失敗", lazy_summarized: "質問時に要約", lazy_reused: "保存済みの要約を再利用",
  sections: "節", groups: "文書群", summarized_docs: "要約した文書",
  components: "組み合わせ", skipped: "使えなかった方式", candidates: "統合した候補",
  judge_api_calls: "判定 API", retrieval_seconds: "検索", answer_seconds: "回答生成",
};
function graphModeLabel(kind, id) {
  const g = (state.engines.find((e) => e.id === "graphrag") || {}).graph;
  const list = g ? (kind === "extract" ? g.extract_modes : g.summary_modes) : [];
  const m = list.find((x) => x.id === id);
  return m ? m.label : id;
}
function statsText(stats) {
  return Object.entries(stats || {}).map(([k, v]) => {
    let val = v;
    if (k === "extract_mode") val = graphModeLabel("extract", v);
    else if (k === "summary_mode") val = graphModeLabel("summaries", v);
    else if (k === "seconds" || k === "retrieval_seconds" || k === "answer_seconds") val = fmtSec(v);
    else if (k === "llm_calls" || k === "judge_api_calls") val = `${v} 回`;
    else if (typeof v === "boolean") val = v ? "あり" : "なし";
    return `${STAT_LABELS[k] || k}: ${val}`;
  }).join(" ・ ");
}

/* 実行ログ（構築方式・質問時の要約など）を折りたたみで表示 */
function logsHtml(logs) {
  if (!logs || !logs.length) return "";
  return `<details class="think"><summary>ログ（${logs.length} 件）</summary>` +
    `<div class="think-body">${esc(logs.join("\n"))}</div></details>`;
}

// ---------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    $(`#tab-${btn.dataset.tab}`).classList.remove("hidden");
    if (btn.dataset.tab === "graph") refreshEngines().then(() => { fillGraphEngineSelect(); loadGraph(); });
    if (btn.dataset.tab === "engines" || btn.dataset.tab === "query") refreshEngines();
    if (btn.dataset.tab === "corpus") refreshCorpus();
  });
});

// ---------------------------------------------------------------- settings

const CFG_FIELDS = ["base_url", "model", "embed_model", "embed_base_url",
  "decision_judge", "decision_beam", "decision_budget",
  "context_window", "request_timeout", "max_tokens", "proxy_url"];

async function loadConfig() {
  const cfg = await api("/api/config");
  for (const f of CFG_FIELDS) { const el = $(`#s-${f}`); if (el) el.value = cfg[f] ?? ""; }
  $("#s-use_proxy").checked = !!cfg.use_proxy;
  $("#s-api_key").placeholder = cfg.has_key
    ? "（設定済み。変更する場合のみ入力）" : "（未設定。不要なサーバでは空のまま）";
  $("#s-embed_api_key").placeholder = cfg.has_embed_key ? "（設定済み）" : "（空なら API Key を流用）";
  fillJudgeApi(cfg);
  $("#q-beam").placeholder = `設定の値（${cfg.decision_beam ?? 4}）`;
  updateConnBadge(cfg);
}

function updateConnBadge(cfg) {
  const ok = cfg.base_url && cfg.model;
  $("#conn-status").innerHTML = ok
    ? `<span class="dot ok"></span>LLM: ${esc(cfg.base_url)} / ${esc(cfg.model)}`
    : '<span class="dot ng"></span>LLM 未設定（BM25 は抜粋モードで動作可）';
}

$("#s-save").addEventListener("click", async () => {
  await saveConfig();
  $("#s-msg").textContent = "✅ 保存しました";
});

// 判定 API（Decisions API）: 接続先ごとの入力欄。API キーは空なら変更しない
const JUDGE_PROVIDERS = ["openai", "azure", "openrouter"];
function showJudgeProvider() {
  for (const el of document.querySelectorAll(".judge-prov")) el.hidden = el.dataset.prov !== $("#da-provider").value;
}
function fillJudgeApi(cfg) {
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
    $(`#da-${prov}-api_key`).value = "";
    $(`#da-${prov}-api_key`).placeholder = sub.has_api_key ? "（設定済み。変更する場合のみ入力）" : "（未設定）";
  }
  showJudgeProvider();
  $("#judge-box").open = cfg.decision_judge === "api";
}
function judgeApiBody() {
  const out = { provider: $("#da-provider").value, timeout: Number($("#da-timeout").value) || 60 };
  for (const prov of JUDGE_PROVIDERS) {
    const sub = { use_proxy: $(`#da-${prov}-use_proxy`).checked };
    for (const k of ["endpoint", "model", "proxy_url", "api_version"]) {
      const el = $(`#da-${prov}-${k}`);
      if (el) sub[k] = el.value;
    }
    if ($(`#da-${prov}-api_key`).value) sub.api_key = $(`#da-${prov}-api_key`).value;
    out[prov] = sub;
  }
  return out;
}
$("#da-provider").addEventListener("change", showJudgeProvider);
$("#da-test").addEventListener("click", async () => {
  const out = $("#da-test-result");
  out.textContent = "保存して判定中…";
  await saveConfig();
  const r = await api("/api/config/test", { judge_api: true });
  out.textContent = `${r.ok ? "✅" : "❌"} ${r.message || r.error || ""}${r.url ? `（送り先: ${r.url}）` : ""}`;
});

async function saveConfig() {
  const body = { use_proxy: $("#s-use_proxy").checked, decision_api: judgeApiBody() };
  for (const f of CFG_FIELDS) body[f] = $(`#s-${f}`).value;
  if ($("#s-api_key").value) body.api_key = $("#s-api_key").value;
  if ($("#s-embed_api_key").value) body.embed_api_key = $("#s-embed_api_key").value;
  const cfg = await api("/api/config", body);
  $("#s-api_key").value = ""; $("#s-embed_api_key").value = "";
  fillJudgeApi(cfg);
  updateConnBadge(cfg);
  refreshEngines();
  return cfg;
}

$("#s-test").addEventListener("click", async () => {
  $("#s-msg").textContent = "接続テスト中…";
  const r = await api("/api/config/test", {});
  const chat = r.chat.ok ? `✅ チャット: ${esc(r.chat.message)}` : `❌ チャット: ${esc(r.chat.message)}`;
  const emb = r.embed.ok ? `✅ 埋め込み: ${esc(r.embed.message)}` : `❌ 埋め込み: ${esc(r.embed.message)}`;
  $("#s-msg").innerHTML = `${chat}<br>${emb}`;
});

// DecisionRAG の結果: 判定モデルの表示名と判定 API の使用量
function judgeLabel(stats) {
  if (stats.judge_api) return stats.judge_api;
  return { llm: "LLM", jev: "Jev" }[stats.judge] || stats.judge || "";
}
function apiUsage(stats) {
  const parts = [];
  if (stats.api_cost_usd) parts.push(`$${stats.api_cost_usd}`);
  if (stats.jev_cost_usd) parts.push(`$${stats.jev_cost_usd}`);
  if (stats.api_input_tokens) parts.push(`入力 ${Number(stats.api_input_tokens).toLocaleString()} トークン`);
  if (stats.api_refusals) parts.push(`判定を断られた候補 ${stats.api_refusals} 件`);
  return parts.length ? " ・ 判定 API: " + esc(parts.join("・")) : "";
}

// ---------------------------------------------------------------- engines

async function refreshEngines() {
  const data = await api("/api/engines");
  state.engines = data.engines;
  state.corpusChunks = data.corpus_chunks || 0;
  renderEngineList();
  renderEngineChecks();
}

/* 再描画してもチェック状態を保つ（初回と、前は選べなかったエンジンは既定の選択） */
function checkState(selector) {
  const els = [...document.querySelectorAll(selector)];
  if (!els.length) return null;
  return { checked: new Set(els.filter((el) => el.checked).map((el) => el.value)),
    enabled: new Set(els.filter((el) => !el.disabled).map((el) => el.value)) };
}
function keepChecked(prev, id, fallback) {
  return prev && prev.enabled.has(id) ? prev.checked.has(id) : fallback;
}

function indexBadge(eng) {
  const idx = eng.index || {};
  // 作るのが一瞬のエンジン（組み合わせ RAG）は、無い・古いインデックスを質問時に作る
  if (eng.auto_index && (!idx.built || idx.stale)) return '<span class="badge">インデックスは質問時に自動で作成</span>';
  if (!idx.built) return '<span class="badge">インデックス未構築</span>';
  if (idx.stale) return '<span class="badge warn">インデックスが古い（要再構築）</span>';
  return '<span class="badge ok">インデックス構築済み</span>';
}

/* GraphRAG の構築方式（標準 / 高速 / 超高速）とコミュニティ要約の選択。変更は次回の構築から反映 */
function graphragSettingsHtml(eng) {
  const g = eng.graph;
  if (!g) return "";
  const n = state.corpusChunks;
  const extractCalls = { standard: n, fast: n, terms: 0 };
  const extractCards = g.extract_modes.map((m) => {
    const sel = m.id === g.extract;
    const est = n ? `（このコーパスでは ${extractCalls[m.id]} 回）` : "";
    return `<label class="mode-card ${sel ? "selected" : ""} v-${esc(m.id)}">
      <input type="radio" name="gr-extract" value="${esc(m.id)}" ${sel ? "checked" : ""}>
      <span class="mode-title">${esc(m.label)}</span>
      <span class="mode-desc">${esc(m.desc)}</span>
      <span class="mode-calls">構築時の LLM: ${esc(m.calls)}${esc(est)}</span>
    </label>`;
  }).join("");
  const summaryCards = g.summary_modes.map((m) => {
    const sel = m.id === g.summaries;
    const off = m.id === "ingest" && g.extract !== "standard";   // 高速・超高速では構築時に要約しない
    return `<label class="mode-card ${sel ? "selected" : ""} ${off ? "disabled" : ""}"
        title="${off ? "高速・超高速では構築時にコミュニティごとの LLM 呼び出しをしません（標準のみ）" : ""}">
      <input type="radio" name="gr-summaries" value="${esc(m.id)}" ${sel ? "checked" : ""} ${off ? "disabled" : ""}>
      <span class="mode-title">${esc(m.label)}</span>
      <span class="mode-desc">${esc(m.desc)}</span>
      <span class="mode-calls">構築時の LLM: ${esc(m.calls)}</span>
    </label>`;
  }).join("");
  // 次回の構築の LLM 呼び出しの目安
  let plan = `${extractCalls[g.extract]} 回`;
  if (g.summaries === "ingest") plan += ` ＋ コミュニティ要約 最大 ${g.max_summarized} 回`;
  if (g.extract !== "terms") plan += "（再試行を除く）";
  // 構築済みインデックスの方式と、設定との違い
  const idx = eng.index || {};
  let current = "";
  if (idx.built) {
    const st = idx.stats || {};
    const facts = [st.seconds != null ? `構築 ${fmtSec(st.seconds)}` : "",
      st.llm_calls != null ? `LLM ${st.llm_calls} 回` : ""].filter(Boolean).join("・");
    const same = idx.variant && idx.variant.label === g.variant.label;
    current = `<div class="gr-current">現在のインデックス: ${variantBadge(idx.variant)}
      ${facts ? `<span class="muted">${esc(facts)}</span>` : ""}
      ${same ? "" : `<span class="gr-diff">設定と違う方式です。インデックスを構築し直すと「${esc(g.variant.label)}」になります</span>`}</div>`;
  }
  return `<div class="gr-settings">
    <div class="gr-head">構築方式 <span class="muted">（次回のインデックス構築から反映）</span></div>
    <div class="mode-cards" role="radiogroup" aria-label="GraphRAG の構築方式">${extractCards}</div>
    <div class="gr-head">コミュニティ要約</div>
    <div class="mode-cards" role="radiogroup" aria-label="コミュニティ要約の作り方">${summaryCards}</div>
    <div class="gr-plan">次回の構築: ${variantBadge(g.variant)}
      <span>構築時の LLM 呼び出しの目安 ${esc(plan)}</span></div>
    ${current}
  </div>`;
}

async function saveGraphragMode(key, value) {
  await api("/api/config", { [key]: value });
  await refreshEngines();
}

/* 組み合わせ RAG: 方式が使うインデックス（各エンジンの構築済みのもの）の状態。表示は state.engines から作る */
function comboSourceStatus(m) {
  if (!m.sources.length) return { cls: "ok", text: `✓ ${m.index_note || "構築不要"}` };
  let stale = null;
  for (const s of m.sources) {
    const eng = state.engines.find((e) => e.id === s.id);
    const idx = (eng && eng.index) || {};
    if (idx.built && !idx.stale) {
      const v = idx.variant && idx.variant.label ? `（${idx.variant.label}）` : "";
      return { cls: "ok", text: `✓ ${s.name}${v} の構築済みインデックスを使う` };
    }
    if (idx.built && !stale) stale = s;
  }
  const alt = m.sources.slice(1).map((s) => `。${s.name} のインデックスでも可`).join("");
  if (stale) {
    return { cls: "warn", miss: true,
      text: `⚠ ${stale.name} のインデックスが古い（コーパスを変えた後に未構築）。このままでは質問時に飛ばす（エンジンタブで構築し直す${alt}）` };
  }
  return { cls: "warn", miss: true,
    text: `⚠ ${m.sources[0].name} のインデックスが未構築。このままでは質問時に飛ばす（このタブで先に構築${alt}）` };
}

/* 組み合わせ RAG の設定（統合する方式・LLM での並べ替え）。構築し直し不要で次の質問から反映 */
function comboSettingsHtml(eng) {
  const c = eng.combo;
  if (!c) return "";
  const missing = [];
  const cards = c.components.map((m) => {
    const sel = c.selected.includes(m.id);
    const st = comboSourceStatus(m);
    if (sel && st.miss) missing.push(m.short);
    return `<label class="mode-card combo-card ${sel ? "selected" : ""}">
      <input type="checkbox" name="combo-comp" value="${esc(m.id)}" ${sel ? "checked" : ""}>
      <span class="mode-title">${esc(m.label)}${m.weight !== 1 ? ` <span class="muted">重み ×${esc(m.weight)}</span>` : ""}</span>
      <span class="mode-desc">${esc(m.desc)}</span>
      <span class="mode-calls">質問時: ${esc(m.cost)}</span>
      <span class="combo-src ${st.cls}">${esc(st.text)}</span>
    </label>`;
  }).join("");
  const r = c.rerank_info;
  const rerank = `<label class="mode-card combo-card ${c.rerank ? "selected" : ""}">
      <input type="checkbox" name="combo-rerank" ${c.rerank ? "checked" : ""}>
      <span class="mode-title">${esc(r.label)}</span>
      <span class="mode-desc">${esc(r.desc)}</span>
      <span class="mode-calls">質問時: ${esc(r.cost)}</span>
    </label>`;
  return `<div class="gr-settings combo-settings">
    <div class="gr-head">統合する方式 <span class="muted">（構築し直し不要。次の質問から反映。各方式の検索結果を RRF で統合し、上位 ${esc(c.top_k)} 件から回答を1回生成）</span></div>
    <div class="mode-cards" role="group" aria-label="組み合わせ RAG で統合する方式">${cards}</div>
    <div class="gr-head">LLM の補助（任意）</div>
    <div class="mode-cards" role="group" aria-label="LLM の補助">${rerank}</div>
    <div class="gr-plan">今の組み合わせ: ${variantBadge(c.variant)}
      <span>${esc(c.estimate.text)}</span>
      ${missing.length ? `<span class="gr-diff">インデックスが未構築・古い方式（${esc(missing.join("・"))}）は、質問時に飛ばして残りで答えます</span>` : ""}</div>
  </div>`;
}

async function saveComboSettings() {
  const comps = [...document.querySelectorAll('#e-list input[name="combo-comp"]:checked')].map((el) => el.value);
  const rr = document.querySelector('#e-list input[name="combo-rerank"]');
  await api("/api/config", { combo_components: comps, combo_rerank: !!(rr && rr.checked) });
  await refreshEngines();
}

$("#e-list").addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.name === "gr-extract") saveGraphragMode("graphrag_extract", el.value);
  else if (el.name === "gr-summaries") saveGraphragMode("graphrag_summaries", el.value);
  else if (el.name === "combo-comp" || el.name === "combo-rerank") saveComboSettings();
});

function renderEngineList() {
  const prev = checkState(".e-check");
  const rows = state.engines.map((eng) => {
    const avail = eng.available
      ? '<span class="badge ok">利用可能</span>'
      : `<span class="badge ng">${esc(eng.reason)}</span>`;
    const exp = eng.experimental ? '<span class="badge exp">実験的</span>' : "";
    const req = [eng.requires.chat ? "チャットLLM" : null,
      eng.requires.embed ? "埋め込みAPI" : null].filter(Boolean).join("・") || "なし";
    const stats = eng.index && eng.index.built && eng.index.stats
      ? `構築済み: ${esc(statsText(eng.index.stats))}` : "";
    const warns = (eng.index && eng.index.warnings || [])
      .map((w) => `<div class="engine-warn">⚠ ${esc(w)}</div>`).join("");
    const want = keepChecked(prev, eng.id, eng.kind === "builtin");
    const checked = eng.available && want ? "checked" : "";
    const disabled = eng.available ? "" : "disabled";
    const built = eng.index && eng.index.built ? variantBadge(eng.index.variant) : "";
    return `<div class="engine-row">
      <div class="stripe" style="background:${engineColor(eng.id)}"></div>
      <input type="checkbox" class="e-check" value="${eng.id}" ${checked} ${disabled}>
      <div class="engine-main">
        <span class="engine-name">${esc(eng.name)}</span>
        ${avail}${exp}${indexBadge(eng)}${built}
        <div class="engine-desc">${esc(eng.description)}　<span class="muted">必要: ${req}</span></div>
        ${stats ? `<div class="engine-stats">${stats}</div>` : ""}
        ${warns}
        ${graphragSettingsHtml(eng)}
        ${comboSettingsHtml(eng)}
      </div>
    </div>`;
  });
  $("#e-list").innerHTML = rows.join("");
}

function renderEngineChecks() {
  const prev = checkState("#q-engines input");
  $("#q-engines").innerHTML = state.engines.map((eng) => {
    // 組み合わせ RAG（auto_index）はインデックスを質問時に作るので、未構築でも選べる
    const usable = eng.available && ((eng.index && eng.index.built) || eng.auto_index);
    const want = keepChecked(prev, eng.id, eng.kind === "builtin");
    const checked = usable && want ? "checked" : "";
    const v = usable ? ((eng.index && eng.index.built && eng.index.variant) || (eng.combo && eng.combo.variant) || null) : null;
    const title = eng.available
      ? (usable ? (v ? v.title : "") : "インデックス未構築") : eng.reason;
    return `<label class="engine-check ${usable ? "" : "disabled"}" title="${esc(title)}">
      <input type="checkbox" value="${eng.id}" ${checked} ${usable ? "" : "disabled"}>
      <span style="color:${engineColor(eng.id)}">●</span>${esc(eng.name)}${variantBadge(v)}
    </label>`;
  }).join("");
}

$("#e-ingest").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".e-check:checked")].map((el) => el.value);
  if (!ids.length) { alert("エンジンを選択してください"); return; }
  const r = await api("/api/ingest", { engines: ids });
  if (r.error) { alert(r.error); return; }
  state.ingestJob = r.job_id;
  pollJobs();
});

// ---------------------------------------------------------------- corpus

async function refreshCorpus() {
  state.corpus = await api("/api/corpus");
  $("#c-rev").textContent = `（rev ${state.corpus.rev} / ${state.corpus.docs.length} 文書）`;
  $("#c-docs").innerHTML = state.corpus.docs.map((d) => `
    <div class="doc-row">
      <div>
        <div class="doc-title">${esc(d.title)}</div>
        <div class="doc-meta">${d.id} ・ ${d.text.length.toLocaleString()} 文字</div>
      </div>
      <button class="btn danger" data-del="${esc(d.id)}">削除</button>
    </div>`).join("") || '<p class="hint">文書がありません。追加するか、サンプルを読み込んでください。</p>';
  document.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/corpus/delete", { id: btn.dataset.del });
      refreshCorpus(); refreshEngines();
    });
  });
}

$("#c-add").addEventListener("click", async () => {
  const r = await api("/api/corpus/add",
    { title: $("#c-title").value, text: $("#c-text").value });
  $("#c-msg").textContent = r.error ? `❌ ${r.error}` : "✅ 追加しました";
  if (!r.error) { $("#c-title").value = ""; $("#c-text").value = ""; refreshCorpus(); refreshEngines(); }
});

$("#c-sample").addEventListener("click", async () => {
  const r = await api("/api/corpus/sample", {});
  $("#c-msg").textContent = r.error ? `❌ ${r.error}` : "✅ サンプルを読み込みました";
  refreshCorpus(); refreshEngines();
});

// ---------------------------------------------------------------- query

$("#q-run").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll("#q-engines input:checked")].map((el) => el.value);
  const question = $("#q-question").value.trim();
  if (!question) { $("#q-hint").textContent = "質問を入力してください"; return; }
  if (!ids.length) { $("#q-hint").textContent = "エンジンを選択してください（インデックス構築が必要）"; return; }
  $("#q-hint").textContent = "";
  const body = { question, engines: ids, mode: $("#q-mode").value };
  if ($("#q-beam").value.trim()) body.beam = Number($("#q-beam").value);
  const r = await api("/api/query", body);
  if (r.error) { $("#q-hint").textContent = r.error; return; }
  state.queryJob = r.job_id;
  pollJobs();
});

// ---------------------------------------------------------------- job polling

function pollJobs() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(async () => {
    let active = false;
    if (state.queryJob) {
      const job = await api(`/api/jobs/${state.queryJob}`);
      renderQueryJob(job);
      if (job.status === "running") active = true; else state.queryJob = null;
    }
    if (state.ingestJob) {
      const job = await api(`/api/jobs/${state.ingestJob}`);
      renderIngestJob(job);
      if (job.status === "running") active = true;
      else { state.ingestJob = null; refreshEngines(); }
    }
    if (!active) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }, 1000);
}

function llmStatsText(stats) {
  if (!stats) return "";
  const parts = [];
  if (stats.chat_calls) parts.push(`LLM呼び出し ${stats.chat_calls} 回`);
  if (stats.embed_texts) parts.push(`埋め込み ${stats.embed_texts} 件`);
  return parts.join(" / ");
}

function renderEngineProgress(eid, e) {
  const color = engineColor(eid);
  const status = { pending: "待機", running: "実行中", done: "完了", error: "失敗" }[e.status];
  const bar = e.status === "running" || e.status === "pending"
    ? `<div class="progressbar"><div style="width:${Math.round(e.progress * 100)}%;background:${color}"></div></div>`
    : "";
  return `<div class="result-card">
    <div class="result-head">
      <span class="result-title" style="color:${color}">${esc(engineName(eid))}${variantBadge(e.variant)}</span>
      <span class="result-meta">${status} ${e.elapsed ? "・" + fmtSec(e.elapsed) : ""}</span>
    </div>
    ${bar}
    <div class="result-meta">${esc(e.message || "")}</div>
    ${e.error ? `<div class="error-text">${esc(e.error)}</div>` : ""}
    ${e.status === "done" && e.result && e.result.stats
      ? `<div class="engine-stats">${esc(statsText(e.result.stats))} ${llmStatsText(e.llm_stats) ? "・" + llmStatsText(e.llm_stats) : ""}</div>` : ""}
    ${e.status === "done" && e.result && e.result.warnings
      ? e.result.warnings.map((w) => `<div class="engine-warn">⚠ ${esc(w)}</div>`).join("") : ""}
    ${e.status === "done" && e.result ? logsHtml(e.result.logs) : ""}
  </div>`;
}

function renderIngestJob(job) {
  const cards = Object.entries(job.engines)
    .map(([eid, e]) => renderEngineProgress(eid, e)).join("");
  $("#e-job").innerHTML = `<div class="card">
    <h2>インデックス構築 <span class="muted">${job.status === "running" ? "実行中…" : "完了"}</span></h2>
    <div class="result-grid">${cards}</div></div>`;
}

/* 推論過程（Qwen3 等の thinking）を折りたたみで表示。デフォルトは閉じている */
function thinkHtml(think) {
  if (!think) return "";
  return `<details class="think"><summary>推論過程（クリックで展開）</summary>` +
    `<div class="think-body">${esc(think)}</div></details>`;
}

function citeHtml(c) {
  const cls = c.type === "entity" ? "entity" : (c.type === "community" ? "community" : "");
  const label = c.type === "entity" ? `👤 ${c.title}`
    : c.type === "community" ? `🧩 ${c.ref} ${c.title}` : `📄 ${c.title} (${c.ref})`;
  return `<span class="cite ${cls}" title="${esc(c.snippet || "")}">${esc(label)}</span>`;
}

/* 秒数（1 秒未満は ms。組み合わせ RAG の方式ごとの時間は数 ms のことが多い） */
function fmtDur(s) {
  if (s == null) return "";
  return s < 1 ? `${Math.round(s * 1000)}ms` : fmtSec(s);
}
function fmtScore(x) {
  const n = Number(x);
  return Math.abs(n) >= 100 ? n.toFixed(0) : (Math.abs(n) >= 1 ? n.toFixed(2) : n.toFixed(4));
}

/* 組み合わせ RAG の結果: 使った方式と時間・飛ばした方式と理由・質問時の LLM 呼び出し */
function comboResultHtml(result) {
  const c = result.combo;
  const used = c.components.filter((r) => !r.skipped);
  const parts = c.components.map((r) => {
    if (r.skipped) return `<span class="combo-part skip" title="${esc(r.skipped)}">${esc(r.short)}: 使えず</span>`;
    const extra = [`${r.hits.length}件`];
    if (r.llm_calls) extra.push(`LLM ${r.llm_calls}回`);
    if (r.judge_api_calls) extra.push(`判定API ${r.judge_api_calls}回`);
    if (r.embed_calls) extra.push(`埋め込み ${r.embed_calls}回`);
    const src = r.source && r.source !== "combo" ? `${engineName(r.source)} のインデックス${r.source_variant ? `（${r.source_variant}）` : ""}` : "";
    const tip = [src, r.load_seconds ? `読み込み ${fmtDur(r.load_seconds)}` : "", r.note].filter(Boolean).join("\n");
    return `<span class="combo-part" title="${esc(tip)}">${esc(r.short)} ${esc(fmtDur(r.seconds))}・${esc(extra.join("・"))}</span>`;
  });
  if (c.rerank && c.rerank.selected) {
    parts.push(`<span class="combo-part${c.rerank.used ? "" : " skip"}" title="${esc(c.rerank.note || "")}">並べ替え ${esc(fmtDur(c.rerank.seconds))}${c.rerank.used ? "" : "（RRF の順のまま）"}</span>`);
  }
  parts.push(`<span class="combo-part">回答 ${esc(fmtDur(c.timings.answer))}</span>`);
  const calls = c.llm_calls;
  const breakdown = [calls.judge ? `判定 ${calls.judge}` : "", calls.rerank ? `並べ替え ${calls.rerank}` : "",
    calls.answer ? `回答 ${calls.answer}` : ""].filter(Boolean).join("・");
  const callLine = `質問時の LLM ${calls.total} 回${breakdown ? `（${breakdown}）` : ""}`
    + (c.judge_api_calls ? `・判定 API ${c.judge_api_calls} 回` : "") + `・埋め込み ${c.embed_calls} 回`
    + ` ・ 統合した候補 ${c.fused.length} 件 → 上位 ${c.top_k} 件`;
  const notes = used.filter((r) => r.note)
    .map((r) => `<div class="engine-stats">${esc(r.short)}: ${esc(r.note)}</div>`).join("");
  const warns = (result.warnings || []).map((w) => `<div class="engine-warn">⚠ ${esc(w)}</div>`).join("");
  const weights = used.filter((r) => r.weight !== 1).map((r) => `${r.short} ×${r.weight}`).join("・");
  return `<div class="combo-box">
    <div class="combo-head">組み合わせ: <b>${esc(used.map((r) => r.short).join(" ＋ ") || "なし")}</b>
      <span class="muted">→ RRF で統合${weights ? `（重み ${esc(weights)}）` : ""}${c.rerank && c.rerank.used ? " → LLM で並べ替え" : ""}</span></div>
    <div class="combo-parts">${parts.join("")}</div>
    <div class="engine-stats">${esc(callLine)}</div>
    ${notes}${warns}
  </div>`;
}

/* 組み合わせ RAG の出典: マウスを載せると、統合後の順位と方式ごとの順位・スコアが出る */
function comboCiteHtml(c, used) {
  const why = Object.fromEntries((c.why || []).map((w) => [w.component, w]));
  const lines = [c.snippet || "", "―――――",
    `総合 ${c.rank} 位（RRF ${c.score}）${c.fused_rank !== c.rank ? `・並べ替え前は ${c.fused_rank} 位` : ""}`];
  for (const r of used) {
    const w = why[r.id];
    lines.push(w ? `${r.label}: ${w.rank} 位・スコア ${fmtScore(w.score)}${w.weight !== 1 ? `（重み ×${w.weight}）` : ""}`
      : `${r.label}: 候補外`);
  }
  const marks = used.map((r) => (why[r.id] ? `${r.short} ${why[r.id].rank}位` : "")).filter(Boolean).join("・");
  return `<span class="cite combo-cite" title="${esc(lines.join("\n"))}">${esc(`${c.rank}. 📄 ${c.title} (${c.ref})`)}`
    + `<span class="cite-why">${esc(marks)}</span></span>`;
}

/* 出典ごとの各方式の順位・スコア（狭い画面では表だけ横にスクロール） */
function comboTableHtml(result) {
  const c = result.combo;
  const used = c.components.filter((r) => !r.skipped);
  if (!used.length || !(result.citations || []).length) return "";
  const head = used.map((r) => `<th>${esc(r.short)}${r.weight !== 1 ? `<span class="muted"> ×${esc(r.weight)}</span>` : ""}</th>`).join("");
  const rows = result.citations.map((cite) => {
    const why = Object.fromEntries((cite.why || []).map((w) => [w.component, w]));
    const cells = used.map((r) => {
      const w = why[r.id];
      return w ? `<td>${w.rank}位<br><span class="muted">${esc(fmtScore(w.score))}</span></td>` : '<td class="muted">—</td>';
    }).join("");
    const moved = cite.fused_rank !== cite.rank ? `<br><span class="muted">RRF ${cite.fused_rank}位</span>` : "";
    return `<tr><td>${cite.rank}${moved}</td><td class="combo-doc">${esc(cite.title)}<br><span class="muted">${esc(cite.ref)}</span></td>${cells}<td>${esc(cite.score)}</td></tr>`;
  }).join("");
  return `<details class="think combo-why"><summary>出典ごとの各方式の順位・スコア（RRF の内訳）</summary>
    <div class="table-scroll"><table class="combo-table">
      <thead><tr><th>順位</th><th>出典</th>${head}<th>RRF</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="combo-foot">RRF: 方式ごとに、順位 r に 重み ÷ (${esc(c.fusion.k)} ＋ r) を足した値（大きいほど上位）。
      スコアの尺度は方式ごとに違う（全文: BM25・埋め込み: コサイン類似度・GraphRAG: たどったエンティティの重みの和・DecisionRAG: 経路スコア）</div>
  </details>`;
}

/* GraphRAG の結果: どの方式のインデックスで答えたか・質問時の要約の作成と再利用 */
function graphragQueryStats(result) {
  const st = result.stats || {};
  const parts = [`抽出: ${graphModeLabel("extract", st.extract_mode)}`,
    `コミュニティ要約: ${graphModeLabel("summaries", st.summary_mode)}`];
  if (st.mapped_communities != null) parts.push(`map ${st.mapped_communities} コミュニティ`);
  if (st.lazy_summarized != null) {
    parts.push(`質問時に要約 ${st.lazy_summarized} 件・保存済みを再利用 ${st.lazy_reused || 0} 件`);
  }
  if (st.fallback) parts.push("チャンク直接回答に切り替え");
  return `<div class="engine-stats">${esc(parts.join(" ・ "))}</div>`;
}

function renderQueryJob(job) {
  const cards = [];
  // 統合レポート
  const syn = job.synthesis || {};
  if (syn.status === "done") {
    cards.push(`<div class="result-card synthesis">
      <div class="result-head"><span class="result-title">🧭 統合レポート</span>
      <span class="result-meta">各エンジンの回答の一致点・相違点</span></div>
      ${thinkHtml(syn.think)}
      <div class="answer">${renderText(syn.text)}</div></div>`);
  } else if (syn.status === "running") {
    cards.push('<div class="result-card synthesis"><div class="result-title">🧭 統合レポート生成中…</div></div>');
  } else if (syn.status === "skipped" && job.status !== "running") {
    cards.push(`<div class="result-card synthesis"><div class="result-title">🧭 統合レポート</div>
      <div class="result-meta">${esc(syn.error)}</div></div>`);
  } else if (syn.status === "error") {
    cards.push(`<div class="result-card synthesis"><div class="result-title">🧭 統合レポート</div>
      <div class="error-text">${esc(syn.error)}</div></div>`);
  }
  // 各エンジン
  for (const [eid, e] of Object.entries(job.engines)) {
    if (e.status === "done" && e.result) {
      const color = engineColor(eid);
      const combo = eid === "combo" && e.result.combo;
      const comboUsed = combo ? e.result.combo.components.filter((r) => !r.skipped) : [];
      const cites = (e.result.citations || [])
        .map((c) => (combo && c.why ? comboCiteHtml(c, comboUsed) : citeHtml(c))).join("");
      cards.push(`<div class="result-card" style="border-top:3px solid ${color}">
        <div class="result-head">
          <span class="result-title" style="color:${color}">${esc(engineName(eid))}${variantBadge(e.result.variant || e.variant)}</span>
          <span class="result-meta">mode=${esc(e.result.mode || "")} ・ ${fmtSec(e.elapsed)}
            ${llmStatsText(e.llm_stats) ? "・" + llmStatsText(e.llm_stats) : ""}</span>
        </div>
        ${thinkHtml(e.result.think)}
        <div class="answer">${renderText(e.result.answer || "")}</div>
        ${eid === "decision" && e.result.stats && e.result.stats.route
          ? `<div class="engine-stats">階層の経路（経路スコア）: ${esc(e.result.stats.route)} ・ ビーム幅 ${esc(e.result.stats.beam)} ・ 判定 ${esc(e.result.stats.calls)} 回${e.result.stats.trimmed ? `（上限で ${esc(e.result.stats.trimmed)} 件未判定）` : ""}（${esc(judgeLabel(e.result.stats))}${e.result.stats.soft ? "・確率" : ""}${e.result.stats.choice ? "・選択式で並べ替え" : ""}）${apiUsage(e.result.stats)}${e.result.stats.note ? " ・ " + esc(e.result.stats.note) : ""}</div>` : ""}
        ${eid === "graphrag" && e.result.stats ? graphragQueryStats(e.result) : ""}
        ${combo ? comboResultHtml(e.result) : ""}
        ${cites ? `<div class="citations">${cites}</div>` : ""}
        ${combo ? comboTableHtml(e.result) : ""}
        ${logsHtml(e.result.logs)}
      </div>`);
    } else {
      cards.push(renderEngineProgress(eid, e));
    }
  }
  $("#q-results").innerHTML = `
    <div class="card">
      <div class="row space-between">
        <h2>結果 <span class="muted">「${esc(job.question)}」（モード: ${esc(job.mode)}）</span></h2>
        <span class="muted">${job.status === "running" ? "実行中…" : "完了"}</span>
      </div>
      <div class="result-grid">${cards.join("")}</div>
    </div>`;
}

// ---------------------------------------------------------------- graph

// グラフ表示に対応するエンジン（組み込み GraphRAG と GraphML を出力する外部エンジン）
const GRAPH_ENGINES = ["graphrag", "lightrag", "minirag", "rag-anything", "nano-graphrag"];
let gview = null;
let gdata = null;

function graphView() {
  if (!gview) {
    gview = new GraphView($("#g-canvas"), {
      tooltip: $("#g-tooltip"), height: 620,
      onSelect: (node) => renderGraphDetail(node),
    });
  }
  return gview;
}

function fillGraphEngineSelect() {
  /* インデックス構築済みのエンジンを優先して選択肢を作る（未構築は表示のみ） */
  const sel = $("#g-engine");
  const current = sel.value;
  const opts = GRAPH_ENGINES.map((id) => {
    const eng = state.engines.find((e) => e.id === id);
    const built = !!(eng && eng.index && eng.index.built);
    const v = built ? variantText(eng.index.variant) : "";
    return { id, built, label: `${eng ? eng.name : id}${v}${built ? "" : "（未構築）"}` };
  });
  sel.innerHTML = opts.map((o) =>
    `<option value="${o.id}">${esc(o.label)}</option>`).join("");
  const keep = opts.find((o) => o.id === current && o.built);
  const firstBuilt = opts.find((o) => o.built);
  sel.value = keep ? current : (firstBuilt ? firstBuilt.id : (current || "graphrag"));
}

async function loadGraph() {
  if (!state.engines.length) await refreshEngines();
  if (!$("#g-engine").options.length) fillGraphEngineSelect();
  const engine = $("#g-engine").value;
  const gv = graphView();
  gv.resize();
  // 組み込み GraphRAG は LLM 要約つきコミュニティ、外部は自動グループ
  $("#g-com-title").textContent = engine === "graphrag"
    ? "コミュニティ要約" : "コミュニティ（ラベル伝播による自動グループ）";
  $("#g-hint").textContent = "読み込み中…";
  const limit = $("#g-limit").value;
  const data = await api(`/api/graph?engine=${encodeURIComponent(engine)}&limit=${limit}`);
  renderGraphDetail(null);
  if (data.error) {
    $("#g-hint").textContent = data.error;
    $("#g-communities").innerHTML = "";
    $("#g-legend").innerHTML = "";
    gv.emptyMessage = data.error.slice(0, 70);
    gdata = null;
    gv.setData({ nodes: [], edges: [], communities: [] });
    return;
  }
  gdata = data;
  gv.emptyMessage = "";
  gv.setShowIsolated($("#g-isolated").checked);
  gv.setData(data);
  updateGraphHint();
  renderLegend();
  if (data.variant) {
    $("#g-com-title").innerHTML = `コミュニティ要約 ${variantBadge(data.variant)}`;
  }
  // summary_source: llm（LLM の要約）/ auto（LLM なしで機械的に作った要約）
  const srcTag = (c) => c.summary_source === "auto"
    ? '<span class="badge">自動（LLM なし）</span>'
    : (c.summary_source === "llm" ? '<span class="badge ok">LLM 要約</span>' : "");
  $("#g-communities").innerHTML = data.communities
    .filter((c) => c.summary)
    .map((c) => `<div class="community-row" data-com="${esc(c.id)}"><b>[${esc(c.id)}] ${esc(c.title)}</b>
      <span class="muted">（${c.size} エンティティ）</span>${srcTag(c)}<br>${esc(c.summary)}</div>`)
    .join("") || '<p class="hint">要約付きコミュニティはありません。</p>';
  document.querySelectorAll("#g-communities [data-com]").forEach((el) => {
    el.addEventListener("click", () => {
      gv.highlightCommunity(el.dataset.com);
      document.querySelectorAll("#g-communities .community-row").forEach((r) =>
        r.classList.toggle("active", r === el && gv.highlightCom === el.dataset.com));
      $("#g-canvas").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
}

function updateGraphHint() {
  if (!gdata) return;
  const st = graphView().stats();
  const parts = [`エンティティ ${gdata.total_nodes ?? gdata.nodes.length}`,
    `関係 ${gdata.total_edges ?? gdata.edges.length}`, `コミュニティ ${gdata.communities.length}`];
  let note = `表示 ${st.shown} ノード`;
  if (gdata.truncated) note += "（関係の多い順に上限まで）";
  if (st.isolated && !$("#g-isolated").checked) note += ` ・ 孤立ノード ${st.isolated} 件は非表示`;
  $("#g-hint").textContent = `${parts.join(" / ")} ・ ${note}。ホイールで拡大縮小、背景ドラッグで移動、クリックで詳細。`;
}

function renderLegend() {
  const items = graphView().legend();
  $("#g-legend").innerHTML = `<div class="muted">凡例（${$("#g-color").value === "type" ? "種別" : "コミュニティ"}）</div>`
    + items.map((it) => `<div class="legend-row" data-key="${esc(it.key)}">
      <span class="sw" style="background:${it.color}"></span>${esc(it.label.slice(0, 28))}
      <span class="cnt">${it.count}</span></div>`).join("");
  if ($("#g-color").value === "community") {
    document.querySelectorAll("#g-legend [data-key]").forEach((el) => {
      el.addEventListener("click", () => graphView().highlightCommunity(el.dataset.key));
    });
  }
}

function renderGraphDetail(node) {
  const box = $("#g-detail");
  if (!node) {
    box.innerHTML = '<p class="hint">ノードをクリックすると、説明・関係・出典がここに表示されます。</p>';
    return;
  }
  const rels = graphView().neighbors(node.id);
  const docs = (node.docs || []).map((d) => `<li>${esc(d)}</li>`).join("");
  box.innerHTML = `<div class="detail-title">${esc(node.name)}</div>
    <div class="muted">${esc(node.type || "")}${node.community ? " ・ " + esc(node.community) : ""} ・ 関係 ${rels.length}</div>
    <p>${esc(node.description || "（説明なし）")}</p>
    ${docs ? `<div class="muted">出典文書</div><ul>${docs}</ul>` : ""}
    <div class="muted">関係（強い順）</div>
    ${rels.slice(0, 40).map((r) => `<div class="detail-rel" data-id="${esc(r.node.id)}">
      → <b>${esc(r.node.name)}</b> <span class="muted">強さ${r.edge.strength ?? ""}</span><br>
      <span class="muted">${esc((r.edge.description || "").slice(0, 120))}</span></div>`).join("")
      || '<p class="hint">関係はありません。</p>'}`;
  box.querySelectorAll("[data-id]").forEach((el) => {
    el.addEventListener("click", () => graphView().focus(el.dataset.id));
  });
}

$("#g-reload").addEventListener("click", loadGraph);
$("#g-engine").addEventListener("change", loadGraph);
$("#g-limit").addEventListener("change", loadGraph);
$("#g-isolated").addEventListener("change", () => {
  graphView().setShowIsolated($("#g-isolated").checked);
  updateGraphHint(); renderLegend();
});
$("#g-color").addEventListener("change", () => {
  graphView().setColorMode($("#g-color").value); renderLegend();
});
$("#g-fit").addEventListener("click", () => graphView().fit());
$("#g-zoom-in").addEventListener("click", () => graphView().zoomBy(1.3));
$("#g-zoom-out").addEventListener("click", () => graphView().zoomBy(1 / 1.3));
$("#g-search").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  const hits = graphView().search($("#g-search").value);
  if (hits.length) graphView().focus(hits[0].id);
  $("#g-hint").textContent = $("#g-search").value
    ? `「${$("#g-search").value}」に一致: ${hits.length} 件` : "";
  if (!$("#g-search").value) updateGraphHint();
});

// ---------------------------------------------------------------- init

(async function init() {
  await loadConfig();
  await refreshCorpus();
  await refreshEngines();
})();
