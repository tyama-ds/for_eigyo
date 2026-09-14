/* ============================================================
   Patent Query Builder — UI（ブラウザ側）
   サーバの /api/* を呼び、案件 → G1 → G2 → G3 → G4 → 3 案＋根拠 の流れを画面にする。
   ============================================================ */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 3) => (v === null || v === undefined || v === "" ? "—" : typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(d)) : String(v));
const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
const VARIANT_JA = { broad: "広め", standard: "標準", narrow: "狭め" };
const STATUS_JA = { new: "登録", g1_pending: "G1 待ち", axes_fixed: "観点確定", g2_pending: "G2 待ち", g3_pending: "G3 待ち", g4_pending: "G4 待ち", finalized: "確定" };
const KIND_JA = { adopted: "採用", rejected: "棄却", candidate: "保留" };

const state = { cases: [], caseId: null, bundle: null, config: null, purposes: {}, tab: "overview", qdialect: {}, filters: { reviewOnly: false, candOnly: false } };

/* ---------------- 共通 ---------------- */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 4000);
}
function busy(on, text = "処理中…") { $("#busy").classList.toggle("hidden", !on); $("#busy-text").textContent = text; }
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModals() { $$(".modal").forEach((m) => m.classList.add("hidden")); }
$$(".modal-close").forEach((b) => b.addEventListener("click", closeModals));
$$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) closeModals(); }));

class Pending extends Error { constructor(data) { super(data.message || "pending"); this.data = data; } }

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  let data = {};
  const ctype = res.headers.get("Content-Type") || "";
  if (ctype.includes("json")) data = await res.json(); else data = { text: await res.text() };
  if (res.status === 409 && data.pending) throw new Pending(data);
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/** アクションを実行し、manual／confirm の保留なら該当モーダルを開いて再実行を待つ */
async function act(label, fn, after) {
  busy(true, label);
  try {
    const r = await fn(false);
    if (after) await after(r);
    return r;
  } catch (e) {
    if (e instanceof Pending) {
      if (e.data.kind === "manual") openManual(e.data.prompts, () => act(label, fn, after));
      else if (e.data.kind === "confirm") openConfirm(e.data, () => act(label, (c) => fn(true), after));
      else toast(e.data.message || "人の判断待ち", "error");
      return null;
    }
    toast(e.message, "error");
    throw e;
  } finally { busy(false); }
}

function readFileB64(file) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(file); });
}
function copyText(text) { navigator.clipboard?.writeText(text).then(() => toast("コピーしました", "ok"), () => toast("コピーに失敗", "error")); }

/* ---------------- 起動 ---------------- */
async function loadConfig() {
  const d = await api("/api/config");
  state.config = d.config; state.purposes = d.purposes || {}; state.dialects = d.dialects; state.csv_dialects = d.csv_dialects;
  const llm = d.config.llm || {};
  const bl = $("#badge-llm"); bl.textContent = `LLM: ${llm.mode}${llm.mode === "api" ? " / " + (llm.model || "モデル未設定") : ""}` + (d.config.offline ? "（offline）" : "") + (d.config.production ? "【production】" : "");
  bl.className = "badge " + (llm.mode === "mock" ? "warn" : "ok");
  const bd = $("#badge-db"); bd.textContent = `母集団: ${d.local_index_size} 件（local_index）`; bd.className = "badge " + (d.local_index_size ? "ok" : "");
  const bdict = $("#badge-dict"); bdict.textContent = `辞書: ${d.dictionary.codes} コード` + (d.dictionary.degraded ? "（縮退）" : ""); bdict.className = "badge " + (d.dictionary.degraded ? "warn" : "ok");
  const sel = $("#purpose-select"); sel.innerHTML = Object.entries(state.purposes).map(([k, v]) => `<option value="${k}">${esc(v.label)}（${v.population_range.join("〜")}件）</option>`).join("");
}
async function loadCases() {
  const d = await api("/api/cases");
  state.cases = d.cases;
  renderCaseList();
}
async function openCase(caseId, tab) {
  state.caseId = caseId;
  state.bundle = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  if (tab) state.tab = tab;
  renderCaseList();
  renderCase();
  updateHash();
}
function updateHash() {
  if (!state.caseId) return;
  const h = `#case=${encodeURIComponent(state.caseId)}&tab=${state.tab}`;
  if (location.hash !== h) history.replaceState(null, "", h);
}
function parseHash() {
  const m = new URLSearchParams(location.hash.replace(/^#/, ""));
  return { caseId: m.get("case"), tab: m.get("tab") };
}
async function refresh(tab) { if (state.caseId) await openCase(state.caseId, tab); await loadCases(); await loadConfig(); }

/* ---------------- 案件一覧 ---------------- */
function renderCaseList() {
  const el = $("#case-list");
  if (!state.cases.length) { el.innerHTML = `<div class="dim" style="font-size:.82rem">案件はまだありません。「＋ 新規」か「🧪 サンプル案件」から。</div>`; return; }
  el.innerHTML = state.cases.map((c) => `
    <button class="case-card ${c.case_id === state.caseId ? "active" : ""}" data-id="${esc(c.case_id)}">
      <div class="name" title="${esc(c.name)}">${esc(c.name)}</div>
      <div class="sub"><span class="status ${esc(c.status)}">${esc(STATUS_JA[c.status] || c.status)}</span><span>v${c.iteration}</span><span>${esc((state.purposes[c.purpose] || {}).label || c.purpose)}</span></div>
    </button>`).join("");
  $$(".case-card", el).forEach((b) => b.addEventListener("click", () => openCase(b.dataset.id)));
}

$("#btn-new-case").addEventListener("click", () => $("#new-case-form").classList.toggle("hidden"));
$("#btn-cancel-new").addEventListener("click", () => $("#new-case-form").classList.add("hidden"));
$("#new-case-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  try {
    const r = await act("案件を登録", () => api("/api/cases", body));
    if (r) { e.target.reset(); $("#new-case-form").classList.add("hidden"); await loadCases(); await openCase(r.case.case_id, "g1"); toast("登録しました。G1: P1 で観点を提案させてください", "ok"); }
  } catch {}
});
$("#btn-demo").addEventListener("click", async () => {
  try {
    const r = await act("サンプルを読み込み", () => api("/api/demo", {}));
    if (r) { await loadConfig(); await loadCases(); await openCase(r.case.case_id, "overview"); toast("合成データのサンプル案件を作りました（母集団 120 件を local_index に読込）", "ok"); }
  } catch {}
});
$("#btn-theme").addEventListener("click", () => {
  const html = document.documentElement; const next = html.dataset.theme === "dark" ? "light" : "dark";
  html.dataset.theme = next; try { localStorage.setItem("pqb.theme", next); } catch {}
});
try { const t = localStorage.getItem("pqb.theme"); if (t) document.documentElement.dataset.theme = t; } catch {}

/* ---------------- 案件ビュー ---------------- */
const STEP_ORDER = { new: 0, g1_pending: 0, axes_fixed: 1, g2_pending: 1, g3_pending: 2, g4_pending: 3, finalized: 4 };
function renderCase() {
  const b = state.bundle;
  $("#empty").classList.add("hidden"); $("#case-view").classList.remove("hidden");
  const c = b.case;
  $("#case-title").textContent = c.name;
  $("#case-meta").innerHTML = `${esc(c.case_id)} ・ ${esc(b.purpose.label)} ・ 反復 v${c.iteration} ・ <span class="status ${esc(c.status)}">${esc(STATUS_JA[c.status] || c.status)}</span>` +
    ` ・ 既知文献 ${c.seeds.length} 件 ・ プール ${b.pool.length} 件 ・ LLM 呼出 ${b.llm_calls.length}${b.llm_calls.length >= 100 ? "+" : ""} 回` +
    (b.pending_prompts.length ? ` ・ <span class="chip warn">LLM 返答待ち ${b.pending_prompts.length}</span>` : "");
  const cur = STEP_ORDER[c.status] ?? 0;
  $$("#stepper li").forEach((li, i) => { li.classList.toggle("done", i < cur || c.status === "finalized"); li.classList.toggle("current", i === cur && c.status !== "finalized"); });
  $$("#tabs button").forEach((t) => t.classList.toggle("active", t.dataset.tab === state.tab));
  const body = $("#tab-body");
  const render = { overview: renderOverview, g1: renderG1, g2: renderG2, g3: renderG3, g4: renderG4, report: renderReport, log: renderLog }[state.tab] || renderOverview;
  body.innerHTML = render(b);
  bindTab(state.tab, b);
}
$("#tabs").addEventListener("click", (e) => { const t = e.target.closest("button"); if (!t) return; state.tab = t.dataset.tab; renderCase(); updateHash(); });
$("#btn-delete-case").addEventListener("click", async () => {
  if (!confirm(`案件 ${state.caseId} を削除しますか？（判断ログ・検索式もすべて消えます）`)) return;
  try { await api(`/api/cases/${encodeURIComponent(state.caseId)}`, null, "DELETE"); state.caseId = null; state.bundle = null; $("#case-view").classList.add("hidden"); $("#empty").classList.remove("hidden"); await loadCases(); toast("削除しました"); } catch (e) { toast(e.message, "error"); }
});
$("#btn-auto").addEventListener("click", async () => {
  if (!confirm("Policy 決定者（案A）で残りのゲートを自動で回します。DB 実行はオフライン母集団（local_index）または db.mode=api で行います。続けますか？")) return;
  try { await act("自動で回しています…", () => api(`/api/cases/${encodeURIComponent(state.caseId)}/auto`, { max_iterations: 3 }), async () => { await refresh("overview"); toast("自動運転が終了しました", "ok"); }); } catch {}
});

/* ---------------- 概要 ---------------- */
function metricsKpis(m) {
  if (!m) return "";
  const cls = (ok) => (ok === null || ok === undefined ? "" : ok ? "ok" : "bad");
  return `<div class="kpis">
    <div class="kpi"><div class="k">母集団サイズ</div><div class="v">${fmt(m.hit_count)}</div></div>
    <div class="kpi ${cls(m.recall_seed === null ? null : m.recall_seed >= 1)}"><div class="k">既知文献再現率</div><div class="v">${pct(m.recall_seed)}</div></div>
    <div class="kpi ${cls(m.recall_pool === null ? null : m.recall_pool >= (state.bundle.config.tau_pool || 0.95))}"><div class="k">プール再現率</div><div class="v">${pct(m.recall_pool)}</div></div>
    <div class="kpi"><div class="k">上位K適合率</div><div class="v">${pct(m.p_at_k)}<span class="dim" style="font-size:.7rem"> (${m.k_judged}件)</span></div></div>
    <div class="kpi ${cls(m.in_range)}"><div class="k">目標レンジ</div><div class="v">${m.in_range === null ? "—" : m.in_range ? "内" : "外"}</div></div>
  </div>`;
}
function renderOverview(b) {
  const c = b.case, lm = b.latest_metrics;
  const nextStep = {
    new: `<button class="btn primary" data-go="g1">G1: 観点を提案させる（P1）</button>`,
    g1_pending: `<button class="btn primary" data-go="g1">G1: 観点を確認・確定する</button>`,
    axes_fixed: `<button class="btn primary" data-go="g2">G2: 候補を展開する（P2／P3）</button>`,
    g2_pending: `<button class="btn primary" data-go="g2">G2: 候補の採否を決める</button>`,
    g3_pending: `<button class="btn primary" data-go="g3">G3: 検索式を DB で実行し結果を取り込む</button>`,
    g4_pending: `<button class="btn primary" data-go="g4">G4: 評価を確認し、確定または再反復</button>`,
    finalized: `<button class="btn primary" data-go="report">根拠レポートを見る</button>`,
  }[c.status] || "";
  const stop = lm?.stop;
  return `
  <div class="card"><h3>次のステップ <span class="sp"></span>${nextStep}</h3>
    <p class="dim">流れ: G1 観点の確認（以後固定）→ G2 語・分類候補の採否 → G3 3 案を DB で実行し CSV 取り込み → 採点（P4）→ 分析（RSJ 逆算・変換候補・推定再現率）→ G4 確定／再反復。</p>
  </div>
  <div class="grid2">
    <div class="card"><h3>入力（技術説明）</h3><p style="white-space:pre-wrap">${esc(c.input_text)}</p>
      <p class="dim">既知文献: ${c.seeds.length ? c.seeds.map((s) => `<span class="chip">${esc(s)}</span>`).join("") : "なし"}</p>
      ${c.settings?.mask_terms?.length ? `<p class="dim">マスキング語: ${c.settings.mask_terms.map((m) => `<span class="chip">${esc(m)}</span>`).join("")}</p>` : ""}
      ${c.settings?.sample ? `<div class="warnbox">この案件は同梱の合成データ（実データではない）です。</div>` : ""}
    </div>
    <div class="card"><h3>観点（${b.axes.length}）</h3>
      ${b.axes.length ? `<table><thead><tr><th>ID</th><th>観点</th><th>種別</th><th>語（採用）</th><th>分類（採用）</th></tr></thead><tbody>${b.axes.map((a) => `<tr><td>${esc(a.axis_id)}</td><td>${esc(a.name)}<div class="dim" style="font-size:.72rem">${esc(a.definition)}</div></td><td><span class="chip ${a.kind === "required" ? "req" : "aux"}">${a.kind === "required" ? "必須" : "補助"}</span></td>
        <td>${b.candidates.filter((x) => x.kind === "term" && x.axis_id === a.axis_id && x.status === "adopted").map((x) => `<span class="chip">${esc(x.value)}</span>`).join("")}</td>
        <td>${b.candidates.filter((x) => x.kind === "code" && x.axis_id === a.axis_id && x.status === "adopted").map((x) => `<span class="chip">${esc(x.scheme)} ${esc(x.value)}</span>`).join("")}</td></tr>`).join("")}</tbody></table>` : `<p class="dim">まだありません。G1 で P1 に提案させます。</p>`}
    </div>
  </div>
  ${lm ? `<div class="card"><h3>最新の評価（反復 v${lm.iteration}）${lm.pareto?.recommended ? `<span class="chip ok">推奨: ${VARIANT_JA[lm.pareto.recommended]}案</span>` : ""}</h3>
    ${["broad", "standard", "narrow"].filter((v) => lm.variants[v]).map((v) => `<h4 style="margin:.6rem 0 .3rem">${VARIANT_JA[v]}案 <span class="dim" style="font-size:.75rem">${esc(lm.variants[v].source)}</span></h4>${metricsKpis(lm.variants[v])}`).join("")}
    <div class="note">停止判定: <b>${stop?.should_stop ? "停止条件を満たす" : "継続"}</b> — ${esc((stop?.reasons || []).join("、"))}。目標レンジ ${esc((stop?.population_range || []).join("〜"))} 件（調査種別: ${esc(b.purpose.label)}）</div>
  </div>` : ""}
  <div class="card"><h3>成果物</h3>
    <div class="actions">
      <a class="btn" href="/api/cases/${encodeURIComponent(c.case_id)}/report?format=html&download=1">📄 根拠レポート（HTML）</a>
      <a class="btn" href="/api/cases/${encodeURIComponent(c.case_id)}/report?format=md&download=1">📝 根拠レポート（Markdown）</a>
      <a class="btn" href="/api/cases/${encodeURIComponent(c.case_id)}/excel">📊 Excel 設計シート</a>
      <label class="btn" style="cursor:pointer">📥 設計シートを読み戻す<input type="file" id="excel-upload" accept=".xlsx" hidden></label>
    </div>
    <p class="dim">設計シートの「採否」「理由コード」「人の総合」「変換候補の採否」列を編集して読み戻すと、判断として取り込みます（ゲートの確定は画面で行います）。</p>
  </div>`;
}

/* ---------------- G1 ---------------- */
function renderG1(b) {
  const axes = b.axes, fixed = axes.some((a) => a.fixed);
  const p1 = b.case.settings?.p1;
  return `
  <div class="card"><h3>G1 観点の確認 <span class="sp"></span>
      <button class="btn" id="btn-structure" ${b.case.status === "finalized" ? "disabled" : ""}>✨ P1 で分解・提案${axes.length ? "（やり直し）" : ""}</button></h3>
    <p class="dim">発明を課題・構成・効果・用途に分解し、検索観点（必須／補助）を決めます。確定後は反復中に変更しません（クエリドリフト防止）。必須観点は AND 軸、補助観点は狭める時だけ使います。</p>
    ${p1 ? `<div class="note">P1: 経路 ${esc(p1.mode)}${p1.low_confidence ? " ・ <b>確信度低（LLM 申告）</b>" : ""}${p1.notes ? " ・ " + esc(p1.notes) : ""}</div>` : ""}
    ${fixed ? `<div class="okbox">観点は確定済み（以後固定）。変更が必要なら新しい案件として分岐してください。</div>` : ""}
    <div id="axes-editor">
      <div class="axis-row dim" style="font-size:.72rem"><span>ID</span><span>観点名</span><span>種別</span><span>定義（一文）</span><span>根拠（入力文の箇所）</span><span>代表語（カンマ区切り）</span><span></span></div>
      ${axes.map((a) => axisRow(a, fixed)).join("")}
    </div>
    ${fixed ? "" : `<div class="actions" style="margin-top:.6rem"><button class="btn small" id="btn-add-axis">＋ 観点を追加</button><span class="sp"></span><button class="btn primary" id="btn-g1" ${axes.length ? "" : "disabled"}>G1 確定（観点を固定して G2 へ）</button></div>`}
  </div>`;
}
function axisRow(a, fixed) {
  const dis = fixed ? "disabled" : "";
  return `<div class="axis-row" data-axis>
    <input class="axis-id mono" value="${esc(a.axis_id)}" ${dis}>
    <input class="axis-name" value="${esc(a.name)}" ${dis}>
    <select class="axis-kind" ${dis}><option value="required" ${a.kind === "required" ? "selected" : ""}>必須</option><option value="auxiliary" ${a.kind === "auxiliary" ? "selected" : ""}>補助</option></select>
    <textarea class="axis-def" rows="2" ${dis}>${esc(a.definition)}</textarea>
    <textarea class="axis-ev" rows="2" ${dis}>${esc(a.evidence)}</textarea>
    <input class="axis-terms" value="${esc((a.terms || []).join(", "))}" ${dis}>
    ${fixed ? "<span></span>" : `<button class="icon-btn axis-del" title="削除">✕</button>`}
  </div>`;
}
function collectAxes() {
  return $$("[data-axis]").map((r) => ({
    axis_id: $(".axis-id", r).value.trim(), name: $(".axis-name", r).value.trim(), kind: $(".axis-kind", r).value,
    definition: $(".axis-def", r).value.trim(), evidence: $(".axis-ev", r).value.trim(),
    terms: $(".axis-terms", r).value.split(/[,、，]/).map((s) => s.trim()).filter(Boolean),
  })).filter((a) => a.axis_id && a.name);
}

/* ---------------- G2 ---------------- */
function statusRadio(c) {
  return ["adopted", "rejected", "candidate"].map((s) => `<label class="check" style="display:inline-flex;margin-right:.3rem"><input type="radio" name="st-${esc(c.candidate_id)}" value="${s}" ${c.status === s ? "checked" : ""}>${KIND_JA[s]}</label>`).join("");
}
function reasonSelect(c) {
  const rc = state.bundle.config.reason_codes || {};
  return `<select class="tiny reason" data-id="${esc(c.candidate_id)}"><option value="">—</option>${Object.entries(rc).map(([k, v]) => `<option value="${k}" ${c.reason_code === k ? "selected" : ""}>${k} ${esc(v)}</option>`).join("")}</select>`;
}
function axisSelect(c, axes) {
  return `<select class="tiny axis" data-id="${esc(c.candidate_id)}">${axes.map((a) => `<option value="${esc(a.axis_id)}" ${a.axis_id === c.axis_id ? "selected" : ""}>${esc(a.axis_id)}</option>`).join("")}</select>`;
}
function statCells(c) {
  return `<td class="num" title="適合側 r / 出現 n（採点済み N=${fmt(c.N)}, 適合 R=${fmt(c.R)}）">${c.n === null || c.n === undefined ? "—" : `${fmt(c.r)}/${fmt(c.n)}`}</td><td class="num">${fmt(c.rsj_w, 2)}</td><td class="num">${fmt(c.offer_w, 2)}</td><td class="num">${fmt(c.w_sample, 2)}</td>`;
}
function renderG2(b) {
  const axes = b.axes;
  const f = state.filters;
  const filt = (c) => (!f.reviewOnly || c.needs_review) && (!f.candOnly || c.status === "candidate");
  const terms = b.candidates.filter((c) => c.kind === "term" && filt(c));
  const codes = b.candidates.filter((c) => c.kind === "code" && filt(c));
  const it = b.case.iteration;
  const drop = b.case.settings?.broad_drop_axis ?? (b.case.settings?.variant_notes?.broad_drop_axis ?? "");
  const canDecide = ["g2_pending", "g3_pending", "g4_pending"].includes(b.case.status);
  const rowTerm = (c) => `<tr class="${c.status} ${c.needs_review ? "review" : ""}">
    <td>${axisSelect(c, axes)}</td><td><b>${esc(c.value)}</b>${c.note ? `<div class="dim" style="font-size:.7rem">${esc(c.note)}</div>` : ""}</td><td>${esc(c.variant_kind || "")}</td>
    <td><span class="chip src-${esc((c.origin || "").split(":")[0])}">${esc(c.origin)}</span>${c.origin_doc ? `<div class="dim mono" style="font-size:.68rem">${esc(c.origin_doc)}</div>` : ""}</td>
    ${statCells(c)}<td class="num">${fmt(c.flip_rate, 2)}</td><td>${c.needs_review ? `<span class="chip warn">要確認</span>` : ""}</td>
    <td>${statusRadio(c)}</td><td>${reasonSelect(c)}</td></tr>`;
  const rowCode = (c) => `<tr class="${c.status} ${c.needs_review ? "review" : ""}">
    <td>${axisSelect(c, axes)}</td><td><b>${esc(c.scheme)}</b> <span class="mono">${esc(c.value)}</span><div class="dim" style="font-size:.7rem">${esc(c.level)}</div></td>
    <td>${esc(c.title || "")}${c.dict_known === false ? ` <span class="chip bad">未照合</span>` : c.dict_known ? ` <span class="chip ok">辞書</span>` : ""}${c.note ? `<div class="dim" style="font-size:.7rem">${esc(c.note)}</div>` : ""}</td>
    <td><span class="chip src-${esc((c.origin || "").split(":")[0])}">${esc(c.origin)}</span></td>
    ${statCells(c)}<td class="num">${fmt(c.flip_rate, 2)}</td><td>${c.needs_review ? `<span class="chip warn">要確認</span>` : ""}</td>
    <td>${statusRadio(c)}</td><td>${reasonSelect(c)}</td></tr>`;
  return `
  <div class="card"><h3>G2 候補の採否（反復 v${it}） <span class="sp"></span>
      <button class="btn" id="btn-expand" ${["axes_fixed", "g2_pending"].includes(b.case.status) ? "" : "disabled"}>🔎 P2／P3 で候補を展開${b.candidates.length ? "（追加）" : ""}</button></h3>
    <p class="dim">語候補（LLM・辞書・既知文献由来、2 回目以降は RSJ 統計付き）と分類候補（辞書照合済み、フリップ率、既知文献での出現数）をチェック方式で採否します。RSJ w ＞ 0 は適合側に偏る語（U の内側での識別力）。要確認は フリップ率 ＞ ${b.config.flip_threshold} または辞書未照合、標本統計と符号不一致。</p>
    <div class="actions">
      <label class="check"><input type="checkbox" id="f-review" ${f.reviewOnly ? "checked" : ""}>要確認のみ</label>
      <label class="check"><input type="checkbox" id="f-cand" ${f.candOnly ? "checked" : ""}>保留のみ</label>
      <span class="sp"></span>
      <button class="btn small" id="btn-adopt-known">辞書照合済み・入力由来を一括採用</button>
      <button class="btn small" id="btn-add-term">＋ 語を追加</button><button class="btn small" id="btn-add-code">＋ 分類を追加</button>
    </div>
  </div>
  <div class="card"><h3>語候補（${terms.length}）</h3>
    <div class="table-wrap"><table><thead><tr><th>観点</th><th>語</th><th>表記種別</th><th>由来</th><th class="num">r/n</th><th class="num">RSJ w</th><th class="num">OW</th><th class="num">標本 w</th><th class="num">フリップ</th><th></th><th>採否</th><th>理由</th></tr></thead>
    <tbody>${terms.map(rowTerm).join("") || `<tr><td colspan="12" class="dim">候補がありません</td></tr>`}</tbody></table></div>
  </div>
  <div class="card"><h3>分類候補（${codes.length}）${b.dictionary.degraded ? `<span class="chip warn">辞書は縮退モード（観測コードのみ）</span>` : ""}</h3>
    <div class="table-wrap"><table><thead><tr><th>観点</th><th>体系・コード</th><th>タイトル（辞書）</th><th>由来</th><th class="num">r/n</th><th class="num">RSJ w</th><th class="num">OW</th><th class="num">標本 w</th><th class="num">フリップ</th><th></th><th>採否</th><th>理由</th></tr></thead>
    <tbody>${codes.map(rowCode).join("") || `<tr><td colspan="12" class="dim">候補がありません</td></tr>`}</tbody></table></div>
  </div>
  <div class="card"><h3>3 案の組立方針</h3>
    <div class="inline">広め案で外す必須観点: <select id="broad-drop" class="tiny"><option value="">自動（分類コードを持つ観点を残す）</option>${axes.filter((a) => a.kind === "required").map((a) => `<option value="${esc(a.axis_id)}" ${drop === a.axis_id ? "selected" : ""}>${esc(a.axis_id)} ${esc(a.name)}</option>`).join("")}</select></div>
    <p class="dim">標準: 必須観点すべて（AB・CL、語と分類は OR）／広め: 必須観点を 1 つ減らし、全文（TX）・一段粗い分類／狭め: 必須＋補助観点、分類のある観点は 語 AND 分類。</p>
    <div class="actions"><span class="sp"></span><button class="btn primary" id="btn-g2" ${canDecide ? "" : "disabled"}>G2 確定 → 検索式（3 案）を組む</button></div>
  </div>`;
}
function collectG2() {
  const decisions = [];
  $$("[data-axis]").length; // no-op
  state.bundle.candidates.forEach((c) => {
    const st = $(`input[name="st-${c.candidate_id}"]:checked`);
    const reason = $(`select.reason[data-id="${c.candidate_id}"]`);
    const axis = $(`select.axis[data-id="${c.candidate_id}"]`);
    if (!st) return;
    const d = { candidate_id: c.candidate_id, status: st.value, reason_code: reason ? reason.value : "", axis_id: axis ? axis.value : c.axis_id };
    if (d.status !== c.status || d.reason_code !== (c.reason_code || "") || d.axis_id !== c.axis_id) decisions.push(d);
    else if (d.status !== "candidate") decisions.push(d);
  });
  return decisions;
}

/* ---------------- G3 ---------------- */
function renderG3(b) {
  const it = b.case.iteration;
  const qs = b.queries.filter((q) => q.iteration === it);
  const runs = b.runs.filter((r) => r.iteration === it);
  const lm = b.latest_metrics;
  const hasPop = runs.some((r) => r.n_docs > 0);
  const hasJudg = b.judgment_table.some((j) => j.iteration === it && j.llm_overall !== null);
  return `
  <div class="card"><h3>G3 DB で実行（反復 v${it}） <span class="sp"></span>
    <button class="btn" id="btn-build" ${["g2_pending", "g3_pending", "g4_pending"].includes(b.case.status) ? "" : "disabled"}>🧩 検索式を組み直す</button></h3>
    <p class="dim">3 案の式をコピーして DB（J-PlatPat など）に貼り、実行してください。件数を入力し、結果 CSV（名称・要約・FI／Fターム／IPC を含むもの）を取り込むと、既知文献再現率・プール再現率を計算し、逆算に使います。<b>本システムは DB を自動操作しません。</b></p>
    ${b.local_index_size ? `<div class="actions"><button class="btn" id="btn-run-local">⚡ オフライン母集団（local_index ${b.local_index_size} 件）で 3 案を実行</button><span class="dim">テスト・デモ・自前索引用。局所照合（部分一致・階層一致）で DB 照合と差があります。</span></div>` : ""}
  </div>
  ${qs.length ? `<div class="grid3">${["broad", "standard", "narrow"].map((v) => { const q = qs.find((x) => x.variant === v); if (!q) return ""; return variantCard(q, runs.filter((r) => r.variant === v), lm?.variants?.[v], b); }).join("")}</div>` : `<div class="card dim">検索式がまだありません。G2 を確定すると 3 案が組まれます。</div>`}
  <div class="card"><h3>採点と分析 <span class="sp"></span>
      <button class="btn" id="btn-score" ${hasPop ? "" : "disabled"}>🧮 採点（P4: 上位 K=${b.config.top_k} 件＋無作為標本）</button>
      <button class="btn primary" id="btn-analyze" ${hasPop ? "" : "disabled"}>📈 分析（RSJ 逆算・変換候補・推定再現率）→ G4</button></h3>
    <p class="dim">採点は母集団（広め案の結果）の上位 K 件と無作為標本に LLM（P4、n=${(state.config.llm.n_samples || {}).P4 || 3} 多数決）で適合度 0〜3 を付けます。分析は適合群と非適合群の語・分類の分布差（RSJ）から次の式を逆算し、変換候補を局所評価します。</p>
    ${hasJudg ? `<div class="okbox">この反復の採点あり（判定 ${b.judgment_table.filter((j) => j.iteration === it && j.llm_overall !== null).length} 件）。採点は追加実行できます。</div>` : ""}
  </div>`;
}
function variantCard(q, runs, m, b) {
  const v = q.variant;
  const rends = q.renderings; const dialects = [...new Set(rends.map((r) => r.dialect))];
  const cur = state.qdialect[v] || (dialects.includes(b.case.dialect) ? b.case.dialect : dialects[0]);
  const parts = rends.filter((r) => r.dialect === cur);
  const s = q.summary;
  const rec = b.latest_metrics?.pareto?.recommended === v;
  return `<div class="card variant-card ${v} ${rec ? "recommended" : ""}" data-variant="${v}">
    <h3>${VARIANT_JA[v]}案 ${rec ? `<span class="chip ok">推奨</span>` : ""}<span class="sp"></span><span class="dim mono" style="font-size:.7rem">${esc(q.query_id)}</span></h3>
    <div class="dim" style="font-size:.76rem">${(s?.blocks || []).map((bl) => `<span class="chip ${bl.required ? "req" : "aux"}">${esc(bl.axis_id)} ${esc(bl.axis_name)}: 語${bl.terms.length} 分類${bl.codes.length} ${bl.join} ${bl.fields.join("/")}</span>`).join("")}</div>
    <div class="qtabs" style="margin-top:.4rem">${dialects.map((d) => `<button data-dialect="${esc(d)}" class="${d === cur ? "active" : ""}">${esc(d)}</button>`).join("")}</div>
    ${parts.map((p) => `<div class="inline dim" style="font-size:.72rem">${parts.length > 1 ? `分割 ${p.part_no}/${parts.length} ・ ` : ""}${p.chars} 文字 ・ 往復検証 ${p.roundtrip_ok ? `<span class="chip ok">OK</span>` : `<span class="chip bad">NG</span>`}<span class="sp"></span><button class="btn small copy" data-text="${esc(p.text)}">📋 コピー</button></div><pre class="query">${esc(p.text)}</pre>`).join("")}
    ${m ? metricsKpis(m) : ""}
    ${runs.length ? `<div class="dim" style="font-size:.74rem;margin-top:.4rem">取り込み: ${runs.map((r) => `${esc(r.source)} ${r.hit_count} 件（文献 ${r.n_docs}）${r.csv_path ? " " + esc(r.csv_path) : ""}`).join(" / ")}</div>` : ""}
    <div class="inline" style="margin-top:.5rem">
      <input type="number" class="tiny hits" placeholder="件数" min="0">
      <label class="btn small" style="cursor:pointer">CSV を選択<input type="file" class="csvfile" accept=".csv,.tsv,.txt" hidden></label>
      <span class="csvname dim" style="font-size:.72rem"></span>
      <button class="btn small primary import">取り込み</button>
    </div>
  </div>`;
}

/* ---------------- G4 ---------------- */
function renderG4(b) {
  const lm = b.latest_metrics;
  const it = b.case.iteration;
  if (!lm) return `<div class="card dim">まだ分析がありません。G3 で実行結果を取り込み、「採点」→「分析」を実行してください。</div>`;
  const tfs = b.transforms.filter((t) => t.iteration === lm.iteration);
  const jt = b.judgment_table.filter((j) => !j.seed);
  const st = lm.stats || {}; const est = lm.estimates || {}; const stop = lm.stop || {};
  const canDecide = b.case.status === "g4_pending";
  const jrow = (j) => `<tr class="${j.needs_review ? "review" : ""} ${j.relevant ? "" : "rejected"}">
    <td class="mono">${esc(j.doc_id)}</td><td>${esc(j.title)}<div class="dim" style="font-size:.7rem">${esc(j.rationale || "")}</div></td><td>${esc(j.selection)}</td>
    <td class="num">${fmt(j.llm_overall)}</td><td class="dim" style="font-size:.72rem">${esc(j.llm_per_axis || "")}</td><td class="num">${fmt(j.flip_rate, 2)}${j.needs_review ? ` <span class="chip warn">要確認</span>` : ""}</td>
    <td><select class="tiny human" data-doc="${esc(j.doc_id)}"><option value="">—</option>${[0, 1, 2, 3].map((n) => `<option value="${n}" ${j.human_overall === n ? "selected" : ""}>${n}</option>`).join("")}</select></td>
    <td><input class="short comment" data-doc="${esc(j.doc_id)}" value="${esc(j.comment || "")}" placeholder="コメント"></td></tr>`;
  const trow = (t) => `<tr class="${t.status} ${t.regression ? "review" : ""}">
    <td>${t.status === "candidate" && canDecide ? `<input type="checkbox" class="tf" data-id="${esc(t.transform_id)}">` : `<span class="chip">${esc(KIND_JA[t.status] || t.status)}</span>`}</td>
    <td><b>${esc(t.op)}</b><div class="dim mono" style="font-size:.7rem">${esc(targetText(t.target))}</div></td>
    <td><span class="chip src-${esc(t.source)}">${esc(t.source)}</span> ${esc(t.direction)}</td>
    <td class="num">${fmt(t.pred_hits)}</td><td class="num">${pct(t.pred_recall_pool)}</td><td class="num">${pct(t.pred_p_at_k)}</td>
    <td>${t.local_eval ? `<span class="chip ok">局所評価</span>` : `<span class="chip">DB 実行要</span>`}${t.regression ? ` <span class="chip bad">退行</span>` : ""}</td>
    <td class="dim" style="font-size:.74rem">${esc(t.reason)}</td></tr>`;
  const statRow = (s, kind) => `<tr><td>${kind}</td><td>${esc(kind === "語" ? s.feature : `${s.scheme} ${s.feature}`)}${s.title ? `<div class="dim" style="font-size:.7rem">${esc(s.title)}</div>` : ""}</td><td class="num">${s.r}</td><td class="num">${s.n}</td><td class="num">${fmt(s.w, 2)}</td><td class="num">${fmt(s.ow, 2)}</td><td class="num">${fmt(s.w_sample, 2)}</td><td>${s.in_query ? `<span class="chip">式に含む</span>` : ""}${s.needs_review ? ` <span class="chip warn">要確認</span>` : ""}</td></tr>`;
  return `
  <div class="card"><h3>G4 確定／再反復（分析は反復 v${lm.iteration}）${lm.pareto?.recommended ? `<span class="chip ok">推奨: ${VARIANT_JA[lm.pareto.recommended]}案</span>` : ""}</h3>
    ${["broad", "standard", "narrow"].filter((v) => lm.variants[v]).map((v) => `<h4 style="margin:.6rem 0 .3rem">${VARIANT_JA[v]}案 <span class="dim" style="font-size:.75rem">${esc(lm.variants[v].source)} ・ 複雑さ ${lm.variants[v].complexity}${lm.variants[v].seeds_missing?.length ? ` ・ <span class="chip bad">既知文献 取り逃し: ${esc(lm.variants[v].seeds_missing.join(", "))}</span>` : ""}</span></h4>${metricsKpis(lm.variants[v])}
      ${est[v] ? `<div class="dim" style="font-size:.78rem;margin-top:.3rem">推定再現率（U 基準、標本 m=${est[v].m}, 適合 s=${est[v].s}, 式内 t=${est[v].t}）: <b>${pct(est[v].recall_hat)}</b> [${pct(est[v].ci_low)}, ${pct(est[v].ci_high)}] ${est[v].low_confidence ? `<span class="chip warn">低信頼（s &lt; ${b.config.sample.s_min}）</span>` : ""} ・ U 内の適合総数推定 ${fmt(est[v].est_relevant_total, 1)} / ${est[v].population_size}</div>` : ""}`).join("")}
    <div class="note" style="margin-top:.6rem">停止判定: <b>${stop.should_stop ? "停止条件を満たす" : "継続"}</b> — ${esc((stop.reasons || []).join("、"))}<br>
      採点済み N=${lm.judged.N}（適合 R=${lm.judged.R}）、プール ${lm.judged.pool} 件、標本 m=${lm.judged.sample_m}（適合 s=${lm.judged.sample_s}）、要確認 ${lm.judged.needs_review} 件${lm.retention !== null ? ` ・ 初回上位適合の保持率 ${pct(lm.retention)}${lm.retention_warning ? ` <span class="chip warn">ドリフト警告</span>` : ""}` : ""}${lm.llm_agreement_with_stats !== null && lm.llm_agreement_with_stats !== undefined ? ` ・ LLM 提案と統計候補の一致率 ${pct(lm.llm_agreement_with_stats)}` : ""}
    </div>
  </div>
  <div class="card"><h3>採点（適合度 0〜3）<span class="dim" style="font-size:.75rem">LLM の採点を確認し、人の総合を上書きできます（集計では人の判定を優先）</span><span class="sp"></span><label class="check"><input type="checkbox" id="j-review">要確認のみ</label></h3>
    <div class="table-wrap"><table id="judg-table"><thead><tr><th>文献</th><th>名称／根拠</th><th>抽出</th><th class="num">LLM総合</th><th>観点別</th><th class="num">フリップ</th><th>人の総合</th><th>コメント</th></tr></thead><tbody>${jt.map(jrow).join("") || `<tr><td colspan="8" class="dim">採点がありません</td></tr>`}</tbody></table></div>
  </div>
  <div class="card"><h3>変換候補（RSJ 逆算・決定木・LLM 提案を同じ評価器で比較）</h3>
    <p class="dim">狭める方向は母集団 U の手元データで局所評価済み（DB 実行不要）。広げる方向は次反復で DB 実行が必要（1 反復あたり ${b.config.budget.db_runs_per_iteration} 件まで）。プールの文献を落とす変換は退行として棄却されます。</p>
    <div class="table-wrap"><table><thead><tr><th>採用</th><th>操作・対象</th><th>発生源</th><th class="num">予測件数</th><th class="num">予測プール再現率</th><th class="num">予測P@K</th><th>評価</th><th>理由</th></tr></thead><tbody>${tfs.map(trow).join("") || `<tr><td colspan="8" class="dim">候補がありません</td></tr>`}</tbody></table></div>
  </div>
  <div class="grid2">
    <div class="card"><h3>RSJ 統計（上位）</h3><p class="dim" style="font-size:.76rem">${esc(st.note || "")}</p>
      <div class="table-wrap" style="max-height:320px"><table><thead><tr><th></th><th>素性</th><th class="num">r</th><th class="num">n</th><th class="num">w</th><th class="num">OW</th><th class="num">標本 w</th><th></th></tr></thead><tbody>
      ${(st.terms || []).map((s) => statRow(s, "語")).join("")}${(st.codes || []).map((s) => statRow(s, "分類")).join("")}
      ${(st.negative_terms || []).slice(0, 5).map((s) => statRow(s, "語(負)")).join("")}${(st.negative_codes || []).slice(0, 3).map((s) => statRow(s, "分類(負)")).join("")}</tbody></table></div>
    </div>
    <div class="card"><h3>決定木の適合経路・CAL</h3>
      ${(lm.dnf || []).length ? `<ul style="margin:.2rem 0;padding-left:1.1rem;font-size:.8rem">${lm.dnf.slice(0, 6).map((p) => `<li>${p.literals.map((l) => (l.present ? "" : "¬") + esc(l.feature)).join(" ∧ ")} <span class="dim">（${p.n} 件中 ${p.n_pos} 件適合）</span></li>`).join("")}</ul>` : `<p class="dim">決定木: 経路なし（採点済み文献が少ない）</p>`}
      ${lm.cal?.ok ? `<p style="font-size:.8rem">CAL 分類器（正例 ${lm.cal.n_pos} / 負例 ${lm.cal.n_neg}${lm.cal.provisional_negatives ? "・暫定負例" : ""}）係数上位: ${(lm.cal.top_features || []).slice(0, 10).map((f) => `<span class="chip">${esc(f.feature)} ${f.weight}</span>`).join("")}</p>
        <p class="dim" style="font-size:.76rem">次に判定すべき文献（スコア順）: ${(lm.cal.batch || []).slice(0, 10).map((d) => `<span class="chip mono">${esc(d)}</span>`).join("")}</p>` : `<p class="dim">CAL: 採点済み文献が 10 件以上になると動きます</p>`}
    </div>
  </div>
  <div class="card"><h3>判断</h3>
    <div class="actions">
      <button class="btn" id="btn-g4-iterate" ${canDecide ? "" : "disabled"}>🔁 再反復（採用した変換を反映し、RSJ 統計付きで G2 へ）</button>
      <span class="sp"></span>
      <button class="btn primary" id="btn-g4-finalize" ${canDecide ? "" : "disabled"}>✅ 確定（3 案と根拠レポートを版確定）</button>
    </div>
    <p class="dim">確定・再反復のどちらでも、人の採点上書きと変換候補の採否を記録します。再反復では G1 の観点は変更しません。</p>
  </div>`;
}
function targetText(t) { return Object.entries(t || {}).filter(([k, v]) => !["assigned", "origin"].includes(k) && v !== "" && v !== null && !(Array.isArray(v) && !v.length)).map(([k, v]) => `${k}=${Array.isArray(v) ? (typeof v[0] === "object" ? v.length + "件" : v.join(",")) : v}`).join(" "); }
function collectG4() {
  const judgments = [];
  $$("select.human").forEach((s) => { if (s.value !== "") judgments.push({ doc_id: s.dataset.doc, overall: Number(s.value), comment: ($(`input.comment[data-doc="${s.dataset.doc}"]`) || {}).value || "" }); });
  const transforms = $$("input.tf").map((c) => ({ transform_id: c.dataset.id, status: c.checked ? "adopted" : "rejected" }));
  const prev = state.bundle.judgment_table;
  return { judgments: judgments.filter((j) => { const p = prev.find((x) => x.doc_id === j.doc_id); return !p || p.human_overall !== j.overall || (p.comment || "") !== j.comment; }), transforms };
}

/* ---------------- レポート／ログ ---------------- */
function renderReport(b) {
  const c = b.case;
  return `<div class="card"><h3>根拠レポート <span class="sp"></span>
      <a class="btn small" href="/api/cases/${encodeURIComponent(c.case_id)}/report?format=html&download=1">HTML を保存</a>
      <a class="btn small" href="/api/cases/${encodeURIComponent(c.case_id)}/report?format=md&download=1">Markdown を保存</a>
      <a class="btn small" href="/api/cases/${encodeURIComponent(c.case_id)}/excel">Excel 設計シート</a></h3>
    <iframe class="report-frame" src="/api/cases/${encodeURIComponent(c.case_id)}/report?format=html" title="report"></iframe></div>`;
}
function renderLog(b) {
  return `
  ${b.pending_prompts.length ? `<div class="card"><h3>LLM 返答待ち（manual モード）</h3><div class="actions"><button class="btn primary" id="btn-open-manual">返答を貼り付ける</button></div></div>` : ""}
  <div class="grid2">
    <div class="card"><h3>判断ログ（${b.decisions.length}）</h3><div class="log-list">${b.decisions.slice().reverse().map((d) => `<div>${esc(d.created_at)} v${d.iteration} <b>${esc(d.gate)}</b> ${esc(d.actor)} ${esc(d.action)} ${esc(JSON.stringify(d.payload).slice(0, 160))}</div>`).join("") || "—"}</div></div>
    <div class="card"><h3>LLM 呼び出し（${b.llm_calls.length}）</h3><div class="log-list">${b.llm_calls.map((l) => `<div>${esc(l.created_at)} ${esc(l.prompt_id)} ${esc(l.mode)}/${esc(l.model)} n=${l.n_samples} flip=${fmt(l.flip_rate, 2)} ${l.masked ? "masked " : ""}${esc(l.status)} ${l.duration_ms}ms</div>`).join("") || "—"}</div></div>
  </div>
  <div class="card"><h3>操作ログ</h3><div class="log-list">${b.logs.map((l) => `<div>${esc(l.created_at)} v${l.iteration} ${esc(l.gate)} ${esc(l.actor)} <b>${esc(l.action)}</b> ${esc(l.message)}</div>`).join("") || "—"}</div></div>
  <div class="card"><h3>版履歴（${b.queries.length}）</h3><div class="table-wrap" style="max-height:260px"><table><thead><tr><th>反復</th><th>案</th><th>query_id</th><th>親</th><th>作成</th></tr></thead><tbody>${b.queries.map((q) => `<tr><td>${q.iteration}</td><td>${VARIANT_JA[q.variant]}</td><td class="mono">${esc(q.query_id)}</td><td class="mono dim">${esc(q.parent_query_id || "—")}</td><td class="dim">${esc(q.created_at)}</td></tr>`).join("")}</tbody></table></div></div>`;
}

/* ---------------- イベント束縛 ---------------- */
function bindTab(tab, b) {
  const cid = encodeURIComponent(b.case.case_id);
  $$("[data-go]").forEach((btn) => btn.addEventListener("click", () => { state.tab = btn.dataset.go; renderCase(); }));
  $$("button.copy").forEach((btn) => btn.addEventListener("click", () => copyText(btn.dataset.text)));
  const up = $("#excel-upload");
  if (up) up.addEventListener("change", async () => { const f = up.files[0]; if (!f) return; const b64 = await readFileB64(f); try { await act("設計シートを読み込み", () => api(`/api/cases/${cid}/excel`, { xlsx_base64: b64 }), async (r) => { toast(`取り込み: 採否 ${r.decisions} 件、判定 ${r.judgments} 件、変換 ${r.transforms} 件`, "ok"); await refresh(); }); } catch {} });

  if (tab === "g1") {
    $("#btn-structure")?.addEventListener("click", () => act("P1 で構造化…", (c) => api(`/api/cases/${cid}/structure`, { confirmed: c }), async () => { await refresh("g1"); toast("観点を提案しました。確認・修正して確定してください", "ok"); }));
    $("#btn-add-axis")?.addEventListener("click", () => { const used = collectAxes().map((a) => a.axis_id); let id = "A"; while (used.includes(id)) id = String.fromCharCode(id.charCodeAt(0) + 1); $("#axes-editor").insertAdjacentHTML("beforeend", axisRow({ axis_id: id, name: "", kind: "auxiliary", definition: "", evidence: "", terms: [] }, false)); bindTab("g1", b); });
    $$(".axis-del").forEach((btn) => btn.onclick = () => btn.closest("[data-axis]").remove());
    $("#btn-g1")?.addEventListener("click", () => { const axes = collectAxes(); if (!axes.length) return toast("観点がありません", "error"); if (!confirm("観点を確定します。以後この案件では観点を変更しません。よろしいですか？")) return; act("観点を確定", () => api(`/api/cases/${cid}/g1`, { axes }), async () => { await refresh("g2"); toast("観点を確定しました。G2: 候補を展開してください", "ok"); }); });
  }
  if (tab === "g2") {
    $("#btn-expand")?.addEventListener("click", () => act("P2／P3 で候補を展開…", (c) => api(`/api/cases/${cid}/expand`, { confirmed: c }), async (r) => { await refresh("g2"); toast(`候補を追加: 語 ${r.added.terms}、分類 ${r.added.codes}${r.added.rsj_terms ? `、RSJ 語 ${r.added.rsj_terms}` : ""}`, "ok"); }));
    $("#f-review")?.addEventListener("change", (e) => { state.filters.reviewOnly = e.target.checked; renderCase(); });
    $("#f-cand")?.addEventListener("change", (e) => { state.filters.candOnly = e.target.checked; renderCase(); });
    $("#btn-adopt-known")?.addEventListener("click", () => { b.candidates.forEach((c) => { if (c.status !== "candidate") return; const ok = c.kind === "term" ? /^(input|seed|human)/.test(c.origin) || (/^rsj/.test(c.origin) && c.rsj_w > 0 && !c.needs_review) : c.dict_known && !c.needs_review; const el = $(`input[name="st-${c.candidate_id}"][value="${ok ? "adopted" : "rejected"}"]`); if (el) el.checked = true; }); toast("一括設定しました（確定はまだです）"); });
    $("#btn-add-term")?.addEventListener("click", () => genericForm("語を追加", [["axis_id", "観点", "select", b.axes.map((a) => [a.axis_id, `${a.axis_id} ${a.name}`])], ["text", "語", "text"]], async (v) => { await act("語を追加", () => api(`/api/cases/${cid}/g2`, { decisions: [], extra: { terms: [v] } }), async () => { await refresh("g2"); }); }));
    $("#btn-add-code")?.addEventListener("click", () => genericForm("分類を追加", [["axis_id", "観点", "select", b.axes.map((a) => [a.axis_id, `${a.axis_id} ${a.name}`])], ["scheme", "体系", "select", [["FI", "FI"], ["FT", "Fターム"], ["IPC", "IPC"]]], ["code", "コード", "text"]], async (v) => { await act("分類を追加", () => api(`/api/cases/${cid}/g2`, { decisions: [], extra: { codes: [v] } }), async () => { await refresh("g2"); }); }));
    $("#btn-g2")?.addEventListener("click", () => { const decisions = collectG2(); const drop = $("#broad-drop").value; act("採否を記録し検索式を組む", () => api(`/api/cases/${cid}/g2`, { decisions, broad_drop_axis: drop }), async (r) => { await refresh("g3"); toast(`3 案を組みました${r.warnings?.length ? "（警告: " + r.warnings.join(" / ") + "）" : ""}`, r.warnings?.length ? "error" : "ok"); }); });
  }
  if (tab === "g3") {
    $("#btn-build")?.addEventListener("click", () => act("検索式を組む", () => api(`/api/cases/${cid}/build`, {}), async () => { await refresh("g3"); }));
    $("#btn-run-local")?.addEventListener("click", () => act("オフライン母集団で実行", () => api(`/api/cases/${cid}/runs_local`, {}), async (r) => { await refresh("g3"); toast(Object.entries(r).map(([v, x]) => `${VARIANT_JA[v]} ${x.hit_count} 件`).join(" / "), "ok"); }));
    $$(".variant-card").forEach((card) => {
      const v = card.dataset.variant;
      $$(".qtabs button", card).forEach((btn) => btn.addEventListener("click", () => { state.qdialect[v] = btn.dataset.dialect; renderCase(); }));
      const file = $(".csvfile", card); file?.addEventListener("change", () => { $(".csvname", card).textContent = file.files[0]?.name || ""; });
      $(".import", card)?.addEventListener("click", async () => {
        const hits = $(".hits", card).value; const f = file?.files[0];
        if (!f && hits === "") return toast("件数か CSV を入力してください", "error");
        const body = { variant: v, hit_count: hits === "" ? null : Number(hits), filename: f?.name || "" };
        if (f) body.csv_base64 = await readFileB64(f);
        try { await act("実行結果を取り込み", () => api(`/api/cases/${cid}/runs`, body), async (r) => { await refresh("g3"); toast(`${VARIANT_JA[v]}案: ${r.hit_count} 件（文献 ${r.n_docs}）既知文献再現率 ${pct(r.recall_seed)}${r.info?.missing?.length ? " ・ 未検出列: " + r.info.missing.join(",") : ""}`, r.seeds_missing?.length ? "error" : "ok"); }); } catch {}
      });
    });
    $("#btn-score")?.addEventListener("click", () => act("P4 で採点中…（文献数に応じて時間がかかります）", (c) => api(`/api/cases/${cid}/score`, { confirmed: c }), async (r) => { await refresh("g3"); toast(`採点 ${r.judged} 件（要確認 ${r.needs_review}、標本 +${r.sample_added}、プール ${r.pool}）`, "ok"); }));
    $("#btn-analyze")?.addEventListener("click", () => act("分析中…", (c) => api(`/api/cases/${cid}/analyze`, { confirmed: c }), async () => { await refresh("g4"); toast("分析しました。G4 で確認してください", "ok"); }));
  }
  if (tab === "g4") {
    $("#j-review")?.addEventListener("change", (e) => { $$("#judg-table tbody tr").forEach((tr) => { tr.style.display = e.target.checked && !tr.classList.contains("review") ? "none" : ""; }); });
    const decide = (action) => { const p = collectG4(); if (action === "finalize" && !confirm("3 案を版確定します。よろしいですか？")) return; act(action === "finalize" ? "確定中…" : "再反復の準備中…", (c) => api(`/api/cases/${cid}/g4`, { ...p, action, confirmed: c }), async () => { await refresh(action === "finalize" ? "report" : "g2"); toast(action === "finalize" ? "確定しました。根拠レポートを確認してください" : "再反復: RSJ 統計付きの候補を G2 で確認してください", "ok"); }); };
    $("#btn-g4-finalize")?.addEventListener("click", () => decide("finalize"));
    $("#btn-g4-iterate")?.addEventListener("click", () => decide("iterate"));
  }
  if (tab === "log") $("#btn-open-manual")?.addEventListener("click", () => openManual(b.pending_prompts.map((p) => ({ call_key: p.call_key, prompt_id: p.prompt_id, sample_no: p.sample_no, prompt_text: p.prompt_text })), () => refresh()));
}

function genericForm(title, fields, onSubmit) {
  $("#generic-title").textContent = title;
  $("#generic-body").innerHTML = `<form id="generic-form">${fields.map(([k, label, type, opts]) => `<label>${esc(label)}${type === "select" ? `<select name="${k}">${opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>` : `<input name="${k}" required>`}</label>`).join("")}<div class="actions" style="margin-top:.6rem"><button class="btn primary" type="submit">追加</button></div></form>`;
  $("#generic-form").addEventListener("submit", async (e) => { e.preventDefault(); const v = Object.fromEntries(new FormData(e.target).entries()); closeModals(); try { await onSubmit(v); } catch {} });
  openModal("#modal-generic");
}

/* ---------------- manual / confirm ---------------- */
function openManual(prompts, retry) {
  const body = $("#manual-body");
  body.innerHTML = `<p class="dim">プロンプトをコピーして社内許可の LLM 画面に貼り、返答の JSON をそのまま貼り戻してください（スキーマ検証して取り込みます）。同じプロンプトが n サンプル分ある場合は「同じ返答を全サンプルに適用」も使えます。</p>
    ${prompts.map((p, i) => `<details ${i === 0 ? "open" : ""} class="card"><summary><b>${esc(p.prompt_id)}</b> サンプル #${p.sample_no} <span class="dim mono" style="font-size:.7rem">${esc(p.call_key)}</span></summary>
      <div class="actions" style="margin:.4rem 0"><button class="btn small copy-prompt" data-i="${i}">📋 プロンプトをコピー</button></div>
      <pre class="query" style="max-height:200px">${esc(p.prompt_text)}</pre>
      <textarea class="answer" data-key="${esc(p.call_key)}" rows="6" placeholder='{"..."}'></textarea>
      <div class="actions"><label class="check"><input type="checkbox" class="apply-all" data-key="${esc(p.call_key)}">同じ返答を全サンプルに適用</label><span class="sp"></span><button class="btn primary small submit-answer" data-key="${esc(p.call_key)}">この返答を取り込む</button></div></details>`).join("")}`;
  $$(".copy-prompt", body).forEach((b) => b.addEventListener("click", () => copyText(prompts[Number(b.dataset.i)].prompt_text)));
  $$(".submit-answer", body).forEach((b) => b.addEventListener("click", async () => {
    const key = b.dataset.key; const text = $(`textarea.answer[data-key="${key}"]`, body).value.trim(); const all = $(`input.apply-all[data-key="${key}"]`, body).checked;
    if (!text) return toast("返答が空です", "error");
    try { const r = await api(`/api/cases/${encodeURIComponent(state.caseId)}/manual`, { call_key: key, text, apply_all: all }); toast(`取り込みました（残り ${r.pending} 件）`, "ok"); if (r.pending === 0) { closeModals(); await retry(); } else { b.closest("details").remove(); } } catch (e) { toast(e.message, "error"); }
  }));
  openModal("#modal-manual");
}
function openConfirm(info, retry) {
  $("#confirm-body").innerHTML = `<p>production モードのため、外部 LLM への送信前に内容を確認します（${esc(info.prompt_id)}${info.masked ? "、マスキング適用後" : ""}）。発明情報の外部送信は社内ルールに従ってください。</p>
    <pre class="query" style="max-height:340px">${esc(info.prompt_text)}</pre>
    <div class="actions"><button class="btn ghost modal-close2">中止</button><span class="sp"></span><button class="btn primary" id="btn-confirm-send">送信を承認して実行</button></div>`;
  $(".modal-close2", $("#confirm-body")).addEventListener("click", closeModals);
  $("#btn-confirm-send").addEventListener("click", async () => { closeModals(); await retry(); });
  openModal("#modal-confirm");
}

/* ---------------- 設定 ---------------- */
$("#btn-settings").addEventListener("click", async () => { await loadConfig(); renderSettings(); openModal("#modal-settings"); });
function renderSettings() {
  const c = state.config; const llm = c.llm; const db = c.db;
  const sel = (name, val, opts) => `<select name="${name}">${opts.map(([v, l]) => `<option value="${v}" ${String(val) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  $("#settings-body").innerHTML = `
  <form id="settings-form">
    <fieldset><legend>LLM 経路（企画書 §8.3）</legend><div class="settings-grid">
      <label>モード ${sel("llm.mode", llm.mode, [["mock", "mock（オフライン・ヒューリスティック）"], ["manual", "manual（プロンプトを貼り、返答を貼り戻す）"], ["api", "api（OpenAI 互換／Anthropic）"], ["browser", "browser（開発用・production では禁止）"]])}</label>
      <label>プロバイダ ${sel("llm.provider", llm.provider, [["openai_compat", "OpenAI 互換（Ollama/LM Studio/vLLM/Azure）"], ["anthropic", "Anthropic"]])}</label>
      <label>Base URL<input name="llm.base_url" value="${esc(llm.base_url)}" placeholder="http://127.0.0.1:11434/v1"></label>
      <label>モデル<input name="llm.model" value="${esc(llm.model)}" placeholder="例: qwen2.5:14b"></label>
      <label>API キー（${llm.has_api_key ? "設定済み・変更時のみ入力" : "未設定"}）<input name="llm.api_key" type="password" placeholder="••••••"></label>
      <label>Max Tokens<input name="llm.max_tokens" type="number" value="${llm.max_tokens}"></label>
      <label>Temperature<input name="llm.temperature" type="number" step="0.1" value="${llm.temperature}"></label>
      <label>タイムアウト（秒）<input name="llm.request_timeout" type="number" value="${llm.request_timeout}"></label>
      <label class="check"><input type="checkbox" name="llm.use_proxy" ${llm.use_proxy ? "checked" : ""}>プロキシを使う（環境変数 HTTPS_PROXY 等）</label>
      <label>プロキシ URL（空なら環境変数）<input name="llm.proxy_url" value="${esc(llm.proxy_url)}"></label>
      <label>社内 CA 証明書バンドル（パス）<input name="llm.ca_bundle" value="${esc(llm.ca_bundle)}" placeholder="C:\\certs\\company-ca.pem"></label>
      <label>サンプル数 P3 / P4（多数決・フリップ率）<div class="inline"><input class="tiny" name="llm.n_samples.P3" type="number" min="1" max="5" value="${llm.n_samples.P3}"><input class="tiny" name="llm.n_samples.P4" type="number" min="1" max="5" value="${llm.n_samples.P4}"></div></label>
      <label class="check"><input type="checkbox" name="llm.masking.enabled" ${llm.masking.enabled ? "checked" : ""}>マスキングを有効化（案件のマスキング語を置換）</label>
      <label class="check"><input type="checkbox" name="llm.masking.mask_numbers" ${llm.masking.mask_numbers ? "checked" : ""}>具体数値もマスキング</label>
    </div><div class="actions" style="margin-top:.5rem"><button type="button" class="btn small" id="btn-test-llm">接続テスト</button><span id="test-result" class="dim"></span></div></fieldset>
    <fieldset><legend>運用フラグ（§14.2）</legend><div class="settings-grid">
      <label class="check"><input type="checkbox" name="offline" ${c.offline ? "checked" : ""}>offline（ネットワーク呼び出しをせず mock／fixtures を使う）</label>
      <label class="check"><input type="checkbox" name="production" ${c.production ? "checked" : ""}>production（browser 禁止・外部送信前の人の確認ゲート）</label>
      <label class="check"><input type="checkbox" name="exclusions_enabled" ${c.exclusions_enabled ? "checked" : ""}>NOT（除外条件）を許可する（既定: 無効）</label>
      <label>DB モード ${sel("db.mode", db.mode, [["csv", "csv（人が実行して CSV を取り込む・既定）"], ["local_index", "local_index（オフライン母集団）"], ["api", "api（商用DB の API・契約後）"]])}</label>
      <label>DB API Base URL<input name="db.api_base_url" value="${esc(db.api_base_url)}"></label>
      <label>DB アクセス上限（回）<input name="db.access_limit" type="number" value="${db.access_limit}"></label>
    </div></fieldset>
    <fieldset><legend>閾値（初期値（仮）。後方テストで調整）</legend><div class="settings-grid">
      <label>適合ラベル閾値（総合 ≥）<input name="relevance_threshold" type="number" min="1" max="3" value="${c.relevance_threshold}"></label>
      <label>上位 K<input name="top_k" type="number" value="${c.top_k}"></label>
      <label>τ_pool（プール再現率）<input name="tau_pool" type="number" step="0.01" value="${c.tau_pool}"></label>
      <label>ρ_target（推定再現率 下側限界）<input name="rho_target" type="number" step="0.01" value="${c.rho_target}"></label>
      <label>フリップ率閾値<input name="flip_threshold" type="number" step="0.05" value="${c.flip_threshold}"></label>
      <label>収束 T（改善なし反復）<input name="converge_T" type="number" value="${c.converge_T}"></label>
      <label>標本 m_max / s_min / m_step<div class="inline"><input class="tiny" name="sample.m_max" type="number" value="${c.sample.m_max}"><input class="tiny" name="sample.s_min" type="number" value="${c.sample.s_min}"><input class="tiny" name="sample.m_step" type="number" value="${c.sample.m_step}"></div></label>
      <label>予算: 反復 / LLM 回 / DB 回 / DB 回毎反復<div class="inline"><input class="tiny" name="budget.iterations" type="number" value="${c.budget.iterations}"><input class="tiny" name="budget.llm_calls" type="number" value="${c.budget.llm_calls}"><input class="tiny" name="budget.db_runs" type="number" value="${c.budget.db_runs}"><input class="tiny" name="budget.db_runs_per_iteration" type="number" value="${c.budget.db_runs_per_iteration}"></div></label>
      <label>RSJ 提示件数 語 / コード<div class="inline"><input class="tiny" name="rsj.top_terms" type="number" value="${c.rsj.top_terms}"><input class="tiny" name="rsj.top_codes" type="number" value="${c.rsj.top_codes}"></div></label>
    </div></fieldset>
    <div class="actions"><button type="submit" class="btn primary">保存</button><span class="dim">設定は pqb.config.json（git 管理外）に保存されます。</span></div>
  </form>
  <fieldset><legend>知識層・オフライン母集団</legend>
    <div class="actions">
      <label class="btn small" style="cursor:pointer">📚 分類表辞書 CSV を取り込む（scheme,code,title,parent,level）<input type="file" id="dict-file" accept=".csv" hidden></label>
      <label class="btn small" style="cursor:pointer">🗂 文献 CSV を local_index に読み込む<input type="file" id="index-file" accept=".csv,.tsv" hidden></label>
      <label class="check"><input type="checkbox" id="index-replace">既存を置き換える</label>
    </div>
    <p class="dim" style="font-size:.76rem">local_index は「特許情報標準データ等から作る自前索引」の簡易版で、手元の文献集合を DSL で機械照合します（テスト・デモ・API 契約前の検証用）。</p>
  </fieldset>`;
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target); const body = {};
    const setPath = (path, val) => { const ks = path.split("."); let o = body; ks.slice(0, -1).forEach((k) => { o[k] = o[k] || {}; o = o[k]; }); o[ks.at(-1)] = val; };
    $$("input, select", e.target).forEach((el) => { if (!el.name) return; if (el.type === "checkbox") setPath(el.name, el.checked); else if (el.type === "number") { if (el.value !== "") setPath(el.name, Number(el.value)); } else if (el.name === "llm.api_key") { if (el.value) setPath(el.name, el.value); } else setPath(el.name, el.value); });
    try { await api("/api/config", body); toast("保存しました", "ok"); closeModals(); await loadConfig(); if (state.caseId) await openCase(state.caseId); } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-test-llm").addEventListener("click", async () => { $("#test-result").textContent = "…"; try { const r = await api("/api/config/test", {}); $("#test-result").textContent = (r.ok ? "OK: " : "NG: ") + r.message; } catch (e) { $("#test-result").textContent = e.message; } });
  $("#dict-file").addEventListener("change", async (e) => { const f = e.target.files[0]; if (!f) return; const text = await f.text(); try { const r = await api("/api/dictionary", { csv_text: text }); toast(`分類表辞書 ${r.imported} 件を取り込みました`, "ok"); await loadConfig(); } catch (err) { toast(err.message, "error"); } });
  $("#index-file").addEventListener("change", async (e) => { const f = e.target.files[0]; if (!f) return; const b64 = await readFileB64(f); try { const r = await api("/api/local_index", { csv_base64: b64, replace: $("#index-replace").checked }); toast(`local_index: ${r.local_index_size} 件（取り込み ${r.imported}、${r.info.encoding}）`, "ok"); await loadConfig(); if (state.caseId) await openCase(state.caseId); } catch (err) { toast(err.message, "error"); } });
}

/* ---------------- 起動 ---------------- */
(async () => {
  try {
    await loadConfig(); await loadCases();
    const h = parseHash();
    if (h.caseId && state.cases.some((c) => c.case_id === h.caseId)) await openCase(h.caseId, h.tab || "overview");
  } catch (e) { toast("サーバに接続できません: " + e.message, "error"); }
})();
