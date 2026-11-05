/* RAG Orchestrator — フロントエンド（依存なしの素の JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const ENGINE_COLORS = {
  graphrag: "#a78bfa", vector: "#5aa2ff", bm25: "#34d399", hybrid: "#f59e0b",
  decision: "#f9a8d4",
  "nano-graphrag": "#f472b6", lightrag: "#f472b6", minirag: "#fb7185",
  hipporag: "#22d3ee", "rag-anything": "#c084fc",
};
const state = {
  engines: [],
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
  "decision_judge", "decision_beam", "decision_budget", "jev_endpoint", "jev_model",
  "context_window", "request_timeout", "max_tokens", "proxy_url"];

async function loadConfig() {
  const cfg = await api("/api/config");
  for (const f of CFG_FIELDS) { const el = $(`#s-${f}`); if (el) el.value = cfg[f] ?? ""; }
  $("#s-use_proxy").checked = !!cfg.use_proxy;
  $("#s-api_key").placeholder = cfg.has_key
    ? "（設定済み。変更する場合のみ入力）" : "（未設定。不要なサーバでは空のまま）";
  $("#s-embed_api_key").placeholder = cfg.has_embed_key ? "（設定済み）" : "（空なら API Key を流用）";
  $("#s-jev_api_key").placeholder = cfg.has_jev_key ? "（設定済み。変更する場合のみ入力）" : "（未設定）";
  updateConnBadge(cfg);
}

function updateConnBadge(cfg) {
  const ok = cfg.base_url && cfg.model;
  $("#conn-status").innerHTML = ok
    ? `<span class="dot ok"></span>LLM: ${esc(cfg.base_url)} / ${esc(cfg.model)}`
    : '<span class="dot ng"></span>LLM 未設定（BM25 は抜粋モードで動作可）';
}

$("#s-save").addEventListener("click", async () => {
  const body = { use_proxy: $("#s-use_proxy").checked };
  for (const f of CFG_FIELDS) body[f] = $(`#s-${f}`).value;
  if ($("#s-api_key").value) body.api_key = $("#s-api_key").value;
  if ($("#s-embed_api_key").value) body.embed_api_key = $("#s-embed_api_key").value;
  if ($("#s-jev_api_key").value) body.jev_api_key = $("#s-jev_api_key").value;
  const cfg = await api("/api/config", body);
  $("#s-api_key").value = ""; $("#s-embed_api_key").value = ""; $("#s-jev_api_key").value = "";
  $("#s-jev_api_key").placeholder = cfg.has_jev_key ? "（設定済み）" : "（未設定）";
  $("#s-msg").textContent = "✅ 保存しました";
  updateConnBadge(cfg);
  refreshEngines();
});

$("#s-test").addEventListener("click", async () => {
  $("#s-msg").textContent = "接続テスト中…";
  const r = await api("/api/config/test", {});
  const chat = r.chat.ok ? `✅ チャット: ${esc(r.chat.message)}` : `❌ チャット: ${esc(r.chat.message)}`;
  const emb = r.embed.ok ? `✅ 埋め込み: ${esc(r.embed.message)}` : `❌ 埋め込み: ${esc(r.embed.message)}`;
  $("#s-msg").innerHTML = `${chat}<br>${emb}`;
});

// ---------------------------------------------------------------- engines

async function refreshEngines() {
  const data = await api("/api/engines");
  state.engines = data.engines;
  renderEngineList();
  renderEngineChecks();
}

function indexBadge(eng) {
  const idx = eng.index || {};
  if (!idx.built) return '<span class="badge">インデックス未構築</span>';
  if (idx.stale) return '<span class="badge warn">インデックスが古い（要再構築）</span>';
  return '<span class="badge ok">インデックス構築済み</span>';
}

function renderEngineList() {
  const rows = state.engines.map((eng) => {
    const avail = eng.available
      ? '<span class="badge ok">利用可能</span>'
      : `<span class="badge ng">${esc(eng.reason)}</span>`;
    const exp = eng.experimental ? '<span class="badge exp">実験的</span>' : "";
    const req = [eng.requires.chat ? "チャットLLM" : null,
      eng.requires.embed ? "埋め込みAPI" : null].filter(Boolean).join("・") || "なし";
    const stats = eng.index && eng.index.built && eng.index.stats
      ? `構築済み: ${esc(Object.entries(eng.index.stats)
          .map(([k, v]) => `${k}=${v}`).join(", "))}` : "";
    const warns = (eng.index && eng.index.warnings || [])
      .map((w) => `<div class="engine-warn">⚠ ${esc(w)}</div>`).join("");
    const checked = eng.available && eng.kind === "builtin" ? "checked" : "";
    const disabled = eng.available ? "" : "disabled";
    return `<div class="engine-row">
      <div class="stripe" style="background:${engineColor(eng.id)}"></div>
      <input type="checkbox" class="e-check" value="${eng.id}" ${checked} ${disabled}>
      <div class="engine-main">
        <span class="engine-name">${esc(eng.name)}</span>
        ${avail}${exp}${indexBadge(eng)}
        <div class="engine-desc">${esc(eng.description)}　<span class="muted">必要: ${req}</span></div>
        ${stats ? `<div class="engine-stats">${stats}</div>` : ""}
        ${warns}
      </div>
    </div>`;
  });
  $("#e-list").innerHTML = rows.join("");
}

function renderEngineChecks() {
  $("#q-engines").innerHTML = state.engines.map((eng) => {
    const usable = eng.available && eng.index && eng.index.built;
    const checked = usable && eng.kind === "builtin" ? "checked" : "";
    const title = eng.available
      ? (usable ? "" : "インデックス未構築") : eng.reason;
    return `<label class="engine-check ${usable ? "" : "disabled"}" title="${esc(title)}">
      <input type="checkbox" value="${eng.id}" ${checked} ${usable ? "" : "disabled"}>
      <span style="color:${engineColor(eng.id)}">●</span>${esc(eng.name)}
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
  const r = await api("/api/query",
    { question, engines: ids, mode: $("#q-mode").value });
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
      <span class="result-title" style="color:${color}">${esc(engineName(eid))}</span>
      <span class="result-meta">${status} ${e.elapsed ? "・" + fmtSec(e.elapsed) : ""}</span>
    </div>
    ${bar}
    <div class="result-meta">${esc(e.message || "")}</div>
    ${e.error ? `<div class="error-text">${esc(e.error)}</div>` : ""}
    ${e.status === "done" && e.result && e.result.stats
      ? `<div class="engine-stats">${esc(Object.entries(e.result.stats)
          .map(([k, v]) => `${k}=${v}`).join(", "))} ${llmStatsText(e.llm_stats) ? "・" + llmStatsText(e.llm_stats) : ""}</div>` : ""}
    ${e.status === "done" && e.result && e.result.warnings
      ? e.result.warnings.map((w) => `<div class="engine-warn">⚠ ${esc(w)}</div>`).join("") : ""}
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
      const cites = (e.result.citations || []).map(citeHtml).join("");
      cards.push(`<div class="result-card" style="border-top:3px solid ${color}">
        <div class="result-head">
          <span class="result-title" style="color:${color}">${esc(engineName(eid))}</span>
          <span class="result-meta">mode=${esc(e.result.mode || "")} ・ ${fmtSec(e.elapsed)}
            ${llmStatsText(e.llm_stats) ? "・" + llmStatsText(e.llm_stats) : ""}</span>
        </div>
        ${thinkHtml(e.result.think)}
        <div class="answer">${renderText(e.result.answer || "")}</div>
        ${eid === "decision" && e.result.stats && e.result.stats.route
          ? `<div class="engine-stats">階層の経路: ${esc(e.result.stats.route)} ・ 判定 ${esc(e.result.stats.calls)} 回（${esc(e.result.stats.judge)}${e.result.stats.soft ? "・確率" : ""}${e.result.stats.choice ? "・選択式で並べ替え" : ""}）${e.result.stats.note ? " ・ " + esc(e.result.stats.note) : ""}</div>` : ""}
        ${cites ? `<div class="citations">${cites}</div>` : ""}
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
    return { id, built, label: `${eng ? eng.name : id}${built ? "" : "（未構築）"}` };
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
  $("#g-communities").innerHTML = data.communities
    .filter((c) => c.summary)
    .map((c) => `<div class="community-row" data-com="${esc(c.id)}"><b>[${esc(c.id)}] ${esc(c.title)}</b>
      <span class="muted">（${c.size} エンティティ）</span><br>${esc(c.summary)}</div>`)
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
