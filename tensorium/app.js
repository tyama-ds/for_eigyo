/* Tensorium — フロントエンド（依存なし）。6 ステップのワークフロー + 履歴 + 環境設定。 */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = Charts.fmt;
  const fmtInt = (n) => (n === null || n === undefined) ? "–" : Number(n).toLocaleString("ja-JP");
  const fmtPct = (v, d = 1) => (v === null || v === undefined) ? "–" : (100 * v).toFixed(d) + "%";
  const fmtDur = (s) => { if (s === null || s === undefined) return "–"; s = Math.round(s); if (s < 60) return s + "秒"; const m = Math.floor(s / 60); if (m < 60) return `${m}分${s % 60}秒`; return `${Math.floor(m / 60)}時間${m % 60}分`; };
  const fmtDate = (iso) => iso ? iso.replace("T", " ").slice(0, 16) : "";
  const store = {
    get(k, d) { try { const v = localStorage.getItem("tensorium." + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem("tensorium." + k, JSON.stringify(v)); } catch (_) { /* ignore */ } },
  };
  const FAM_COLOR = { hf: "var(--violet)", sbert: "var(--cyan)", scratch: "var(--amber)", tabular: "var(--green)", baseline: "var(--gray)" };
  const FAM_SERIES = { hf: 6, sbert: 0, scratch: 3, tabular: 2, baseline: null };   // dataviz パレットの slot（0 起点）
  const FAM_ICON = { hf: "🧬", sbert: "🧭", scratch: "⚡", tabular: "▦", baseline: "▁" };
  const ROLE_LABEL = { target: "目的変数", text: "テキスト", numeric: "数値", categorical: "カテゴリ", ignore: "使わない" };
  const TYPE_LABEL = { numeric: "数値", categorical: "カテゴリ", text: "テキスト", datetime: "日時", id: "ID", empty: "空" };
  const LANG_LABEL = { ja: "日本語", multi: "多言語", en: "英語" };

  // ---------------------------------------------------------------- API
  async function api(method, url, body, headers) {
    const opt = { method, headers: Object.assign({}, headers || {}) };
    if (body !== undefined) {
      if (body instanceof Blob) opt.body = body;
      else { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    }
    const res = await fetch(url, opt);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
    return data;
  }
  const GET = (u) => api("GET", u);
  const POST = (u, b) => api("POST", u, b === undefined ? {} : b);
  const upload = (u, file, headers) => api("POST", u, file, Object.assign({ "X-Filename": encodeURIComponent(file.name) }, headers || {}));

  // ---------------------------------------------------------------- 状態
  const S = {
    catalog: null, env: null, settings: null, samples: [], dataset: null, spec: null, validate: null,
    family: store.get("family", "sbert"), model: store.get("model", ""), hparams: store.get("hparams", {}), lang: store.get("lang", "all"),
    job: null, jobTimer: null, logNext: 0, runs: [], evalRun: null, evalSplit: "val", predRun: null, predResult: null, step: "data",
    augStatus: null,
    aug: { profile: null, plan: null, n: 200, mode: "balance", cap: true, custom: {}, current: null, job: null, jobTimer: null, logNext: 0, planTimer: null },
  };

  // ---------------------------------------------------------------- toast / theme
  function toast(msg, kind = "", ms = 4500) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = `<span>${kind === "err" ? "⚠ " : kind === "ok" ? "✓ " : ""}${esc(msg)}</span><span class="x">✕</span>`;
    el.querySelector(".x").onclick = () => el.remove();
    $("#toasts").appendChild(el);
    if (ms) setTimeout(() => el.remove(), ms);
  }
  function setTheme(t) {
    document.documentElement.dataset.theme = t; store.set("theme", t);
    $("#theme-toggle").textContent = t === "dark" ? "☾" : "☀";
    requestAnimationFrame(() => Charts.redrawAll());
  }

  // ---------------------------------------------------------------- ナビゲーション
  function locked(step) {
    if ((step === "task" || step === "model") && !S.dataset) return "先にデータを読み込んでください";
    if (step === "model" && !(S.spec && S.spec.target)) return "先に目的変数を選んでください";
    if (step === "augment" && !(S.dataset && S.spec && S.spec.target)) return "先にデータを読み込み、目的変数を選んでください";
    if ((step === "eval" || step === "predict") && !S.runs.length && !(S.job && S.job.status === "done")) return "学習済みモデルがまだありません";
    return null;
  }
  function go(step) {
    const why = locked(step);
    if (why) { toast(why, "err", 2500); return; }
    S.step = step;
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + step));
    $$(".step").forEach((b) => b.classList.toggle("active", b.dataset.step === step));
    $$(".side-link").forEach((b) => b.classList.toggle("active", b.dataset.step === step));
    if (step === "task") renderTask();
    if (step === "model") renderModel();
    if (step === "train") renderTrain();
    if (step === "eval") renderEvalPanel();
    if (step === "predict") renderPredictPanel();
    if (step === "runs") loadRuns().then(renderRuns);
    if (step === "augment") renderAugmentPanel();
    if (step === "env") loadEnv();
    window.scrollTo({ top: 0, behavior: "smooth" });
    requestAnimationFrame(() => Charts.redrawAll());
  }
  function updateStepper() {
    const done = {
      data: !!S.dataset, task: !!(S.spec && S.spec.target && S.validate), model: !!(S.spec && S.spec.target && S.family),
      train: !!(S.job && S.job.status === "done") || S.runs.length > 0, eval: !!S.evalRun, predict: !!S.predResult,
    };
    $$(".step").forEach((b) => {
      const st = b.dataset.step;
      b.classList.toggle("done", !!done[st] && st !== S.step);
      b.classList.toggle("locked", !!locked(st));
    });
    $("#ss-data").textContent = S.dataset ? `${S.dataset.name} · ${fmtInt(S.dataset.n_rows)} 行` : "CSV / XLSX を読み込む";
    $("#ss-task").textContent = S.spec && S.spec.target ? `${S.spec.target} · ${S.spec.task === "regression" ? "回帰" : "分類"}` : "目的変数・分割";
    const fam = S.catalog && S.catalog.families.find((f) => f.id === S.family);
    $("#ss-model").textContent = fam ? fam.name + (S.model && (S.family === "hf" || S.family === "sbert") ? " · " + shortModel(S.model) : "") : "ファミリーとパラメータ";
    $("#ss-train").textContent = S.job ? ({ running: "学習中…", done: "完了", failed: "失敗", cancelled: "中止", queued: "待機中" }[S.job.status] || S.job.status) : "進捗と損失曲線";
    $("#ss-eval").textContent = S.runs.length ? `${S.runs.length} 件の実行` : "指標・誤差・混同行列";
  }
  function shortModel(id) {
    if (!id) return "";
    const p = S.catalog && S.catalog.presets.find((x) => x.id === id);
    return p ? p.name : id.replace(/\\/g, "/").replace(/\/$/, "").split("/").pop();
  }
  function updatePills() {
    const pd = $("#pill-data");
    pd.querySelector(".dot").className = "dot " + (S.dataset ? "ok" : "");
    pd.querySelector("span:last-child").textContent = S.dataset ? `${S.dataset.name} · ${fmtInt(S.dataset.n_rows)} 行` : "データ未読込";
    const pj = $("#pill-job");
    const aj = S.aug.job;
    if (aj && (aj.status === "running" || aj.status === "queued")) {
      pj.querySelector(".dot").className = "dot run";
      pj.querySelector("span:last-child").textContent = `合成データ生成中 ${Math.round((aj.progress || {}).pct || 0)}%`;
      pj.onclick = () => go("augment");
    } else pj.onclick = () => go("train");
    const j = S.job;
    const jd = !j ? "" : j.status === "running" || j.status === "queued" ? "run" : j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "";
    if (!(aj && (aj.status === "running" || aj.status === "queued"))) {
    pj.querySelector(".dot").className = "dot " + jd;
    pj.querySelector("span:last-child").textContent = !j ? "学習なし" : j.status === "running" ? `学習中 ${Math.round(j.progress.pct || 0)}%` : ({ done: "学習完了", failed: "学習失敗", cancelled: "学習中止", queued: "待機中" }[j.status] || j.status);
    }
    const badge = $("#aug-badge");
    const as = S.augStatus;
    const augOk = augMatches(as);
    badge.classList.toggle("hidden", !augOk);
    if (augOk) badge.textContent = `+${fmtInt(as.n_rows)}`;
    const pe = $("#pill-env");
    if (S.env) {
      const d = S.env.device;
      pe.querySelector(".dot").className = "dot " + (d.torch ? (d.cuda || d.mps ? "ok" : "warn") : "bad");
      pe.querySelector("span:last-child").textContent = !d.torch ? "PyTorch 未検出" : d.cuda ? `GPU: ${d.gpu_name}` : d.mps ? "MPS (Apple)" : `PyTorch ${d.torch_version} · CPU ${d.cpu_count} コア`;
    }
  }

  // ---------------------------------------------------------------- 1. データ
  function renderSamples() {
    $("#samples").innerHTML = S.samples.map((s) => `<button class="sample" data-file="${esc(s.file)}">
      <div class="n">${esc(s.name)} <span class="tag ${s.task === "regression" ? "reg" : "cls"}">${s.task === "regression" ? "回帰" : "分類"}</span></div>
      <div class="d">${esc(s.desc)}</div><div class="dim small mono">${esc(s.file)}</div></button>`).join("");
    $$("#samples .sample").forEach((b) => b.onclick = () => loadSample(b.dataset.file));
  }
  async function loadSample(file) {
    try { const r = await POST("/api/dataset/sample", { name: file }); setDataset(r.dataset); toast(`サンプル「${file}」を読み込みました`, "ok", 2500); }
    catch (e) { toast(e.message, "err"); }
  }
  async function uploadDataset(file) {
    if (!file) return;
    const t = document.createElement("div"); t.className = "toast"; t.innerHTML = `<span class="spinner"></span><span>${esc(file.name)} を読み込み中…</span>`; $("#toasts").appendChild(t);
    try { const r = await upload("/api/dataset/upload", file); setDataset(r.dataset); toast(`${file.name} を読み込みました（${fmtInt(r.dataset.n_rows)} 行）`, "ok", 2500); }
    catch (e) { toast(e.message, "err", 8000); }
    finally { t.remove(); }
  }
  function suggestTask(col) {
    if (!col) return "classification";
    if (col.type === "numeric") { const n = Math.max(col.n - col.missing, 1); return (col.unique > 20 || col.unique / n > 0.2) ? "regression" : "classification"; }
    return "classification";
  }
  function setDataset(ds) {
    S.dataset = ds; S.validate = null; S.augStatus = null; S.aug.current = null; S.aug.profile = null; S.aug.plan = null;
    const saved = store.get("spec:" + ds.name);
    if (saved && saved.roles && ds.columns.every((c) => c in saved.roles) && ds.columns.includes(saved.target)) S.spec = saved;
    else S.spec = { target: ds.suggest.target, task: ds.suggest.task, roles: Object.assign({}, ds.suggest.roles), split: { val: 0.15, test: 0.15, seed: 42, stratify: true } };
    renderDataset(); updatePills(); updateStepper();
  }
  function saveSpec() { if (S.dataset && S.spec) store.set("spec:" + S.dataset.name, S.spec); updateStepper(); }
  function colInfo(name) { return S.dataset.analysis.find((c) => c.name === name); }
  function renderDataset() {
    const ds = S.dataset;
    $("#data-empty").classList.add("hidden"); $("#data-loaded").classList.remove("hidden");
    $("#ds-name").textContent = ds.name;
    $("#ds-reader").textContent = ds.encoding ? `CSV · ${ds.encoding}` : `XLSX · ${ds.reader === "openpyxl" ? "openpyxl" : "内蔵リーダー"}`;
    const sel = $("#ds-sheet");
    if (ds.sheets && ds.sheets.length > 1) { sel.classList.remove("hidden"); sel.innerHTML = ds.sheets.map((s) => `<option ${s === ds.sheet ? "selected" : ""}>${esc(s)}</option>`).join(""); }
    else sel.classList.add("hidden");
    const t = colInfo(S.spec.target);
    $("#ds-tiles").innerHTML = [
      ["行数", fmtInt(ds.n_rows), ""], ["列数", fmtInt(ds.n_cols), ""], ["欠損セル", ds.missing_pct + "%", ""],
      ["目的変数", esc(S.spec.target || "未選択"), t ? TYPE_LABEL[t.type] + ` · ${t.unique} 種` : ""],
      ["タスク", S.spec.task === "regression" ? "回帰" : "分類", "自動判定（変更可）"],
    ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
    renderCols(); renderPreview();
  }
  function renderCols() {
    const ds = S.dataset, roles = S.spec.roles;
    $("#cols").innerHTML = ds.analysis.map((c) => {
      const role = roles[c.name] || "ignore";
      const st = c.stats || {};
      let meta = `${c.unique} 種 · 欠損 ${c.missing}`;
      if (c.type === "numeric") meta = `${fmt(st.min)} 〜 ${fmt(st.max)} · 平均 ${fmt(st.mean)}`;
      if (c.type === "text") meta = `平均 ${Math.round(st.avg_len)} 文字 · 最大 ${st.max_len}`;
      if (c.type === "datetime") meta = `${esc(st.min)} 〜 ${esc(st.max)}`;
      const usable = ["numeric", "categorical", "text"].includes(c.type) || c.unique >= 2;
      const opts = ["target", "text", "numeric", "categorical", "ignore"].map((r) => `<option value="${r}" ${r === role ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("");
      return `<div class="col ${role === "target" ? "is-target" : ""} ${role === "ignore" ? "is-ignore" : ""}" data-col="${esc(c.name)}">
        <div class="col-h"><span class="col-name" title="${esc(c.name)}">${esc(c.name)}</span><span class="tag ${c.type}">${TYPE_LABEL[c.type] || c.type}</span></div>
        <div class="col-meta"><span>${meta}</span></div>
        <div class="col-viz"><canvas></canvas></div>
        <select ${usable ? "" : "disabled"}>${opts}</select></div>`;
    }).join("");
    $$("#cols .col").forEach((el) => {
      const c = colInfo(el.dataset.col); const cv = el.querySelector("canvas"); const st = c.stats || {};
      requestAnimationFrame(() => {
        if (c.type === "numeric" && st.hist) Charts.spark(cv, st.hist.counts, "var(--cyan)".startsWith("var") ? Charts.theme().series[0] : null);
        else if ((c.type === "categorical" || c.type === "id") && st.top) Charts.spark(cv, st.top.map((x) => x.count), Charts.theme().series[2]);
        else if (c.type === "text" && st.len_hist) Charts.spark(cv, st.len_hist.counts, Charts.theme().series[6]);
        else if (c.type === "datetime" && st.top) Charts.spark(cv, st.top.map((x) => x.count), Charts.theme().series[3]);
      });
      el.querySelector("select").onchange = (e) => setRole(c.name, e.target.value);
    });
  }
  function setRole(name, role) {
    const roles = S.spec.roles;
    if (role === "target") {
      const prev = S.spec.target;
      if (prev && prev !== name) { const pc = colInfo(prev); roles[prev] = pc && ["numeric", "categorical", "text"].includes(pc.type) ? pc.type : "ignore"; }
      S.spec.target = name; roles[name] = "target"; S.spec.task = suggestTask(colInfo(name));
    } else {
      if (S.spec.target === name) S.spec.target = null;
      roles[name] = role;
    }
    S.validate = null; saveSpec(); renderDataset(); updatePills();
  }
  function renderPreview() {
    const ds = S.dataset, roles = S.spec.roles;
    const types = Object.fromEntries(ds.analysis.map((c) => [c.name, c.type]));
    const head = ds.columns.map((c) => `<th class="${roles[c] === "target" ? "target" : ""} ${types[c] === "numeric" ? "num" : ""}">${esc(c)}<br><span class="tag ${roles[c] === "target" ? "target" : roles[c] || "ignore"}">${ROLE_LABEL[roles[c]] || "使わない"}</span></th>`).join("");
    const body = ds.preview.map((r) => `<tr>${r.map((v, i) => `<td class="${roles[ds.columns[i]] === "target" ? "target" : ""} ${types[ds.columns[i]] === "numeric" ? "num" : ""}" title="${esc(v)}">${esc(v)}</td>`).join("")}</tr>`).join("");
    $("#prev-tbl").innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
    $("#prev-n").textContent = `先頭 ${ds.preview.length} 行 / 全 ${fmtInt(ds.n_rows)} 行`;
  }

  // ---------------------------------------------------------------- 2. タスク
  let validateTimer = null;
  function renderTask() {
    const ds = S.dataset, sp = S.spec;
    $("#t-target").innerHTML = ds.analysis.map((c) => `<option value="${esc(c.name)}" ${c.name === sp.target ? "selected" : ""} ${c.type === "empty" ? "disabled" : ""}>${esc(c.name)}（${TYPE_LABEL[c.type] || c.type} · ${c.unique} 種）</option>`).join("");
    $$("#t-task button").forEach((b) => b.classList.toggle("active", b.dataset.v === sp.task));
    const t = colInfo(sp.target);
    $("#t-suggest").textContent = t ? `自動判定: ${suggestTask(t) === "regression" ? "回帰" : "分類"}（${TYPE_LABEL[t.type]} · ${t.unique} 種）` : "";
    const groups = ["text", "numeric", "categorical", "ignore"];
    $("#t-roles").innerHTML = groups.map((g) => {
      const cols = ds.columns.filter((c) => sp.roles[c] === g);
      return `<div class="row"><span class="tag ${g}" style="min-width:64px;text-align:center">${ROLE_LABEL[g]}</span><span class="chips">${cols.length ? cols.map((c) => `<span class="chip">${esc(c)}</span>`).join("") : '<span class="dim small">なし</span>'}</span></div>`;
    }).join("") + `<div class="hint">ロールは「1. データ」画面の各列カードで変更できます。</div>`;
    $("#t-val").value = Math.round(sp.split.val * 100); $("#t-test").value = Math.round(sp.split.test * 100);
    $("#t-val-v").textContent = Math.round(sp.split.val * 100) + "%"; $("#t-test-v").textContent = Math.round(sp.split.test * 100) + "%";
    $("#t-seed").value = sp.split.seed; $("#t-strat").checked = !!sp.split.stratify;
    $("#t-strat-wrap").style.visibility = sp.task === "classification" ? "visible" : "hidden";
    scheduleValidate(0);
  }
  function scheduleValidate(ms = 350) {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(async () => {
      try {
        const r = await POST("/api/spec/validate", { spec: S.spec });
        S.validate = r; S.spec = Object.assign(S.spec, { target: r.spec.target, task: r.spec.task, roles: r.spec.roles, split: r.spec.split });
        renderValidate(r); updateStepper();
      } catch (e) { S.validate = null; $("#t-warn").innerHTML = `<div class="err-box">${esc(e.message)}</div>`; $("#t-valid").textContent = ""; }
    }, ms);
  }
  function renderValidate(r) {
    const sc = r.split_counts;
    $("#t-split").innerHTML = `学習 <b>${fmtInt(sc.train)}</b> 行 / 検証 <b>${fmtInt(sc.val)}</b> 行 / テスト <b>${fmtInt(sc.test)}</b> 行（有効 ${fmtInt(r.n_valid)} 行）`;
    $("#t-valid").textContent = `有効 ${fmtInt(r.n_valid)} 行` + (r.n_dropped ? ` · 目的変数が欠損/無効の ${fmtInt(r.n_dropped)} 行は除外` : "");
    $("#t-warn").innerHTML = r.warning ? `<div class="warn-box">⚠ ${esc(r.warning)}</div>` : "";
    renderImbalanceHint(r);
    const cv = $("#t-chart");
    if (r.spec.task === "regression") Charts.hist(cv, { edges: r.target_hist.edges, counts: r.target_hist.counts, xLabel: r.spec.target, yLabel: "件数" });
    else Charts.bars(cv, { labels: r.class_counts.map((c) => c.value), values: r.class_counts.map((c) => c.count), unit: " 件", fmt: fmtInt, horizontal: r.class_counts.length > 8 });
  }

  // ---------------------------------------------------------------- 3. モデル
  function famAvailable(f) {
    if (!S.env) return { ok: true, why: "" };
    if (!S.env.families_available[f.id]) return { ok: false, why: "要インストール" };
    if (f.needs_text && !(S.spec && S.spec.text_cols ? S.spec.text_cols.length : Object.values(S.spec.roles).includes("text"))) return { ok: false, why: "テキスト列が必要" };
    if (f.id === "tabular" && !Object.values(S.spec.roles).some((r) => r === "numeric" || r === "categorical")) return { ok: false, why: "数値/カテゴリ列が必要" };
    return { ok: true, why: "" };
  }
  function renderModel() {
    const cat = S.catalog;
    $("#fams").innerHTML = cat.families.map((f) => {
      const av = famAvailable(f);
      return `<button class="fam ${f.id === S.family ? "on" : ""} ${av.ok ? "" : "off"}" data-fam="${f.id}" style="--fam-c:${FAM_COLOR[f.id]}">
        <span class="badge ${av.ok ? "ok" : f.id === "hf" || f.id === "sbert" ? "warn" : "ng"}">${av.ok ? (f.downloads ? "要ダウンロード" : "オフライン可") : av.why}</span>
        <div class="fam-ico">${FAM_ICON[f.id]}</div><div class="n">${esc(f.name)}</div><div class="s">${esc(f.short)}</div>
        <div class="meta"><span class="chip">速度: ${esc(f.speed)}</span><span class="chip">精度: ${esc(f.accuracy)}</span></div></button>`;
    }).join("");
    $$("#fams .fam").forEach((b) => b.onclick = () => { S.family = b.dataset.fam; store.set("family", S.family); renderModel(); updateStepper(); });
    const f = cat.families.find((x) => x.id === S.family) || cat.families[0];
    $("#fam-desc").style.setProperty("--fam-c", FAM_COLOR[f.id]);
    $("#fam-desc").innerHTML = `<b>${esc(f.name)}</b> — ${esc(f.desc)}` + (f.requires.length ? `<div class="dim small" style="margin-top:4px">必要: ${f.requires.join(", ")}${f.optional ? `（任意: ${f.optional.join(", ")}）` : ""}</div>` : "");
    const usesPreset = f.id === "hf" || f.id === "sbert";
    $("#preset-card").classList.toggle("hidden", !usesPreset);
    if (usesPreset) renderPresets();
    renderHparams();
    const av = famAvailable(f);
    const t = S.spec;
    const n = S.validate ? S.validate.split_counts : null;
    const parts = [`データ <b>${esc(S.dataset.name)}</b>`, `目的変数 <b>${esc(t.target)}</b>（${t.task === "regression" ? "回帰" : "分類"}）`,
      `テキスト ${Object.values(t.roles).filter((r) => r === "text").length} 列 / 数値 ${Object.values(t.roles).filter((r) => r === "numeric").length} 列 / カテゴリ ${Object.values(t.roles).filter((r) => r === "categorical").length} 列`];
    if (n) parts.push(`学習 ${fmtInt(n.train)} / 検証 ${fmtInt(n.val)} / テスト ${fmtInt(n.test)} 行`);
    let check = `<div class="ok-box">✓ ${parts.join(" · ")}</div>`;
    if (!av.ok) check += `<div class="err-box" style="margin-top:8px">✕ このファミリーは使えません: ${esc(av.why)}${av.why === "要インストール" ? '（「環境・設定」にインストールコマンドがあります）' : ""}</div>`;
    if (usesPreset && !S.model) check += `<div class="warn-box" style="margin-top:8px">⚠ 事前学習モデルを選ぶか、ID / パスを入力してください</div>`;
    if (usesPreset && S.model) { const p = cat.presets.find((x) => x.id === S.model); if (p && p.extra_pip) check += `<div class="warn-box" style="margin-top:8px">⚠ このモデルには追加ライブラリが必要: <code>pip install ${p.extra_pip.join(" ")}</code></div>`; }
    if (usesPreset && S.env && S.settings && S.settings.hf_offline) check += `<div class="warn-box" style="margin-top:8px">ℹ オフライン設定中: キャッシュ済み / ローカルフォルダのモデルのみ使えます</div>`;
    $("#m-check").innerHTML = check;
    renderSynthToggle();
    $("#m-train").disabled = !av.ok || (usesPreset && !S.model);
  }
  function renderPresets() {
    const langs = ["all", "ja", "multi", "en"];
    $("#lang-chips").innerHTML = langs.map((l) => `<span class="chip click ${S.lang === l ? "on" : ""}" data-lang="${l}">${l === "all" ? "すべて" : LANG_LABEL[l]}</span>`).join("");
    $$("#lang-chips .chip").forEach((c) => c.onclick = () => { S.lang = c.dataset.lang; store.set("lang", S.lang); renderPresets(); });
    const list = S.catalog.presets.filter((p) => p.families.includes(S.family) && (S.lang === "all" || p.lang === S.lang));
    $("#presets").innerHTML = list.map((p) => `<button class="preset ${p.id === S.model ? "on" : ""}" data-id="${esc(p.id)}">
      <div class="n">${p.recommended ? '<span class="star" title="おすすめ">★</span>' : ""}${esc(p.name)} <span class="lang">${LANG_LABEL[p.lang]}</span><span class="dim small">${esc(p.params)}</span></div>
      <div class="id">${esc(p.id)}</div><div class="note">${esc(p.note)}</div>${p.extra_pip ? `<div class="extra">＋ pip install ${esc(p.extra_pip.join(" "))}</div>` : ""}</button>`).join("");
    $$("#presets .preset").forEach((b) => b.onclick = () => { S.model = b.dataset.id; store.set("model", S.model); $("#m-model").value = S.model; renderModel(); });
    $("#m-model").value = S.model || "";
    const p = S.catalog.presets.find((x) => x.id === S.model);
    $("#m-model-note").textContent = p ? `${p.name} · ${p.params} · ${p.note}` : S.model ? "カタログ外のモデル ID / パス（AutoModel で読み込みます）" : "";
  }
  function hpValues() {
    const fam = S.family, task = S.spec.task;
    const schema = (S.catalog.hparams[fam] || []).filter((f) => !f.task || f.task === task);
    const saved = (S.hparams[fam] || {});
    const out = {};
    schema.forEach((f) => { out[f.key] = f.key in saved ? saved[f.key] : f.default; });
    return { schema, values: out };
  }
  function renderHparams() {
    const { schema, values } = hpValues();
    const render = (f) => {
      const v = values[f.key];
      const help = f.help ? `<span class="help">${esc(f.help)}</span>` : "";
      if (f.type === "bool") return `<div class="hp-bool"><label class="check"><input type="checkbox" data-hp="${f.key}" ${v ? "checked" : ""}> ${esc(f.label)}</label>${f.help ? `<span class="dim small">${esc(f.help)}</span>` : ""}</div>`;
      if (f.type === "select") return `<div><label class="field">${esc(f.label)}${help}</label><select data-hp="${f.key}">${f.options.map((o) => `<option value="${esc(o.value)}" ${o.value === v ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></div>`;
      if (f.type === "text") return `<div><label class="field">${esc(f.label)}${help}</label><input type="text" data-hp="${f.key}" value="${esc(v)}"></div>`;
      return `<div><label class="field">${esc(f.label)}${help}</label><input type="number" data-hp="${f.key}" value="${esc(v)}" ${f.min !== undefined ? `min="${f.min}"` : ""} ${f.max !== undefined ? `max="${f.max}"` : ""} step="${f.type === "float" ? "any" : 1}"></div>`;
    };
    $("#hp-basic").innerHTML = schema.filter((f) => f.group === "basic").map(render).join("") || '<div class="dim small">このファミリーにパラメータはありません</div>';
    $("#hp-adv").innerHTML = schema.filter((f) => f.group !== "basic").map(render).join("");
    $("#hp-adv-wrap").classList.toggle("hidden", !schema.some((f) => f.group !== "basic"));
    $$("[data-hp]").forEach((el) => el.oninput = () => {
      const f = schema.find((x) => x.key === el.dataset.hp);
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (f.type === "int") v = parseInt(v, 10); if (f.type === "float") v = parseFloat(v);
      S.hparams[S.family] = Object.assign({}, S.hparams[S.family] || {}, { [f.key]: v }); store.set("hparams", S.hparams); renderEstimate();
    });
    renderEstimate();
  }
  function renderEstimate() {
    const { values } = hpValues();
    const n = S.validate ? S.validate.split_counts.train : null;
    if (!n || !values.epochs) { $("#hp-est").textContent = ""; return; }
    const steps = Math.ceil(n / Math.max(1, values.batch_size || 1));
    $("#hp-est").innerHTML = `学習 ${fmtInt(n)} 行 → 1 エポック ${fmtInt(steps)} ステップ × ${values.epochs} エポック = <b>${fmtInt(steps * values.epochs)}</b> ステップ` +
      (S.family === "hf" ? "（BERT base を CPU で回す場合、1 ステップ数秒が目安。GPU 推奨）" : "");
  }
  async function startTraining() {
    const { values } = hpValues();
    const as = S.augStatus;
    const useSyn = !!(as && as.enabled && augMatches(as));
    const body = { spec: S.spec, family: S.family, model: (S.family === "hf" || S.family === "sbert") ? S.model : null, hparams: values, name: $("#m-name").value, use_synthetic: useSyn };
    const btn = $("#m-train"); btn.disabled = true;
    try {
      const r = await POST("/api/train", body);
      S.job = r.job; S.logNext = 0; $("#j-log").textContent = "";
      go("train"); pollJob(); updatePills();
    } catch (e) { toast(e.message, "err", 8000); }
    finally { btn.disabled = false; }
  }

  // ---------------------------------------------------------------- 4. 学習
  function renderTrain() {
    const has = !!S.job;
    $("#train-empty").classList.toggle("hidden", has); $("#train-live").classList.toggle("hidden", !has);
    if (has) renderJob(S.job);
  }
  async function pollJob() {
    clearTimeout(S.jobTimer);
    if (!S.job) return;
    try {
      const r = await GET(`/api/jobs/${S.job.id}?log_from=${S.logNext}`);
      if (r.log && r.log.length) appendLog(r.log);
      S.logNext = r.log_next; S.job = r;
      renderJob(r); updatePills();
      if (r.status === "running" || r.status === "queued") S.jobTimer = setTimeout(pollJob, 800);
      else onJobFinished(r);
    } catch (e) { S.jobTimer = setTimeout(pollJob, 2000); }
  }
  function appendLog(lines) {
    const el = $("#j-log");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.insertAdjacentHTML("beforeend", lines.map((l) => `<div class="${/エラー|Error|Traceback/.test(l) ? "err" : ""}">${esc(l)}</div>`).join(""));
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
  const STATUS_JA = { running: "学習中", queued: "待機中", done: "完了", failed: "失敗", cancelled: "中止" };
  function renderJob(j) {
    if (S.step !== "train") return;
    const p = j.params || {};
    const st = $("#j-status"); st.className = "status " + j.status; st.innerHTML = (j.status === "running" ? '<span class="spinner"></span>' : "") + (STATUS_JA[j.status] || j.status);
    $("#j-title").textContent = p.name || `${p.family_name || p.family}${p.model ? " · " + shortModel(p.model) : ""}`;
    $("#j-chips").innerHTML = `<span class="chip">${esc(p.target)}</span><span class="chip">${p.task === "regression" ? "回帰" : "分類"}</span>`;
    const pr = j.progress || {};
    const eta = pr.eta_sec !== undefined && pr.eta_sec !== null && j.status === "running" ? ` · 残り約 ${fmtDur(pr.eta_sec)}` : "";
    $("#j-time").textContent = `経過 ${fmtDur(j.elapsed)}${eta}`;
    $("#j-prog .fill").style.width = Math.max(0, Math.min(100, pr.pct || 0)) + "%";
    $("#j-prog").classList.toggle("done", j.status !== "running");
    $("#j-phase").textContent = (pr.phase || "") + (pr.epoch ? `　epoch ${pr.epoch}/${pr.epochs} · step ${pr.step}/${pr.steps}` : "") + (pr.loss !== undefined ? ` · loss ${fmt(pr.loss)}` : "") + (pr.lr ? ` · lr ${pr.lr.toExponential(1)}` : "");
    $("#j-pct").textContent = (pr.pct || 0).toFixed(0) + "%";
    $("#j-cancel").classList.toggle("hidden", j.status !== "running" && j.status !== "queued");
    $("#j-eval").classList.toggle("hidden", j.status !== "done");
    $("#j-again").classList.toggle("hidden", j.status === "running" || j.status === "queued");
    $("#j-error").innerHTML = j.error ? `<div class="err-box" style="margin-top:10px">${esc(j.error)}</div>` : "";
    renderJobCharts(j);
  }
  function renderJobCharts(j) {
    const th = Charts.theme();
    const steps = (j.progress && j.progress.steps) || 1;
    const curves = j.curves || [];
    const series = [];
    if (j.step_losses && j.step_losses.length > 1) series.push({ name: "ステップ損失", points: j.step_losses, color: th.series[0], alpha: 0.35, width: 1, noHover: true, dots: false });
    series.push({ name: "学習損失", points: curves.map((c) => [c.epoch * steps, c.train_loss]), color: th.series[0], dots: true });
    if (curves.some((c) => c.val_loss !== undefined && c.val_loss !== null)) series.push({ name: "検証損失", points: curves.filter((c) => c.val_loss !== null && c.val_loss !== undefined).map((c) => [c.epoch * steps, c.val_loss]), color: th.series[1], dots: true });
    Charts.line($("#j-loss"), { series: series.filter((s) => s.points.length), xLabel: "ステップ", yLabel: "損失", empty: "学習が始まるとここに損失が表示されます" });
    $("#j-loss-legend").innerHTML = series.map((s) => `<span><i style="background:${s.color};opacity:${s.alpha || 1}"></i>${s.name}</span>`).join("");
    const isReg = (j.params || {}).task === "regression";
    const keys = isReg ? [["rmse", "RMSE", 2], ["mae", "MAE", 3]] : [["accuracy", "Accuracy", 2], ["f1_macro", "F1 (macro)", 3]];
    const ms = keys.map(([k, name, ci]) => ({ name, points: curves.filter((c) => c[k] !== undefined).map((c) => [c.epoch, c[k]]), color: th.series[ci], dots: true })).filter((s) => s.points.length);
    $("#j-metric-title").textContent = isReg ? "検証指標（誤差・小さいほど良い）" : "検証指標（大きいほど良い）";
    Charts.line($("#j-metric"), { series: ms, xLabel: "エポック", yLabel: isReg ? "誤差" : "スコア", yMin: isReg ? 0 : 0, yMax: isReg ? undefined : 1, xTicks: curves.map((c) => c.epoch).filter((e, i, a) => a.length <= 12 || i % Math.ceil(a.length / 12) === 0), empty: "エポック終了ごとに検証指標を表示します" });
    $("#j-metric-legend").innerHTML = ms.map((s) => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("");
  }
  async function onJobFinished(j) {
    await loadRuns();
    if (j.status === "done" && j.result) {
      S.evalRun = j.result.run_id; S.predRun = j.result.run_id;
      const pm = j.result.primary_metric || {};
      toast(`学習完了: ${pm.name || ""} ${pm.value !== undefined && pm.value !== null ? fmt(pm.value) : ""}（検証）`, "ok", 6000);
    } else if (j.status === "failed") toast("学習に失敗しました: " + (j.error || ""), "err", 10000);
    else if (j.status === "cancelled") toast("学習を中止しました", "", 3000);
    updateStepper(); updatePills();
  }

  // ---------------------------------------------------------------- runs
  async function loadRuns() {
    try { const r = await GET("/api/runs"); S.runs = r.runs.filter((m) => m.status === "done"); } catch (_) { S.runs = []; }
    updateStepper();
    return S.runs;
  }
  function runLabel(m) { return `${m.name}　[${m.family_name || m.family}${m.model ? " · " + shortModel(m.model) : ""}] ${fmtDate(m.created)}`; }
  function primaryOf(m, split) { const mm = m.metrics && m.metrics[split]; if (!mm) return null; return m.task === "regression" ? mm.rmse : mm.f1_macro; }
  function bestIds(runs, split = "val") {
    const groups = {};
    runs.forEach((m) => { const k = m.target + "|" + m.task + "|" + (m.dataset && m.dataset.name); const v = primaryOf(m, split); if (v === null || v === undefined) return; if (!groups[k] || (m.task === "regression" ? v < groups[k].v : v > groups[k].v)) groups[k] = { v, id: m.id }; });
    return new Set(Object.values(groups).map((g) => g.id));
  }

  // ---------------------------------------------------------------- 5. 評価
  async function renderEvalPanel() {
    await loadRuns();
    const sel = $("#e-run");
    if (!S.runs.length) { sel.innerHTML = ""; $("#e-head").innerHTML = ""; $("#e-body").innerHTML = `<div class="card empty"><div class="ico">📊</div>学習済みモデルがまだありません</div>`; return; }
    if (!S.evalRun || !S.runs.some((m) => m.id === S.evalRun)) S.evalRun = S.runs[0].id;
    sel.innerHTML = S.runs.map((m) => `<option value="${m.id}" ${m.id === S.evalRun ? "selected" : ""}>${esc(runLabel(m))}</option>`).join("");
    try { const meta = await GET(`/api/runs/${S.evalRun}`); renderEval(meta); } catch (e) { toast(e.message, "err"); }
    updateStepper();
  }
  function metricTile(k, v, t, best) { return `<div class="metric ${best ? "best" : ""}"><div class="k">${k}</div><div class="v">${v}</div><div class="t">${t}</div></div>`; }
  function renderEval(meta) {
    const isReg = meta.task === "regression";
    const other = S.evalSplit === "val" ? "test" : "val";
    $$("#e-split button").forEach((b) => { b.classList.toggle("active", b.dataset.v === S.evalSplit); b.disabled = !(meta.eval && meta.eval[b.dataset.v]); });
    $("#e-head").innerHTML = [
      `<span class="chip" style="border-color:${FAM_COLOR[meta.family]}">${FAM_ICON[meta.family] || ""} ${esc(meta.family_name || meta.family)}</span>`,
      meta.model ? `<span class="chip mono" title="${esc(meta.model)}">${esc(shortModel(meta.model))}</span>` : "",
      `<span class="chip">${isReg ? "回帰" : "分類"} · ${esc(meta.target)}</span>`,
      `<span class="chip">学習 ${fmtInt(meta.n_train)} / 検証 ${fmtInt(meta.n_val)} / テスト ${fmtInt(meta.n_test)}</span>`,
      meta.n_synthetic ? `<span class="chip" style="border-color:var(--green)">⚗ 合成 ${fmtInt(meta.n_synthetic)} 行を学習に使用</span>` : "",
      `<span class="chip">${fmtDur(meta.duration_sec)} · ${esc(meta.device || "")}</span>`,
      meta.n_params ? `<span class="chip">${fmtInt(meta.n_params)} params</span>` : "",
      meta.best_epoch ? `<span class="chip">best epoch ${meta.best_epoch}/${meta.epochs_run}</span>` : "",
      `<span class="chip">${esc(meta.dataset && meta.dataset.name)}</span>`,
    ].join("");
    const ev = meta.eval && meta.eval[S.evalSplit];
    if (!ev) {
      const alt = meta.eval && meta.eval[other];
      $("#e-body").innerHTML = `<div class="card empty">この分割（${S.evalSplit === "val" ? "検証" : "テスト"}）は 0 行のため評価がありません。${alt ? `<br><button class="btn sm" id="e-alt">${other === "val" ? "検証" : "テスト"}データを見る</button>` : ""}</div>`;
      if (alt) $("#e-alt").onclick = () => { S.evalSplit = other; renderEval(meta); };
      return;
    }
    const m = ev.metrics;
    const splitName = S.evalSplit === "val" ? "検証データ" : "テストデータ";
    let html = "";
    if (isReg) {
      html += `<div class="card"><div class="card-head"><h2>指標 <span class="muted small">${splitName} · ${fmtInt(m.n)} 行</span></h2></div><div class="metric-row">
        ${metricTile("RMSE", fmt(m.rmse), "二乗平均平方根誤差", true)}${metricTile("MAE", fmt(m.mae), "平均絶対誤差")}${metricTile("R²", m.r2.toFixed(3), "決定係数（1 が最良）")}
        ${metricTile("MAPE", m.mape !== null && m.mape !== undefined ? m.mape.toFixed(1) + "%" : "–", "平均絶対パーセント誤差")}${metricTile("Pearson r", m.pearson.toFixed(3), "実測と予測の相関")}${metricTile("中央絶対誤差", fmt(m.median_ae), "誤差の中央値")}
        ${metricTile("目的変数の std", fmt(m.y_std), "比較用（RMSE がこれ未満なら平均より良い）")}</div></div>`;
      html += `<div class="grid-2"><div class="card"><div class="chart-title">実測 vs 予測 <span class="dim">点線 = 完全一致</span></div><div class="chart tall"><canvas id="e-scatter"></canvas></div></div>
        <div class="card"><div class="chart-title">残差（予測 − 実測）の分布</div><div class="chart tall"><canvas id="e-resid"></canvas></div></div></div>`;
    } else {
      html += `<div class="card"><div class="card-head"><h2>指標 <span class="muted small">${splitName} · ${fmtInt(m.n)} 行 · ${m.classes.length} クラス</span></h2></div><div class="metric-row">
        ${metricTile("Accuracy", fmtPct(m.accuracy), "正解率", true)}${metricTile("F1 (macro)", m.f1_macro.toFixed(3), "クラス平均 F1")}${metricTile("F1 (weighted)", m.f1_weighted.toFixed(3), "件数で重み付けした F1")}
        ${metricTile("Precision", m.precision_macro.toFixed(3), "macro 平均")}${metricTile("Recall", m.recall_macro.toFixed(3), "macro 平均")}
        ${m.log_loss !== undefined ? metricTile("Log loss", m.log_loss.toFixed(3), "確信度の較正（小さいほど良い）") : ""}${m.auc !== undefined ? metricTile("ROC-AUC", m.auc.toFixed(3), `陽性 = ${esc(m.positive_class)}`) : ""}</div></div>`;
      html += `<div class="card"><div class="cm-wrap"><div><div class="chart-title">混同行列（行 = 実際、列 = 予測）</div><div class="chart tall"><canvas id="e-cm"></canvas></div></div>
        <div><div class="chart-title">クラス別指標</div><div class="tbl-wrap" style="max-height:340px"><table class="tbl compact"><thead><tr><th>クラス</th><th class="num">件数</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead><tbody>
        ${m.per_class.map((c) => `<tr><td>${esc(c.class)}</td><td class="num">${c.support}</td><td><span class="pc-bar" style="width:${Math.round(60 * c.precision)}px"></span> ${c.precision.toFixed(2)}</td><td><span class="pc-bar" style="width:${Math.round(60 * c.recall)}px;background:var(--s3)"></span> ${c.recall.toFixed(2)}</td><td><span class="pc-bar" style="width:${Math.round(60 * c.f1)}px;background:var(--s7)"></span> ${c.f1.toFixed(2)}</td></tr>`).join("")}
        </tbody></table></div></div></div></div>`;
    }
    if (meta.curves && meta.curves.length) html += `<div class="grid-2"><div class="card"><div class="chart-title">損失の推移</div><div class="chart short"><canvas id="e-curve"></canvas></div></div>
      <div class="card"><div class="chart-title">検証指標の推移</div><div class="chart short"><canvas id="e-curve2"></canvas></div></div></div>`;
    const ex = ev.examples || [];
    if (ex.length) {
      html += `<div class="card"><div class="card-head"><h2>${isReg ? "誤差が大きい例" : "誤分類の例（確信度の高い順）"} <span class="muted small">上位 ${ex.length} 件 · 行番号は元ファイルの行（ヘッダー = 1）</span></h2></div>
        <div class="tbl-wrap" style="max-height:380px"><table class="tbl compact"><thead><tr><th class="num">行</th><th>入力（抜粋）</th>${isReg ? '<th class="num">実測</th><th class="num">予測</th><th class="num">誤差</th>' : '<th>実際</th><th>予測</th><th class="num">確信度</th>'}</tr></thead><tbody>
        ${ex.map((e) => `<tr><td class="num">${e.row}</td><td class="wrap">${esc(e.text)}</td>${isReg ? `<td class="num">${fmt(e.y_true)}</td><td class="num">${fmt(e.y_pred)}</td><td class="num" style="color:${e.error > 0 ? "var(--st-serious)" : "var(--s1)"}">${e.error > 0 ? "+" : ""}${fmt(e.error)}</td>` : `<td><span class="tag target">${esc(e.true)}</span></td><td><span class="tag numeric">${esc(e.pred)}</span></td><td class="num">${fmtPct(e.conf)}</td>`}</tr>`).join("")}
        </tbody></table></div></div>`;
    }
    $("#e-body").innerHTML = html;
    const th = Charts.theme();
    requestAnimationFrame(() => {
      if (isReg) {
        Charts.scatter($("#e-scatter"), { points: ev.plot.points, xLabel: "実測", yLabel: "予測", color: th.series[0] });
        Charts.hist($("#e-resid"), { edges: ev.plot.residual_hist.edges, counts: ev.plot.residual_hist.counts, xLabel: "残差", yLabel: "件数", color: th.series[1], zeroLine: true });
      } else {
        Charts.heatmap($("#e-cm"), { matrix: ev.plot.confusion, labels: ev.plot.classes });
      }
      if (meta.curves && meta.curves.length) {
        const cv = meta.curves;
        Charts.line($("#e-curve"), { series: [{ name: "学習損失", points: cv.map((c) => [c.epoch, c.train_loss]), color: th.series[0], dots: true }, { name: "検証損失", points: cv.filter((c) => c.val_loss !== null && c.val_loss !== undefined).map((c) => [c.epoch, c.val_loss]), color: th.series[1], dots: true }].filter((s) => s.points.length), xLabel: "エポック", yLabel: "損失" });
        const keys = isReg ? [["rmse", "RMSE", 2], ["mae", "MAE", 3]] : [["accuracy", "Accuracy", 2], ["f1_macro", "F1 (macro)", 3]];
        Charts.line($("#e-curve2"), { series: keys.map(([k, name, ci]) => ({ name, points: cv.filter((c) => c[k] !== undefined).map((c) => [c.epoch, c[k]]), color: th.series[ci], dots: true })).filter((s) => s.points.length), xLabel: "エポック", yLabel: isReg ? "誤差" : "スコア", yMin: 0, yMax: isReg ? undefined : 1 });
      }
    });
  }

  // ---------------------------------------------------------------- 6. 予測
  let predMeta = null;
  async function renderPredictPanel() {
    await loadRuns();
    const sel = $("#p-run");
    if (!S.runs.length) { sel.innerHTML = ""; $("#p-req").textContent = "学習済みモデルがありません"; return; }
    if (!S.predRun || !S.runs.some((m) => m.id === S.predRun)) S.predRun = S.evalRun && S.runs.some((m) => m.id === S.evalRun) ? S.evalRun : S.runs[0].id;
    sel.innerHTML = S.runs.map((m) => `<option value="${m.id}" ${m.id === S.predRun ? "selected" : ""}>${esc(runLabel(m))}</option>`).join("");
    predMeta = S.runs.find((m) => m.id === S.predRun);
    const sp = predMeta.spec;
    const req = [].concat(sp.text_cols, sp.num_cols, sp.cat_cols);
    $("#p-req").innerHTML = `必要な列: ${req.map((c) => `<code>${esc(c)}</code>`).join(" ")} → 予測: <b>${esc(sp.target)}</b>（${predMeta.task === "regression" ? "回帰" : "分類"}${predMeta.classes ? " · " + predMeta.classes.length + " クラス" : ""}）`;
    $("#p-form").innerHTML = [
      ...sp.text_cols.map((c) => `<label class="field">${esc(c)} <span class="help">テキスト</span></label><textarea data-col="${esc(c)}" rows="3"></textarea>`),
      ...sp.num_cols.map((c) => `<label class="field">${esc(c)} <span class="help">数値</span></label><input type="text" inputmode="decimal" data-col="${esc(c)}">`),
      ...sp.cat_cols.map((c) => `<label class="field">${esc(c)} <span class="help">カテゴリ</span></label><input type="text" data-col="${esc(c)}">`),
    ].join("");
    $("#p-one-res").innerHTML = "";
    if (S.predResult && S.predResult.run_id === S.predRun) renderPredResult(S.predResult); else $("#p-res-card").classList.add("hidden");
  }
  async function predictFile(file) {
    if (!file || !S.predRun) return;
    const t = document.createElement("div"); t.className = "toast"; t.innerHTML = `<span class="spinner"></span><span>${esc(file.name)} を予測中…（モデル読み込みに時間がかかることがあります）</span>`; $("#toasts").appendChild(t);
    try { const r = await upload("/api/predict/upload", file, { "X-Run-Id": S.predRun }); r.filename = file.name; S.predResult = r; renderPredResult(r); toast(`${fmtInt(r.predictions.length)} 行を予測しました`, "ok", 3000); updateStepper(); }
    catch (e) { toast(e.message, "err", 9000); }
    finally { t.remove(); }
  }
  function renderPredResult(r) {
    const card = $("#p-res-card"); card.classList.remove("hidden");
    const isReg = r.task === "regression";
    const predCol = `予測_${r.target}`;
    const byRow = new Map(r.predictions.map((p) => [p.row, p]));
    const head = r.columns.map((c) => `<th class="${c === r.target ? "target" : ""}">${esc(c)}</th>`).join("") + `<th class="pred">${esc(predCol)}</th>` + (isReg ? "" : `<th class="pred num">確信度</th>`);
    const rows = r.rows.slice(0, 200).map((row, i) => { const p = byRow.get(i); return `<tr>${row.map((v, j) => `<td class="${r.columns[j] === r.target ? "target" : ""}" title="${esc(v)}">${esc(v)}</td>`).join("")}<td class="pred">${p ? (isReg ? fmt(p.pred) : esc(p.pred)) : "–"}</td>${isReg ? "" : `<td class="pred num">${p ? fmtPct(p.prob) : ""}</td>`}</tr>`; }).join("");
    $("#p-tbl").innerHTML = `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody>`;
    $("#p-res-n").textContent = `${fmtInt(r.predictions.length)} 行${r.rows.length > 200 ? "（先頭 200 行を表示）" : ""}`;
    let metric = "";
    if (r.actual) {
      const pairs = r.predictions.map((p, k) => [r.actual[k], p]).filter(([a]) => a !== null && a !== "");
      if (isReg) {
        const nums = pairs.map(([a, p]) => [parseFloat(String(a).replace(/[,¥$]/g, "")), p.pred]).filter(([a]) => isFinite(a));
        if (nums.length) { const mse = nums.reduce((s, [a, p]) => s + (p - a) ** 2, 0) / nums.length; const mae = nums.reduce((s, [a, p]) => s + Math.abs(p - a), 0) / nums.length; metric = `実測との比較（${nums.length} 行）: RMSE ${fmt(Math.sqrt(mse))} · MAE ${fmt(mae)}`; }
      } else if (pairs.length) { const acc = pairs.filter(([a, p]) => String(a) === String(p.pred)).length / pairs.length; metric = `実測との比較（${pairs.length} 行）: 正解率 ${fmtPct(acc)}`; }
    }
    $("#p-res-metric").textContent = metric;
  }
  function downloadCsv() {
    const r = S.predResult; if (!r) return;
    const isReg = r.task === "regression";
    const byRow = new Map(r.predictions.map((p) => [p.row, p]));
    const q = (v) => { const s = String(v ?? ""); return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    const head = [...r.columns, `予測_${r.target}`, ...(isReg ? [] : ["確信度", ...(r.classes || []).map((c) => `確率_${c}`)])];
    const lines = [head.map(q).join(",")];
    r.rows.forEach((row, i) => { const p = byRow.get(i); const extra = !p ? [""] : isReg ? [p.pred] : [p.pred, p.prob !== null ? p.prob.toFixed(4) : "", ...(p.probs || []).map((x) => x.toFixed(4))]; lines.push([...row, ...extra].map(q).join(",")); });
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `predictions_${(r.filename || "manual").replace(/\.[^.]+$/, "")}_${r.run_id}.csv`; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  async function predictOne() {
    if (!S.predRun) return;
    const row = {}; $$("#p-form [data-col]").forEach((el) => row[el.dataset.col] = el.value);
    const btn = $("#p-one"); btn.disabled = true;
    try {
      const r = await POST("/api/predict", { run_id: S.predRun, rows: [row], columns: Object.keys(row) });
      const p = r.predictions[0];
      if (r.task === "regression") $("#p-one-res").innerHTML = `<div class="sep"></div><div class="dim small">${esc(r.target)} の予測値</div><div class="result-hero">${fmt(p.pred)}</div>`;
      else {
        const order = r.classes.map((c, i) => [c, p.probs[i]]).sort((a, b) => b[1] - a[1]);
        $("#p-one-res").innerHTML = `<div class="sep"></div><div class="dim small">${esc(r.target)} の予測</div><div class="result-hero">${esc(p.pred)}</div><div class="dim small" style="margin-bottom:8px">確信度 ${fmtPct(p.prob)}</div>
          <div class="probs">${order.slice(0, 8).map(([c, v]) => `<div class="prob"><span title="${esc(c)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c)}</span><span class="track"><i style="width:${Math.round(100 * v)}%"></i></span><span class="num dim">${fmtPct(v)}</span></div>`).join("")}</div>`;
      }
    } catch (e) { toast(e.message, "err", 8000); }
    finally { btn.disabled = false; }
  }

  // ---------------------------------------------------------------- 履歴
  function renderRuns() {
    const runs = S.runs;
    const targets = Array.from(new Set(runs.map((m) => `${m.target}｜${m.task === "regression" ? "回帰" : "分類"}`)));
    const fsel = $("#r-filter"); const cur = fsel.value;
    fsel.innerHTML = `<option value="">すべての目的変数</option>` + targets.map((t) => `<option ${t === cur ? "selected" : ""}>${esc(t)}</option>`).join("");
    const filt = fsel.value ? runs.filter((m) => `${m.target}｜${m.task === "regression" ? "回帰" : "分類"}` === fsel.value) : runs;
    const best = bestIds(filt);
    if (!filt.length) { $("#r-tbl").innerHTML = `<tbody><tr><td class="dim" style="padding:20px">実行がありません</td></tr></tbody>`; Charts.bars($("#r-chart"), { labels: [], values: [] }); return; }
    $("#r-tbl").innerHTML = `<thead><tr><th>名前</th><th>作成</th><th>ファミリー</th><th>モデル</th><th>タスク / 目的変数</th><th class="num">検証</th><th class="num">テスト</th><th class="num">時間</th><th></th></tr></thead><tbody>
      ${filt.map((m) => { const pk = m.task === "regression" ? "RMSE" : "F1"; const v = primaryOf(m, "val"), t = primaryOf(m, "test"); return `<tr class="${best.has(m.id) ? "best-row" : ""}" data-id="${m.id}">
        <td><input class="name-in" value="${esc(m.name)}" data-rename="${m.id}" title="クリックして名前を編集"></td><td class="dim">${fmtDate(m.created)}</td>
        <td><span class="chip" style="border-color:${FAM_COLOR[m.family]}">${FAM_ICON[m.family] || ""} ${esc(m.family_name || m.family)}</span></td>
        <td class="mono small" title="${esc(m.model || "")}">${esc(shortModel(m.model))}</td><td>${m.task === "regression" ? "回帰" : "分類"} · ${esc(m.target)}${m.n_synthetic ? ` <span class="dim small">⚗+${fmtInt(m.n_synthetic)}</span>` : ""}</td>
        <td class="num">${best.has(m.id) ? "🏆 " : ""}${pk} <b>${v === null || v === undefined ? "–" : fmt(v)}</b></td><td class="num">${t === null || t === undefined ? "–" : fmt(t)}</td><td class="num dim">${fmtDur(m.duration_sec)}</td>
        <td><span class="row" style="gap:4px"><button class="btn xs" data-act="eval">評価</button><button class="btn xs" data-act="predict">予測</button><button class="btn xs danger" data-act="del">削除</button></span></td></tr>`; }).join("")}</tbody>`;
    $$("#r-tbl [data-act]").forEach((b) => b.onclick = async () => {
      const id = b.closest("tr").dataset.id;
      if (b.dataset.act === "eval") { S.evalRun = id; go("eval"); }
      if (b.dataset.act === "predict") { S.predRun = id; go("predict"); }
      if (b.dataset.act === "del") { if (!confirm("この実行（モデルと結果）を削除しますか？")) return; try { await POST(`/api/runs/${id}/delete`); toast("削除しました", "ok", 2000); await loadRuns(); renderRuns(); } catch (e) { toast(e.message, "err"); } }
    });
    $$("#r-tbl [data-rename]").forEach((inp) => inp.onchange = async () => { try { await POST(`/api/runs/${inp.dataset.rename}/rename`, { name: inp.value }); await loadRuns(); } catch (e) { toast(e.message, "err"); } });
    const th = Charts.theme();
    const sameTask = filt.every((m) => m.task === filt[0].task);
    const shown = sameTask ? filt.slice().reverse() : [];
    $("#r-chart-title").textContent = sameTask ? `代表指標の比較（検証データ · ${filt[0].task === "regression" ? "RMSE · 小さいほど良い" : "F1 macro · 大きいほど良い"}）` : "代表指標の比較 — 目的変数を選ぶとタスクが揃い比較できます";
    $("#r-chart-wrap").style.height = Math.max(160, 26 * shown.length + 40) + "px";
    Charts.bars($("#r-chart"), { labels: shown.map((m) => m.name), values: shown.map((m) => primaryOf(m, "val") || 0), horizontal: true, colors: shown.map((m) => (FAM_SERIES[m.family] === null ? th.muted : th.series[FAM_SERIES[m.family] ?? 0])) });
  }

  // ---------------------------------------------------------------- 環境
  async function loadEnv() {
    try {
      const [env, st] = await Promise.all([GET("/api/env"), GET("/api/settings")]);
      S.env = env; S.settings = st; renderEnv(); updatePills();
    } catch (e) { toast(e.message, "err"); }
  }
  function renderEnv() {
    const e = S.env, d = e.device;
    $("#env-tiles").innerHTML = [
      ["PyTorch", d.torch ? d.torch_version : "未検出", d.torch ? "" : "学習にはインストールが必要"],
      ["デバイス", d.selected.toUpperCase(), d.cuda ? esc(d.gpu_name) : d.mps ? "Apple Silicon GPU" : "GPU なし（CPU で学習）"],
      ["CPU", `${d.cpu_count} <small>コア</small>`, d.threads ? `torch スレッド ${d.threads}` : ""],
      ["Python", e.python, esc(e.platform.split("-").slice(0, 2).join(" "))],
      ["Hub 接続", e.hf_offline_env ? "オフライン" : "オンライン", e.hf_endpoint_env ? `ミラー: ${esc(e.hf_endpoint_env)}` : e.proxy_env ? `プロキシ: ${esc(e.proxy_env)}` : "直接接続（環境変数なし）"],
    ].map(([k, v, dd]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${dd}</div></div>`).join("");
    $("#env-libs").innerHTML = `<thead><tr><th>パッケージ</th><th>状態</th><th>用途</th></tr></thead><tbody>${e.libs.map((l) => `<tr><td class="mono">${esc(l.dist)}</td><td>${l.installed ? `<span class="badge ok">✓ ${esc(l.version || "")}</span>` : '<span class="badge ng">未導入</span>'}</td><td class="dim small wrap">${esc(l.desc)}</td></tr>`).join("")}</tbody>`;
    $("#env-install").innerHTML = e.install_hints.map((h) => `<div><div class="small muted">${esc(h.label)}</div><div class="row" style="gap:6px"><code style="flex:1;overflow:auto;white-space:nowrap">${esc(h.cmd)}</code><button class="btn xs" data-copy="${esc(h.cmd)}">コピー</button></div></div>`).join("");
    $$("#env-install [data-copy]").forEach((b) => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "✓"; setTimeout(() => (b.textContent = "コピー"), 1200); } catch (_) { prompt("コピーしてください", b.dataset.copy); } });
    const st = S.settings;
    ["device", "num_threads", "hf_endpoint", "hf_cache_dir", "proxy_url"].forEach((k) => { $("#s-" + k).value = st[k] ?? ""; });
    $("#s-hf_token").value = ""; $("#s-hf_token").placeholder = st.hf_token_set ? "（設定済み・変更する場合のみ入力）" : "（未設定）";
    ["hf_offline", "use_proxy", "trust_remote_code"].forEach((k) => { $("#s-" + k).checked = !!st[k]; });
  }
  async function saveSettings() {
    const body = {};
    ["device", "num_threads", "hf_endpoint", "hf_cache_dir", "proxy_url", "hf_token"].forEach((k) => body[k] = $("#s-" + k).value);
    ["hf_offline", "use_proxy", "trust_remote_code"].forEach((k) => body[k] = $("#s-" + k).checked);
    try { S.settings = await POST("/api/settings", body); $("#s-msg").textContent = "保存しました（次の学習・予測から反映）"; toast("設定を保存しました", "ok", 2000); loadEnv(); }
    catch (e) { toast(e.message, "err"); }
  }

  // ---------------------------------------------------------------- データ拡張（LLM 知識蒸留）
  const AUG_REAL = () => "var(--s1)";
  const AUG_SYN = () => "var(--s3)";
  function specKey(spec) {
    const feats = Object.entries(spec.roles).filter(([, r]) => ["text", "numeric", "categorical"].includes(r)).map(([c, r]) => `${c}:${r}`).sort();
    return [spec.target, spec.task, ...feats].join("|");
  }
  function augMatches(as) { return !!(as && as.n_rows > 0 && S.spec && S.spec.target && as.spec_key === specKey(S.spec)); }
  function fmtRatio(r) { return r === null || r === undefined ? "∞" : (Math.round(r * 100) / 100).toFixed(2); }
  function fmtBal(e) { return e === null || e === undefined ? "–" : (100 * e).toFixed(0) + "%"; }
  function renderImbalanceHint(r) {
    const box = $("#t-imbalance");
    if (!box) return;
    if (r.spec.task !== "classification" || !r.class_counts || r.class_counts.length < 2) { box.innerHTML = ""; return; }
    const counts = r.class_counts.map((c) => c.count);
    const mx = Math.max(...counts), mn = Math.min(...counts), ratio = mn ? mx / mn : Infinity;
    const n = counts.reduce((a, b) => a + b, 0), k = counts.length;
    const ent = -counts.filter((c) => c > 0).reduce((a, c) => a + (c / n) * Math.log(c / n), 0) / Math.log(k);
    if (ratio < 1.5) { box.innerHTML = `<div class="ok-box">✓ クラスの偏りは小さめです（最大/最小比 ${fmtRatio(ratio)} · 均衡度 ${fmtBal(ent)}）</div>`; return; }
    box.innerHTML = `<div class="warn-box row between"><span>⚠ クラスが偏っています: 最大/最小比 <b>${fmtRatio(ratio)}</b> · 均衡度 <b>${fmtBal(ent)}</b>（最少「${esc(r.class_counts[r.class_counts.length - 1].value)}」${fmtInt(mn)} 行）。少数クラスは LLM 知識蒸留の合成データで均衡化できます。</span><button class="btn sm" data-go="augment">⚗ データ拡張へ</button></div>`;
    box.querySelector("[data-go]").onclick = () => go("augment");
  }
  function renderSynthToggle() {
    const box = $("#m-synthetic");
    if (!box) return;
    const as = S.augStatus;
    const ok = augMatches(as);
    if (!ok) { box.innerHTML = as && as.n_rows > 0 && S.spec && S.spec.target ? `<div class="warn-box">⚠ 合成データ ${fmtInt(as.n_rows)} 行がありますが、目的変数 / タスク / 列のロールが生成時と異なるため学習には使われません。<button class="btn xs" data-go="augment">再生成</button></div>` : ""; const b = box.querySelector("[data-go]"); if (b) b.onclick = () => go("augment"); return; }
    box.innerHTML = `<div class="${as.enabled ? "ok-box" : "warn-box"} row between"><label class="check"><input type="checkbox" id="m-use-syn" ${as.enabled ? "checked" : ""}> 合成データ <b>${fmtInt(as.n_rows)}</b> 行を学習データに含める（検証 / テストには含めない）</label><button class="btn sm ghost" data-go="augment">拡張タブで確認</button></div>`;
    box.querySelector("#m-use-syn").onchange = async (e) => { try { const r = await POST("/api/augment/enable", { enabled: e.target.checked }); S.augStatus.enabled = r.enabled; renderSynthToggle(); } catch (err) { toast(err.message, "err"); } };
    box.querySelector("[data-go]").onclick = () => go("augment");
  }
  async function refreshAugStatus() {
    try { const st = await GET("/api/status"); S.augStatus = st.augment; } catch (_) { /* ignore */ }
    updatePills();
  }
  async function renderAugmentPanel() {
    const ok = S.dataset && S.spec && S.spec.target;
    $("#aug-nodata").classList.toggle("hidden", !!ok); $("#aug-body").classList.toggle("hidden", !ok);
    if (!ok) return;
    try {
      const prof = await POST("/api/augment/profile", { spec: S.spec });
      S.aug.profile = prof;
    } catch (e) { toast(e.message, "err"); return; }
    const prof = S.aug.profile;
    $("#aug-target").textContent = `${prof.target} · ${prof.task === "regression" ? "回帰（分位ビン）" : "分類"} · 有効 ${fmtInt(prof.n_valid)} 行`;
    const st = prof.stats;
    const minG = prof.groups.reduce((a, g) => (g.count < a.count ? g : a), prof.groups[0]);
    $("#aug-tiles-before").innerHTML = [
      ["グループ数", fmtInt(st.k), prof.task === "regression" ? "目的変数の分位ビン" : "クラス"],
      ["最少グループ", fmtInt(st.min), esc(minG.label)], ["最大 / 最小比", fmtRatio(st.ratio), "1.00 が完全均衡"],
      ["均衡度", fmtBal(st.entropy), "正規化エントロピー（100% が均等）"],
    ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
    const chartH = Math.max(200, prof.groups.length > 8 ? 24 * prof.groups.length + 40 : 200) + "px";
    $("#aug-chart-before").parentElement.style.height = chartH; $("#aug-chart-after").parentElement.style.height = chartH;
    Charts.bars($("#aug-chart-before"), { labels: prof.groups.map((g) => g.label), values: prof.groups.map((g) => g.count), fmt: fmtInt, unit: " 行" });
    $("#aug-verify").disabled = prof.task !== "classification";
    if (prof.task !== "classification") $("#aug-verify").checked = false;
    // 件数チップ
    const presets = prof.presets || [100, 200, 500, 1000, 2000];
    $("#aug-counts").innerHTML = presets.map((n) => `<span class="chip click ${S.aug.n === n ? "on" : ""}" data-n="${n}">${fmtInt(n)}</span>`).join("");
    $$("#aug-counts .chip").forEach((c) => c.onclick = () => { S.aug.n = +c.dataset.n; $("#aug-n").value = S.aug.n; renderAugCounts(); planAugment(0); });
    $("#aug-n").value = S.aug.n;
    $$("#aug-mode button").forEach((b) => b.classList.toggle("active", b.dataset.v === S.aug.mode));
    $("#aug-cap").checked = S.aug.cap;
    $("#aug-custom").classList.toggle("hidden", S.aug.mode !== "custom");
    if (S.aug.mode === "custom") renderAugCustom();
    // 生徒モデル候補
    await loadRuns();
    const cands = S.runs.filter((m) => m.target === prof.target && m.task === prof.task && m.family !== "baseline");
    $("#aug-student").innerHTML = `<option value="">使わない</option>` + cands.map((m) => `<option value="${m.id}">${esc(runLabel(m))}</option>`).join("");
    fillLlmSettings();
    planAugment(0);
    if (prof.current) { S.aug.current = prof.current; renderAugResult(prof.current); } else $("#aug-result").classList.add("hidden");
    if (S.aug.job) renderAugJob(S.aug.job);
    updatePills();
  }
  function renderAugCounts() { $$("#aug-counts .chip").forEach((c) => c.classList.toggle("on", +c.dataset.n === S.aug.n)); }
  function renderAugCustom() {
    const prof = S.aug.profile; if (!prof) return;
    $("#aug-custom-tbl").innerHTML = `<thead><tr><th>グループ</th><th class="num">実データ</th><th class="num">追加件数</th></tr></thead><tbody>${prof.groups.map((g) => `<tr><td>${esc(g.label)}</td><td class="num">${fmtInt(g.count)}</td><td class="num"><input class="num-in" type="number" min="0" max="20000" data-key="${esc(g.key)}" value="${S.aug.custom[g.key] || 0}"></td></tr>`).join("")}</tbody>`;
    $$("#aug-custom-tbl input").forEach((inp) => inp.oninput = () => { S.aug.custom[inp.dataset.key] = parseInt(inp.value || "0", 10); planAugment(); });
  }
  function planAugment(ms = 200) {
    clearTimeout(S.aug.planTimer);
    S.aug.planTimer = setTimeout(async () => {
      if (!S.aug.profile) return;
      const seq = (S.aug.planSeq = (S.aug.planSeq || 0) + 1);
      try {
        const r = await POST("/api/augment/plan", { spec: S.spec, n_total: S.aug.n, mode: S.aug.mode, cap: S.aug.cap, custom: S.aug.custom });
        if (seq !== S.aug.planSeq) return;                      // 古い応答は捨てる
        S.aug.plan = r; renderAugPlan(r);
      } catch (e) { if (seq === S.aug.planSeq) $("#aug-plan-hint").textContent = e.message; }
    }, ms);
  }
  function renderAugPlan(r) {
    const prof = S.aug.profile;
    const labels = r.groups.map((g) => g.label);
    const alloc = r.groups.map((g) => r.allocation[g.key] || 0);
    const b = r.stats_before, a = r.stats_after;
    const arrow = (x, y, fmt, betterLow) => { const same = fmt(x) === fmt(y); const good = betterLow ? (y < x) : (y > x); return `${fmt(x)} <span class="dim">→</span> <span class="${same ? "" : good ? "delta-up" : "delta-down"}">${fmt(y)}</span>`; };
    $("#aug-tiles-after").innerHTML = [
      ["合計件数", `${fmtInt(a.n)} <small>+${fmtInt(r.n_alloc)}</small>`, `実データ ${fmtInt(b.n)} 行 + 合成 ${fmtInt(r.n_alloc)} 行`],
      ["最少グループ", arrow(b.min, a.min, fmtInt, false), "少数グループの件数"],
      ["最大 / 最小比", arrow(b.ratio === null ? Infinity : b.ratio, a.ratio === null ? Infinity : a.ratio, fmtRatio, true), "1.00 が完全均衡"],
      ["均衡度", arrow(b.entropy, a.entropy, fmtBal, false), "正規化エントロピー"],
    ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
    $("#aug-legend").innerHTML = `<span><i class="box" style="background:${AUG_REAL()}"></i>実データ</span><span><i class="box" style="background:${AUG_SYN()}"></i>合成データ</span><span><i style="background:var(--chart-axis);height:1px"></i>最多グループ</span>`;
    Charts.stacked($("#aug-chart-after"), { labels, series: [{ name: "実データ", values: r.counts_before, slot: 0 }, { name: "合成データ", values: alloc, slot: 2 }], refLine: b.max });
    const maxAfter = Math.max(...r.counts_after) || 1;
    $("#aug-plan-tbl").innerHTML = `<thead><tr><th>グループ</th><th class="num">実データ</th><th class="num">追加</th><th class="num">合成後</th><th>構成比（合成後）</th></tr></thead><tbody>${r.groups.map((g, i) => `<tr><td>${esc(g.label)}${g.range ? ` <span class="dim small">(${esc(prof.target)})</span>` : ""}</td><td class="num">${fmtInt(r.counts_before[i])}</td><td class="num" style="color:${alloc[i] ? AUG_SYN() : "inherit"}">${alloc[i] ? "+" + fmtInt(alloc[i]) : "–"}</td><td class="num"><b>${fmtInt(r.counts_after[i])}</b></td><td><span class="bar" style="width:${Math.round(120 * r.counts_before[i] / maxAfter)}px;background:${AUG_REAL()}"></span><span class="bar" style="width:${Math.round(120 * alloc[i] / maxAfter)}px;background:${AUG_SYN()};margin-left:-6px"></span> ${(100 * r.counts_after[i] / a.n).toFixed(1)}%</td></tr>`).join("")}</tbody>`;
    const est = Math.ceil(r.n_alloc / Math.max(1, parseInt($("#aug-batch").value || "10", 10)));
    const capped = S.aug.mode === "balance" && S.aug.cap && r.n_alloc < S.aug.n;
    const isLlm = $("#llm-provider").value !== "builtin";
    const verifyN = isLlm && prof.task === "classification" && $("#aug-verify").checked ? Math.ceil(r.n_alloc / 10) : 0;
    $("#aug-plan-hint").innerHTML = r.n_alloc ? `合計 <b>${fmtInt(r.n_alloc)}</b> 行を生成${capped ? `（「最多グループを超えない」により要求 ${fmtInt(S.aug.n)} 行から制限。もっと増やすにはチェックを外すか「均等」を選択）` : ""}${isLlm ? `（LLM 呼び出し約 ${fmtInt(est)} 回${verifyN ? " + 検証 " + fmtInt(verifyN) + " 回" : ""}）` : "（内蔵生成・LLM 呼び出しなし）"}` : "追加件数が 0 です（既に均衡している場合は「均等」またはグループ別指定を選んでください）";
  }
  function fillLlmSettings() {
    const st = S.settings || {};
    $("#llm-provider").value = st.llm_provider || "openai";
    ["base_url", "model", "max_tokens", "timeout"].forEach((k) => { $("#llm-" + k).value = st["llm_" + k] ?? ""; });
    $("#llm-api_key").value = ""; $("#llm-api_key").placeholder = st.llm_api_key_set ? "（設定済み・変更する場合のみ入力）" : "（未設定。ローカル LLM なら不要）";
    $("#llm-key-clear").classList.toggle("hidden", !st.llm_api_key_set);
    $("#llm-fields").classList.toggle("hidden", $("#llm-provider").value === "builtin");
    $("#aug-conc").value = st.llm_concurrency || 2;
  }
  async function saveLlmSettings(silent = false) {
    const body = { llm_provider: $("#llm-provider").value, llm_base_url: $("#llm-base_url").value.trim(), llm_model: $("#llm-model").value.trim(),
      llm_api_key: $("#llm-api_key").value, llm_max_tokens: $("#llm-max_tokens").value || 4096, llm_timeout: $("#llm-timeout").value || 180,
      llm_concurrency: $("#aug-conc").value || 2 };
    S.settings = await POST("/api/settings", body);
    fillLlmSettings();
    if (!silent) { $("#llm-msg").textContent = "保存しました"; toast("LLM 設定を保存しました", "ok", 2000); }
  }
  async function testLlm() {
    $("#llm-msg").innerHTML = '<span class="spinner"></span> 接続中…';
    try { await saveLlmSettings(true); const r = await POST("/api/llm/test"); $("#llm-msg").innerHTML = r.ok ? `<span class="check-ok">✓ ${esc(r.message)}</span>` : `<span class="check-ng">✕ ${esc(r.message)}</span>`; }
    catch (e) { $("#llm-msg").innerHTML = `<span class="check-ng">✕ ${esc(e.message)}</span>`; }
  }
  function augParams() {
    return { n_total: S.aug.n, mode: S.aug.mode, cap: S.aug.cap, custom: S.aug.custom,
      batch_rows: parseInt($("#aug-batch").value || "10", 10), concurrency: parseInt($("#aug-conc").value || "2", 10),
      verify_llm: $("#aug-verify").checked, student_run: $("#aug-student").value || null, student_policy: $("#aug-student-policy .active").dataset.v,
      dedupe: $("#aug-dedupe").checked, instructions: $("#aug-instructions").value, temperature: $("#aug-temp").value === "" ? null : parseFloat($("#aug-temp").value),
      seed: parseInt($("#aug-seed").value || "42", 10) };
  }
  async function startAugment() {
    const btn = $("#aug-start"); btn.disabled = true;
    try {
      await saveLlmSettings(true);
      const r = await POST("/api/augment/start", { spec: S.spec, params: augParams() });
      S.aug.job = r.job; S.aug.logNext = 0; $("#ag-log").textContent = ""; $("#aug-result").classList.add("hidden");
      renderAugJob(r.job); pollAug(); updatePills();
    } catch (e) { toast(e.message, "err", 8000); btn.disabled = false; }
  }
  async function pollAug() {
    clearTimeout(S.aug.jobTimer);
    if (!S.aug.job) return;
    try {
      const r = await GET(`/api/jobs/${S.aug.job.id}?log_from=${S.aug.logNext}`);
      if (r.log && r.log.length) { const el = $("#ag-log"); const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30; el.insertAdjacentHTML("beforeend", r.log.map((l) => `<div class="${/エラー|Error/.test(l) ? "err" : ""}">${esc(l)}</div>`).join("")); if (atBottom) el.scrollTop = el.scrollHeight; }
      S.aug.logNext = r.log_next; S.aug.job = r;
      renderAugJob(r); updatePills();
      if (r.status === "running" || r.status === "queued") S.aug.jobTimer = setTimeout(pollAug, 800);
      else await onAugFinished(r);
    } catch (e) { S.aug.jobTimer = setTimeout(pollAug, 2000); }
  }
  function renderAugJob(j) {
    if (S.step !== "augment") return;
    $("#ag-live").classList.remove("hidden");
    const st = $("#ag-status"); st.classList.remove("hidden"); st.className = "status " + j.status; st.innerHTML = (j.status === "running" ? '<span class="spinner"></span>' : "") + (STATUS_JA[j.status] || j.status);
    const pr = j.progress || {};
    $("#ag-prog .fill").style.width = Math.max(0, Math.min(100, pr.pct || 0)) + "%"; $("#ag-prog").classList.toggle("done", j.status !== "running");
    $("#ag-phase").textContent = pr.phase || ""; $("#ag-pct").textContent = (pr.pct || 0).toFixed(0) + "%";
    $("#ag-time").textContent = `経過 ${fmtDur(j.elapsed)}`;
    $("#ag-cancel").classList.toggle("hidden", !(j.status === "running" || j.status === "queued"));
    $("#aug-start").disabled = j.status === "running" || j.status === "queued";
    $("#ag-error").innerHTML = j.error ? `<div class="err-box" style="margin-top:8px">${esc(j.error)}</div>` : "";
  }
  async function onAugFinished(j) {
    await refreshAugStatus();
    if (j.status === "done") {
      try { const r = await GET("/api/augment"); S.aug.current = r.augment; if (r.augment) renderAugResult(r.augment); } catch (e) { toast(e.message, "err"); }
      const a = j.result || {};
      toast(`合成データ ${fmtInt(a.n_rows)} 行を生成しました（均衡度 ${fmtBal((a.stats_before || {}).entropy)} → ${fmtBal((a.stats_after || {}).entropy)}）`, "ok", 7000);
    } else {
      if (j.status === "failed") toast("生成に失敗しました: " + (j.error || ""), "err", 10000);
      else if (j.status === "cancelled") toast("生成を中止しました", "", 3000);
      if (S.aug.current) renderAugResult(S.aug.current);
    }
    if (j.status === "done" && j.result && j.result.stored === false) toast("生成中にデータが差し替えられたため、合成データは保存されませんでした", "err", 8000);
    renderSynthToggle();
  }
  function renderAugResult(aug) {
    const card = $("#aug-result"); card.classList.remove("hidden");
    const b = aug.stats_before || {}, a = aug.stats_after || {};
    const rejected = Object.values(aug.rejected || {}).reduce((x, y) => x + y, 0);
    const ls = aug.llm_stats;
    $("#aug-res-n").textContent = `${fmtInt(aug.n_rows)} 行 · ${aug.provider === "builtin" ? "内蔵生成" : `${aug.provider} / ${aug.model || ""}`} · ${fmtDur(aug.duration_sec)}`;
    $("#aug-enabled").checked = !!aug.enabled;
    $("#aug-res-tiles").innerHTML = [
      ["採用", fmtInt(aug.n_rows), `却下 ${fmtInt(rejected)} 行`],
      ["均衡度", `${fmtBal(b.entropy)} <small>→</small> ${fmtBal(a.entropy)}`, "正規化エントロピー"],
      ["最大 / 最小比", `${fmtRatio(b.ratio)} <small>→</small> ${fmtRatio(a.ratio)}`, "1.00 が完全均衡"],
      ["最少グループ", `${fmtInt(b.min)} <small>→</small> ${fmtInt(a.min)}`, "行数"],
      ls ? ["LLM 呼び出し", fmtInt(ls.calls), `入力 ${fmtInt(ls.input_tokens)} / 出力 ${fmtInt(ls.output_tokens)} トークン`] : ["生成元", "内蔵", "実データの組み替え（LLM なし）"],
    ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
    const rej = Object.entries(aug.rejected || {});
    $("#aug-res-rejected").innerHTML = (aug.aborted ? `<div class="warn-box" style="margin-bottom:6px">⚠ ${esc(aug.aborted)}</div>` : "") + (rej.length ? "却下の内訳: " + rej.map(([k, v]) => `${esc(k)} ${fmtInt(v)}`).join(" · ") : "却下なし") +
      (aug.groups ? `　｜　グループ別追加: ${aug.groups.map((g) => `${esc(g.label)} +${fmtInt(g.added)}`).join(" · ")}` : "");
    const cols = aug.columns, spec = S.spec;
    const featureCols = cols.filter((c) => spec.roles[c] && spec.roles[c] !== "ignore" && spec.roles[c] !== "target");
    const mark = (v) => v === true ? '<span class="check-ok">✓</span>' : v === false ? '<span class="check-ng">✕</span>' : '<span class="check-na">–</span>';
    const hasTeacher = (aug.meta || []).some((m) => m.checks && m.checks.teacher_agree !== undefined);
    const hasStudent = (aug.meta || []).some((m) => m.checks && m.checks.student_agree !== undefined);
    $("#aug-res-tbl").innerHTML = `<thead><tr><th>#</th><th>グループ</th>${featureCols.map((c) => `<th>${esc(c)}</th>`).join("")}${hasTeacher ? "<th>教師</th>" : ""}${hasStudent ? "<th>生徒</th>" : ""}</tr></thead><tbody>${(aug.rows || []).map((r, i) => { const m = (aug.meta || [])[i] || { checks: {} }; return `<tr><td class="num dim">${i + 1}</td><td><span class="tag target">${esc(m.label)}</span></td>${featureCols.map((c) => `<td class="${spec.roles[c] === "text" ? "wrap-s" : ""}" title="${esc(r[cols.indexOf(c)])}">${esc(r[cols.indexOf(c)])}</td>`).join("")}${hasTeacher ? `<td>${mark(m.checks.teacher_agree)}${m.checks.teacher_label && m.checks.teacher_agree === false ? ` <span class="dim small">${esc(m.checks.teacher_label)}</span>` : ""}</td>` : ""}${hasStudent ? `<td>${mark(m.checks.student_agree)}${m.checks.student_pred !== undefined && m.checks.student_agree === false ? ` <span class="dim small">${esc(m.checks.student_pred)}</span>` : ""}${m.checks.student_conf ? ` <span class="dim small">${fmtPct(m.checks.student_conf, 0)}</span>` : ""}</td>` : ""}</tr>`; }).join("")}</tbody>`;
    if (aug.n_rows > (aug.rows || []).length) $("#aug-res-n").textContent += `（先頭 ${aug.rows.length} 行を表示）`;
  }
  async function downloadAugment(kind) {
    try {
      const res = await fetch(`/api/augment/export?kind=${kind}`);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
      const blob = await res.blob();
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${kind === "all" ? "augmented" : "synthetic"}_${(S.dataset.name || "data").replace(/\.[^.]+$/, "")}.csv`; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (e) { toast(e.message, "err"); }
  }
  function bindAugmentEvents() {
    $("#aug-n").oninput = (e) => { S.aug.n = Math.max(0, parseInt(e.target.value || "0", 10)); renderAugCounts(); planAugment(); };
    $$("#aug-mode button").forEach((b) => b.onclick = () => { S.aug.mode = b.dataset.v; $$("#aug-mode button").forEach((x) => x.classList.toggle("active", x === b)); $("#aug-custom").classList.toggle("hidden", S.aug.mode !== "custom"); $("#aug-cap-wrap").style.visibility = S.aug.mode === "balance" ? "visible" : "hidden"; if (S.aug.mode === "custom") renderAugCustom(); planAugment(0); });
    $("#aug-cap").onchange = (e) => { S.aug.cap = e.target.checked; planAugment(0); };
    $("#aug-batch").oninput = () => { if (S.aug.plan) renderAugPlan(S.aug.plan); };
    $("#aug-verify").onchange = () => { if (S.aug.plan) renderAugPlan(S.aug.plan); };
    $$("#aug-student-policy button").forEach((b) => b.onclick = () => $$("#aug-student-policy button").forEach((x) => x.classList.toggle("active", x === b)));
    $("#llm-provider").onchange = () => { $("#llm-fields").classList.toggle("hidden", $("#llm-provider").value === "builtin"); if (S.aug.plan) renderAugPlan(S.aug.plan); };
    $("#llm-key-clear").onclick = async () => { try { S.settings = await POST("/api/settings", { llm_api_key_clear: true }); fillLlmSettings(); toast("API キーを削除しました", "ok", 2000); } catch (e) { toast(e.message, "err"); } };
    $("#llm-save").onclick = () => saveLlmSettings().catch((e) => toast(e.message, "err"));
    $("#llm-test").onclick = testLlm;
    $("#aug-start").onclick = startAugment;
    $("#ag-cancel").onclick = async () => { if (!S.aug.job) return; try { await POST(`/api/jobs/${S.aug.job.id}/cancel`); } catch (e) { toast(e.message, "err"); } };
    $("#aug-enabled").onchange = async (e) => { try { const r = await POST("/api/augment/enable", { enabled: e.target.checked }); await refreshAugStatus(); toast(r.enabled ? "学習データに含めます" : "学習データから外しました", "ok", 2000); } catch (err) { toast(err.message, "err"); } };
    $("#aug-dl-syn").onclick = () => downloadAugment("synthetic"); $("#aug-dl-all").onclick = () => downloadAugment("all");
    $("#aug-clear").onclick = async () => { if (!confirm("合成データを削除しますか？")) return; try { await POST("/api/augment/clear"); S.aug.current = null; $("#aug-result").classList.add("hidden"); await refreshAugStatus(); renderSynthToggle(); toast("削除しました", "ok", 2000); } catch (e) { toast(e.message, "err"); } };
  }

  // ---------------------------------------------------------------- 初期化
  function bindEvents() {
    $("#theme-toggle").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    $$(".step, .side-link").forEach((b) => b.onclick = () => go(b.dataset.step));
    $$("[data-go]").forEach((b) => b.onclick = () => go(b.dataset.go));
    $("#brand").onclick = (e) => { e.preventDefault(); go("data"); };
    $("#pill-data").onclick = () => go("data"); $("#pill-job").onclick = () => go("train"); $("#pill-env").onclick = () => go("env");
    // データ
    const drop = $("#drop");
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => uploadDataset(e.dataTransfer.files[0]));
    $("#file").onchange = (e) => { uploadDataset(e.target.files[0]); e.target.value = ""; };
    $("#ds-replace").onclick = () => { $("#data-loaded").classList.add("hidden"); $("#data-empty").classList.remove("hidden"); };
    $("#ds-sheet").onchange = async (e) => { try { const r = await POST("/api/dataset/sheet", { sheet: e.target.value }); setDataset(r.dataset); } catch (err) { toast(err.message, "err"); } };
    // タスク
    $("#t-target").onchange = (e) => setRole(e.target.value, "target") || renderTask();
    $$("#t-task button").forEach((b) => b.onclick = () => { S.spec.task = b.dataset.v; S.validate = null; saveSpec(); renderTask(); });
    const onSplit = () => { S.spec.split.val = +$("#t-val").value / 100; S.spec.split.test = +$("#t-test").value / 100; S.spec.split.seed = parseInt($("#t-seed").value || "42", 10); S.spec.split.stratify = $("#t-strat").checked;
      $("#t-val-v").textContent = $("#t-val").value + "%"; $("#t-test-v").textContent = $("#t-test").value + "%"; saveSpec(); scheduleValidate(); };
    ["#t-val", "#t-test", "#t-seed", "#t-strat"].forEach((s) => { $(s).oninput = onSplit; $(s).onchange = onSplit; });
    // モデル
    $("#m-model").oninput = (e) => { S.model = e.target.value.trim(); store.set("model", S.model); $$("#presets .preset").forEach((b) => b.classList.toggle("on", b.dataset.id === S.model)); const p = S.catalog.presets.find((x) => x.id === S.model); $("#m-model-note").textContent = p ? `${p.name} · ${p.params} · ${p.note}` : S.model ? "カタログ外のモデル ID / パス（AutoModel で読み込みます）" : ""; $("#m-train").disabled = !S.model; };
    $("#hp-reset").onclick = () => { delete S.hparams[S.family]; store.set("hparams", S.hparams); renderHparams(); };
    $("#m-train").onclick = startTraining;
    // 学習
    $("#j-cancel").onclick = async () => { if (!S.job) return; try { await POST(`/api/jobs/${S.job.id}/cancel`); toast("中止を要求しました", "", 2000); } catch (e) { toast(e.message, "err"); } };
    $("#j-eval").onclick = () => go("eval");
    $("#j-again").onclick = () => go("model");
    // 評価
    $("#e-run").onchange = (e) => { S.evalRun = e.target.value; renderEvalPanel(); };
    $$("#e-split button").forEach((b) => b.onclick = () => { S.evalSplit = b.dataset.v; renderEvalPanel(); });
    // 予測
    $("#p-run").onchange = (e) => { S.predRun = e.target.value; renderPredictPanel(); };
    const pd = $("#p-drop");
    ["dragenter", "dragover"].forEach((ev) => pd.addEventListener(ev, (e) => { e.preventDefault(); pd.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => pd.addEventListener(ev, (e) => { e.preventDefault(); pd.classList.remove("over"); }));
    pd.addEventListener("drop", (e) => predictFile(e.dataTransfer.files[0]));
    $("#p-file").onchange = (e) => { predictFile(e.target.files[0]); e.target.value = ""; };
    $("#p-one").onclick = predictOne; $("#p-dl").onclick = downloadCsv;
    // 履歴 / 環境
    $("#r-filter").onchange = renderRuns; $("#r-refresh").onclick = () => loadRuns().then(renderRuns);
    $("#s-save").onclick = saveSettings;
    bindAugmentEvents();
  }
  async function init() {
    setTheme(store.get("theme", "dark"));
    bindEvents();
    try {
      const [status, catalog, samples] = await Promise.all([GET("/api/status"), GET("/api/catalog"), GET("/api/samples")]);
      $("#ver").textContent = status.version; S.catalog = catalog; S.samples = samples.samples; S.settings = status.settings; renderSamples();
      loadEnv();
      loadRuns();
      if (status.dataset) { const d = await GET("/api/dataset"); if (d.dataset) setDataset(d.dataset); }
      S.augStatus = status.augment || null;
      if (status.job && status.job.kind === "augment") { S.aug.job = { id: status.job.id, status: status.job.status, progress: status.job.progress, params: status.job.params }; pollAug(); }
      else if (status.job) { S.job = { id: status.job.id, status: status.job.status, progress: status.job.progress, params: status.job.params, curves: [], step_losses: [] }; pollJob(); }
    } catch (e) { toast("サーバーに接続できません: " + e.message, "err", 0); }
    updatePills(); updateStepper();
  }
  document.addEventListener("DOMContentLoaded", init);
})();
