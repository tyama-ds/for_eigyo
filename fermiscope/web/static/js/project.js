/* project.js — プロジェクト画面: レポート描画・SSE進捗・パラメータ編集・再計算。
 * 外部データはすべて textContent 経由で挿入する(XSS防止)。 */
(function () {
  "use strict";
  const PID = document.getElementById("project-root").dataset.projectId;
  const fmt = window.FermiCharts.fmt;
  let report = null;
  let eventSource = null;

  // ---------- ユーティリティ ----------
  function h(tag, cls, textStr) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (textStr !== undefined && textStr !== null) node.textContent = textStr;
    return node;
  }
  function clear(node) { node.textContent = ""; return node; }
  function basisBadge(basis) {
    const map = {
      evidence: ["badge evidence", "証拠あり"],
      user_input: ["badge user", "ユーザー入力"],
      assumption: ["badge assumption", "仮定"],
      derived: ["badge derived", "導出値"],
      unresolved: ["badge unresolved", "未解決"],
    };
    const [cls, label] = map[basis] || ["badge", basis];
    return h("span", cls, label);
  }
  function aiBadge() { return h("span", "badge ai", "AI補助"); }
  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `エラー (${res.status})`);
    }
    return res.json();
  }
  function setStatus(msg) { document.getElementById("run-status").textContent = msg; }

  // ---------- タブ ----------
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach((p) => {
        const active = p.id === `tab-${tab.dataset.tab}`;
        p.classList.toggle("active", active);
        p.hidden = !active;
      });
    });
  });
  function switchTab(name) {
    const tab = document.querySelector(`.tab[data-tab="${name}"]`);
    if (tab) tab.click();
  }

  // ---------- ダイアログ ----------
  const backdrop = document.getElementById("detail-dialog-backdrop");
  document.getElementById("dialog-close").addEventListener("click", () => { backdrop.hidden = true; });
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) backdrop.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") backdrop.hidden = true; });
  function openDialog(title, bodyNode) {
    document.getElementById("dialog-title").textContent = title;
    const body = clear(document.getElementById("dialog-body"));
    body.appendChild(bodyNode);
    backdrop.hidden = false;
    document.getElementById("dialog-close").focus();
  }

  // ---------- 結果タブ ----------
  function renderConclusion() {
    const card = clear(document.getElementById("conclusion-card"));
    const c = report.conclusion;
    if (c.central === null || c.central === undefined) {
      card.appendChild(h("p", "hint",
        "まだ数値結果がありません。「調査を開始」を押すか、未解決のパラメータに値を入力してください。"));
      return;
    }
    card.appendChild(h("div", "big", `${c.central_display} ${unitJa(c.unit)}`));
    card.appendChild(h("div", "range", `妥当範囲(P10–P90): ${c.range_display} ${unitJa(c.unit)}`));
    const conf = h("div", "range");
    conf.appendChild(document.createTextNode("結果の信頼度: "));
    conf.appendChild(h("strong", "", c.confidence !== null ? `${Math.round(c.confidence * 100)} / 100` : "—"));
    card.appendChild(conf);
    if (c.confidence_reasons && c.confidence_reasons.length) {
      const det = h("details");
      det.appendChild(h("summary", "", "信頼度の内訳を見る"));
      const ul = h("ul");
      c.confidence_reasons.forEach((r) => ul.appendChild(h("li", "", r)));
      det.appendChild(ul);
      card.appendChild(det);
    }
  }
  function unitJa(unit) {
    const map = { person: "人", household: "世帯", item: "台", event: "件", JPY: "円", store: "店", company: "社" };
    return map[unit] || unit || "";
  }

  function renderScenarios() {
    const wrap = clear(document.getElementById("scenario-summary"));
    if (!report.scenarios.length) return;
    wrap.appendChild(h("h2", "", "シナリオ"));
    const table = h("table", "data");
    const head = h("tr");
    ["シナリオ", "値", "説明"].forEach((s) => head.appendChild(h("th", "", s)));
    table.appendChild(head);
    report.scenarios.forEach((s) => {
      const tr = h("tr");
      tr.appendChild(h("td", "", s.name));
      tr.appendChild(h("td", "num", `${s.value_display} ${unitJa(report.conclusion.unit)}`));
      tr.appendChild(h("td", "", s.description));
      table.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderCaveats() {
    const wrap = clear(document.getElementById("caveats"));
    const caveats = report.conclusion.key_caveats || [];
    if (!caveats.length) return;
    wrap.appendChild(h("h2", "", "主な注意点"));
    caveats.forEach((c) => {
      const card = h("div", "card warn");
      card.appendChild(h("p", "", c));
      wrap.appendChild(card);
    });
  }

  function renderValidationSummary() {
    const wrap = clear(document.getElementById("validation-summary"));
    const v = report.validation;
    if (!v) return;
    wrap.appendChild(h("h2", "", "検算モデルとの比較"));
    const card = h("div", v.agreement === "discrepant" ? "card error-card" : "card");
    const agreementJa = { consistent: "整合(桁が一致)", moderate: "おおむね整合", discrepant: "大きな不一致", unknown: "判定不能" };
    card.appendChild(h("p", "", `判定: ${agreementJa[v.agreement] || v.agreement} — 中心値の比 ${v.central_ratio ?? "—"} 倍 / 区間の重なり ${v.interval_overlap !== null ? Math.round(v.interval_overlap * 100) + "%" : "—"}`));
    if (v.note) card.appendChild(h("p", "hint", v.note));
    (v.warnings || []).forEach((w) => card.appendChild(h("p", "", `⚠ ${w}`)));
    const analysis = Object.values(v.difference_analysis || {});
    if (analysis.length) {
      const det = h("details");
      det.appendChild(h("summary", "", "差の分析観点"));
      const ul = h("ul");
      analysis.forEach((a) => ul.appendChild(h("li", "", a)));
      det.appendChild(ul);
      card.appendChild(det);
    }
    wrap.appendChild(card);
  }

  function renderIrreducible() {
    const wrap = clear(document.getElementById("irreducible-summary"));
    const items = report.irreducible_assumptions || [];
    if (!items.length) return;
    wrap.appendChild(h("h2", "", "これ以上分解できなかった仮定"));
    items.forEach((i) => {
      const p = report.parameters.find((x) => x.id === i.parameter_id);
      const card = h("div", "card warn");
      card.appendChild(h("h3", "", p ? p.name : i.parameter_id));
      card.appendChild(h("p", "", `これ以上、信頼できる下位データへ分解できませんでした。${i.reason}`));
      if (i.why_rejected && i.why_rejected.length) {
        const det = h("details");
        det.appendChild(h("summary", "", "分解を見送った理由"));
        const ul = h("ul");
        i.why_rejected.forEach((r) => ul.appendChild(h("li", "", r)));
        det.appendChild(ul);
        card.appendChild(det);
      }
      if (i.what_new_evidence_would_resolve_it) {
        card.appendChild(h("p", "hint", `解決に必要な情報: ${i.what_new_evidence_would_resolve_it}`));
      }
      if (i.user_editable_value) {
        const btn = h("button", "btn small", "この値を自分で入力する");
        btn.addEventListener("click", () => { switchTab("params"); });
        card.appendChild(btn);
      }
      wrap.appendChild(card);
    });
  }

  function renderContradictions() {
    const wrap = clear(document.getElementById("contradiction-summary"));
    const items = report.contradictions || [];
    if (!items.length) return;
    wrap.appendChild(h("h2", "", "証拠間の矛盾(平均で隠さず表示しています)"));
    items.forEach((con) => {
      const p = report.parameters.find((x) => x.id === con.parameter_id);
      const card = h("div", "card warn");
      card.appendChild(h("h3", "", `${p ? p.name : con.parameter_id} — ${con.ratio}倍の乖離`));
      card.appendChild(h("p", "", con.note));
      const ul = h("ul");
      Object.values(con.analysis || {}).forEach((a) => ul.appendChild(h("li", "", a)));
      card.appendChild(ul);
      wrap.appendChild(card);
    });
  }

  // ---------- スコープタブ ----------
  function renderScope() {
    const form = clear(document.getElementById("scope-form"));
    const q = report.question;
    const provisional = new Set((q.provisional || []).map((p) => p.field));
    const fields = [
      ["subject", "推定対象", q.subject],
      ["geography", "対象地域", q.geography],
      ["reference_date", "基準時点", q.reference_date],
      ["target_unit", "求める単位", q.target_unit],
      ["time_period", "対象期間(フローの場合)", q.time_period],
    ];
    fields.forEach(([key, label, value]) => {
      const field = h("div", "field");
      const lab = h("label", "", label + (provisional.has(key) ? "(暫定値です — 確認してください)" : ""));
      lab.setAttribute("for", `scope-${key}`);
      const input = h("input");
      input.id = `scope-${key}`;
      input.name = key;
      input.value = value || "";
      field.appendChild(lab);
      field.appendChild(input);
      form.appendChild(field);
    });
    const sofField = h("div", "field");
    const sofLabel = h("label", "", "ストック / フロー");
    sofLabel.setAttribute("for", "scope-stock_or_flow");
    const sel = h("select");
    sel.id = "scope-stock_or_flow";
    [["stock", "ストック(ある時点の数量)"], ["flow", "フロー(期間あたりの量)"]].forEach(([v, t]) => {
      const opt = h("option", "", t);
      opt.value = v;
      if (q.stock_or_flow === v) opt.selected = true;
      sel.appendChild(opt);
    });
    sofField.appendChild(sofLabel);
    sofField.appendChild(sel);
    form.appendChild(sofField);

    [["inclusions", "含めるもの(1行1件)", (q.inclusions || []).join("\n")],
     ["exclusions", "除外するもの(1行1件)", (q.exclusions || []).join("\n")],
     ["known_facts", "既知の情報(1行1件)", (q.known_facts || []).join("\n")]].forEach(([key, label, value]) => {
      const field = h("div", "field");
      const lab = h("label", "", label);
      lab.setAttribute("for", `scope-${key}`);
      const ta = h("textarea");
      ta.id = `scope-${key}`;
      ta.rows = 2;
      ta.value = value;
      field.appendChild(lab);
      field.appendChild(ta);
      form.appendChild(field);
    });

    const actions = h("div", "actions");
    const save = h("button", "btn primary", "スコープを保存(モデル再生成)");
    save.type = "submit";
    actions.appendChild(save);
    const status = h("span", "hint");
    status.id = "scope-status";
    actions.appendChild(status);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      status.textContent = "保存中…";
      try {
        report = await api(`/api/projects/${PID}/question`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            subject: document.getElementById("scope-subject").value,
            geography: document.getElementById("scope-geography").value,
            reference_date: document.getElementById("scope-reference_date").value,
            target_unit: document.getElementById("scope-target_unit").value,
            time_period: document.getElementById("scope-time_period").value,
            stock_or_flow: sel.value,
            inclusions: document.getElementById("scope-inclusions").value.split("\n").map((s) => s.trim()).filter(Boolean),
            exclusions: document.getElementById("scope-exclusions").value.split("\n").map((s) => s.trim()).filter(Boolean),
            known_facts: document.getElementById("scope-known_facts").value.split("\n").map((s) => s.trim()).filter(Boolean),
            regenerate_models: true,
          }),
        });
        status.textContent = "保存しました。";
        renderAll();
      } catch (err) {
        status.textContent = `保存に失敗しました: ${err.message}`;
      }
    };
  }

  // ---------- モデルタブ ----------
  function renderModels() {
    const wrap = clear(document.getElementById("model-list"));
    report.models.forEach((m) => {
      const roleJa = { primary: "主モデル", check: "検算モデル", rejected: "不採用", candidate: "候補" };
      const card = h("div", `card ${m.role === "primary" ? "primary-model" : m.role === "check" ? "check-model" : ""}`);
      const title = h("h3");
      title.appendChild(h("strong", "", `[${roleJa[m.role] || m.role}] `));
      title.appendChild(document.createTextNode(m.name));
      if (m.proposed_by === "llm") title.appendChild(aiBadge());
      card.appendChild(title);
      card.appendChild(h("p", "", m.description));
      const code = h("p");
      code.appendChild(h("code", "", m.expression_raw));
      card.appendChild(code);
      card.appendChild(h("p", "meta", `総合スコア: ${m.total_score} / 単位検査: ${m.unit_check_passed ? "合格 — " + m.unit_check_detail : "不合格 — " + m.unit_check_detail}`));
      if (m.selection_reason) card.appendChild(h("p", "meta", `選択理由: ${m.selection_reason}`));
      const det = h("details");
      det.appendChild(h("summary", "", "採点内訳"));
      const ul = h("ul");
      const scoreJa = { estimability: "推定可能性", unit_consistency: "単位整合性", explainability: "説明可能性", evidence_availability: "証拠入手可能性", double_counting_risk: "二重計上リスク(高いほど安全)", dependency_risk: "変数間依存リスク(高いほど安全)", independence: "他モデルとの独立性" };
      Object.entries(m.scores || {}).forEach(([k, v]) => ul.appendChild(h("li", "", `${scoreJa[k] || k}: ${v}`)));
      det.appendChild(ul);
      card.appendChild(det);
      if (m.role !== "primary") {
        const btn = h("button", "btn small", "このモデルを主モデルにする");
        btn.addEventListener("click", async () => {
          const currentPrimary = report.models.find((x) => x.role === "primary");
          try {
            report = await api(`/api/projects/${PID}/models/select`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ primary_id: m.id, check_id: currentPrimary ? currentPrimary.id : null }),
            });
            renderAll();
          } catch (err) { alert(`変更できませんでした: ${err.message}`); }
        });
        card.appendChild(btn);
      }
      wrap.appendChild(card);
    });
  }

  // ---------- 進捗タブ ----------
  function renderProgress(status) {
    const grid = clear(document.getElementById("progress-counters"));
    const run = status || (report.run ? {
      stage: report.run.stage, status: report.run.status,
      searches_executed: report.run.searches_executed,
      cache_hits: report.run.search_cache_hits,
      documents_fetched: report.run.documents_fetched,
      evidence_found: report.run.evidence_found,
      parameters_verified: report.run.parameters_verified,
      warnings: report.run.warnings_count,
      ai_fallback_uses: report.run.ai_fallback_uses,
    } : null);
    if (!run) { grid.appendChild(h("div", "hint", "まだ調査は実行されていません。")); return; }
    const stageJa = { parsing: "問いの解析", model_generation: "モデル生成", planning: "検索計画", searching: "検索中", extracting: "抽出中", ranking: "証拠採点", fusing: "統合", verifying: "敵対的検証", decomposing: "再分解判断", simulating: "シミュレーション", sensitivity: "感度分析", validating: "検算", reporting: "整理", done: "完了", failed: "失敗", cancelled: "キャンセル" };
    const items = [
      ["現在の段階", stageJa[run.stage] || run.stage],
      ["完了した検索数", run.searches_executed],
      ["キャッシュ利用", run.cache_hits],
      ["取得した資料数", run.documents_fetched],
      ["発見した証拠数", run.evidence_found],
      ["検証済みパラメータ", run.parameters_verified],
      ["警告数", run.warnings],
      ["AIフォールバック", run.ai_fallback_uses],
    ];
    items.forEach(([label, value]) => {
      const cell = h("div");
      cell.appendChild(h("dt", "", label));
      cell.appendChild(h("dd", "", String(value ?? 0)));
      grid.appendChild(cell);
    });
  }
  function appendEvent(ev) {
    const log = document.getElementById("event-log");
    const li = h("li");
    li.appendChild(h("span", "etype", ev.type));
    li.appendChild(document.createTextNode(ev.message));
    log.appendChild(li);
    while (log.children.length > 300) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }

  // ---------- 式タブ ----------
  function paramLabels() {
    const labels = {};
    report.parameters.forEach((p) => { labels[p.id] = p.name; });
    return labels;
  }
  function renderFormula() {
    const wrap = clear(document.getElementById("formula-view"));
    const labels = paramLabels();
    report.models.filter((m) => m.role === "primary" || m.role === "check").forEach((m) => {
      const card = h("div", `card ${m.role === "primary" ? "primary-model" : "check-model"}`);
      card.appendChild(h("h3", "", `${m.role === "primary" ? "主モデル" : "検算モデル"}: ${m.name}`));
      const disp = h("div", "formula-display");
      disp.appendChild(window.FermiFormula.render(m.formula_tree, labels, openParamDialog));
      card.appendChild(disp);
      card.appendChild(h("p", "meta", `目標単位: ${m.target_unit} / 単位検査: ${m.unit_check_passed ? "合格" : "不合格"} — ${m.unit_check_detail}`));
      const det = h("details");
      det.appendChild(h("summary", "", "分解ツリーを見る"));
      det.appendChild(window.FermiFormula.renderTree(m.formula_tree, labels, openParamDialog));
      card.appendChild(det);
      wrap.appendChild(card);
    });
    if (!wrap.children.length) wrap.appendChild(h("p", "hint", "モデルがまだありません。"));
  }

  // ---------- 図表タブ ----------
  function renderCharts() {
    const primary = report.models.find((m) => m.role === "primary");
    const check = report.models.find((m) => m.role === "check");
    const primarySim = primary ? report.simulation.results.find((r) => r.model_id === primary.id) : null;
    const checkSim = check ? report.simulation.results.find((r) => r.model_id === check.id) : null;

    window.FermiCharts.barChart(
      document.getElementById("chart-scenarios"),
      report.scenarios.filter((s) => ["bear", "base", "bull"].includes(s.kind)).map((s) => ({
        label: s.name, value: s.value,
        color: s.kind === "base" ? "#1d4ed8" : s.kind === "bear" ? "#f59e0b" : "#0e7490",
      }))
    );
    if (primarySim) {
      window.FermiCharts.histogram(
        document.getElementById("chart-histogram"),
        primarySim.histogram_bin_edges, primarySim.histogram_counts,
        [
          { value: primarySim.quantiles["0.1"], label: "P10" },
          { value: primarySim.quantiles["0.5"], label: "P50" },
          { value: primarySim.quantiles["0.9"], label: "P90" },
        ].filter((m) => m.value !== undefined)
      );
    } else {
      document.getElementById("chart-histogram").textContent = "シミュレーション結果がありません";
    }
    const primarySens = report.sensitivity.filter((s) => primary && s.model_id === primary.id);
    const base = primarySim ? primarySim.median : null;
    window.FermiCharts.tornado(
      document.getElementById("chart-tornado"),
      primarySens.slice().sort((a, b) => (b.oat_span || 0) - (a.oat_span || 0)).map((s) => ({
        label: s.parameter_name || s.parameter_id, low: s.oat_low_output, high: s.oat_high_output,
      })),
      base ?? 0
    );
    window.FermiCharts.barChart(
      document.getElementById("chart-importance"),
      primarySens.slice().sort((a, b) => b.importance - a.importance).map((s) => ({
        label: s.parameter_name || s.parameter_id, value: s.importance, color: "#6d28d9",
      }))
    );
    const modelItems = [];
    if (primarySim) modelItems.push({ label: "主モデル", low: primarySim.quantiles["0.1"], high: primarySim.quantiles["0.9"], central: primarySim.median });
    if (checkSim) modelItems.push({ label: "検算モデル", low: checkSim.quantiles["0.1"], high: checkSim.quantiles["0.9"], central: checkSim.median });
    window.FermiCharts.intervalChart(document.getElementById("chart-models"), modelItems);
  }

  // ---------- パラメータタブ ----------
  function critiquesFor(pid) { return report.critiques.filter((c) => c.parameter_id === pid); }
  function evidenceFor(pid) { return report.evidence.filter((e) => e.parameter_id === pid); }

  function paramDetailNode(p, compact) {
    const wrap = h("div");
    const head = h("p");
    head.appendChild(basisBadge(p.value_basis));
    if (p.ai_assisted) head.appendChild(aiBadge());
    if (p.user_overridden) head.appendChild(h("span", "badge user", "手動編集済み"));
    head.appendChild(document.createTextNode(` ${p.definition || ""}`));
    wrap.appendChild(head);

    if (p.status === "unresolved") {
      const alertCard = h("div", "card error-card");
      alertCard.appendChild(h("p", "", `未解決: ${p.unresolved_reason || "証拠が見つからず、値を捏造していません。下の欄から値を入力してください。"}`));
      wrap.appendChild(alertCard);
    } else {
      wrap.appendChild(h("p", "", `中心値 ${p.central_display} ${p.unit} (low ${p.low !== null ? fmt(p.low) : "—"} / high ${p.high !== null ? fmt(p.high) : "—"}) — 分布: ${p.distribution}`));
      if (p.distribution_rationale) wrap.appendChild(h("p", "meta", `分布の選択理由: ${p.distribution_rationale}`));
      if (p.fusion_note) wrap.appendChild(h("p", "meta", `統合方法: ${p.fusion_note}`));
      wrap.appendChild(h("p", "meta", `対象: ${p.geography || "—"} / 時点: ${p.period || "—"} / 信頼度: ${p.confidence ?? "—"} / 感度(Spearman): ${p.sensitivity !== null && p.sensitivity !== undefined ? p.sensitivity.toFixed(2) : "—"}`));
    }
    if (p.verification_note) wrap.appendChild(h("p", "meta", `✔ ${p.verification_note}`));
    (p.assumptions || []).forEach((a) => {
      const asum = h("p");
      asum.appendChild(h("span", "badge assumption", "仮定"));
      asum.appendChild(document.createTextNode(" " + a));
      wrap.appendChild(asum);
    });

    // 証拠
    const evs = evidenceFor(p.id);
    if (evs.length) {
      const det = h("details");
      if (!compact) det.open = true;
      det.appendChild(h("summary", "", `出典と根拠(${evs.length}件)`));
      evs.forEach((e) => {
        const card = h("div", "card");
        const title = h("p");
        title.appendChild(h("strong", "", `[${e.source_class}] `));
        const link = h("a", "", e.title || e.url);
        link.href = e.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        title.appendChild(link);
        if (e.ai_assisted) title.appendChild(aiBadge());
        card.appendChild(title);
        card.appendChild(h("p", "meta", `スコア ${e.evidence_score ?? "—"} / 発行 ${e.publication_date || "—"} / 取得 ${(e.retrieval_date || "").slice(0, 10)} / 値 ${e.extracted_value ?? "—"} ${e.unit || ""} / 位置 ${e.locator || "—"}`));
        if (e.excerpt) card.appendChild(h("blockquote", "meta", `「${e.excerpt}」`));
        if (e.incompatible_reason) card.appendChild(h("p", "meta", `⚠ ${e.incompatible_reason}`));
        card.appendChild(h("p", "meta", `採点理由: ${(e.scoring_reasons || []).join(" ")}`));
        det.appendChild(card);
      });
      wrap.appendChild(det);
    }

    // 批判
    const crits = critiquesFor(p.id);
    if (crits.length) {
      const det = h("details");
      if (!compact) det.open = true;
      det.appendChild(h("summary", "", `敵対的検証の指摘(${crits.length}件)`));
      crits.forEach((c) => {
        const card = h("div", c.severity >= 0.6 ? "card error-card" : "card warn");
        const title = h("p");
        title.appendChild(h("strong", "", `重大度 ${Math.round(c.severity * 100)}/100 `));
        if (c.ai_assisted) { title.appendChild(aiBadge()); title.appendChild(h("span", "badge assumption", "AI仮説(根拠なし)")); }
        title.appendChild(document.createTextNode(" " + c.claim));
        card.appendChild(title);
        card.appendChild(h("p", "meta", `検出: ${c.detected_by === "deterministic_check" ? "決定論チェック" : c.detected_by === "critique_search" ? "反証検索" : c.detected_by} / ${c.check_detail || ""}`));
        if (c.estimated_impact) card.appendChild(h("p", "meta", `影響: ${c.estimated_impact}(バイアス方向: ${c.direction === "up" ? "過大" : c.direction === "down" ? "過小" : "不明"})`));
        if (c.recommended_action) card.appendChild(h("p", "meta", `推奨対応: ${c.recommended_action}`));
        card.appendChild(h("p", "meta", `状態: ${c.resolution_status}${c.resolution_note ? " — " + c.resolution_note : ""}`));
        det.appendChild(card);
      });
      wrap.appendChild(det);
    }

    // 編集フォーム
    const form = h("div", "param-edit-row");
    const mkField = (label, id, value) => {
      const f = h("div", "field");
      const lab = h("label", "", label);
      lab.setAttribute("for", id);
      const input = h("input");
      input.type = "number"; input.step = "any"; input.id = id;
      if (value !== null && value !== undefined) input.value = value;
      f.appendChild(lab); f.appendChild(input);
      return [f, input];
    };
    const [f1, inCentral] = mkField("中心値", `edit-central-${p.id}`, p.central);
    const [f2, inLow] = mkField("low", `edit-low-${p.id}`, p.low);
    const [f3, inHigh] = mkField("high", `edit-high-${p.id}`, p.high);
    const distField = h("div", "field");
    const distLab = h("label", "", "分布");
    distLab.setAttribute("for", `edit-dist-${p.id}`);
    const distSel = h("select");
    distSel.id = `edit-dist-${p.id}`;
    ["lognormal", "triangular", "uniform", "loguniform", "fixed"].forEach((d) => {
      const opt = h("option", "", d);
      opt.value = d;
      if (p.distribution === d) opt.selected = true;
      distSel.appendChild(opt);
    });
    distField.appendChild(distLab); distField.appendChild(distSel);
    const saveBtn = h("button", "btn primary small", "保存して再計算");
    const statusSpan = h("span", "hint");
    saveBtn.addEventListener("click", async () => {
      statusSpan.textContent = "再計算中…";
      try {
        const body = { distribution: distSel.value, note: "GUIから編集" };
        if (inCentral.value !== "") body.central = Number(inCentral.value);
        if (inLow.value !== "") body.low = Number(inLow.value);
        if (inHigh.value !== "") body.high = Number(inHigh.value);
        report = await api(`/api/projects/${PID}/parameters/${p.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        statusSpan.textContent = "再計算しました(Web検索は再実行していません)。";
        renderAll();
        backdrop.hidden = true;
      } catch (err) {
        statusSpan.textContent = `失敗: ${err.message}`;
      }
    });
    form.appendChild(f1); form.appendChild(f2); form.appendChild(f3); form.appendChild(distField);
    form.appendChild(saveBtn); form.appendChild(statusSpan);
    wrap.appendChild(h("h3", "", "値を手動で編集"));
    wrap.appendChild(form);

    // 変更履歴
    if (p.history && p.history.length) {
      const det = h("details");
      det.appendChild(h("summary", "", `変更履歴(${p.history.length}件)`));
      const ul = h("ul");
      p.history.forEach((hh) => {
        ul.appendChild(h("li", "meta", `${(hh.timestamp || "").slice(0, 19)} [${hh.actor}] ${hh.field}: ${hh.old_value ?? "—"} → ${hh.new_value ?? "—"} ${hh.note || ""}`));
      });
      det.appendChild(ul);
      wrap.appendChild(det);
    }
    return wrap;
  }

  function openParamDialog(pid) {
    const p = report.parameters.find((x) => x.id === pid);
    if (!p) return;
    openDialog(p.name, paramDetailNode(p, false));
  }

  function renderParams() {
    const wrap = clear(document.getElementById("param-list"));
    report.parameters
      .filter((p) => {
        const models = report.models.filter((m) => m.role === "primary" || m.role === "check");
        return models.some((m) => m.parameter_ids.includes(p.id)) || p.decomposition_status === "decomposed";
      })
      .forEach((p) => {
        const card = h("div", p.status === "unresolved" ? "card error-card" : "card");
        const title = h("h3");
        title.appendChild(document.createTextNode(p.name + " "));
        title.appendChild(basisBadge(p.value_basis));
        if (p.decomposition_status === "decomposed") title.appendChild(h("span", "badge derived", "分解済み"));
        if (p.decomposition_status === "irreducible") title.appendChild(h("span", "badge assumption", "分解不能"));
        card.appendChild(title);
        const det = h("details");
        det.appendChild(h("summary", "", p.status === "unresolved"
          ? "未解決 — 値の入力が必要です(クリックで開く)"
          : `${p.central_display} ${p.unit}(low ${p.low !== null ? fmt(p.low) : "—"} / high ${p.high !== null ? fmt(p.high) : "—"})— 詳細・編集`));
        det.appendChild(paramDetailNode(p, true));
        card.appendChild(det);
        wrap.appendChild(card);
      });
  }

  // ---------- 証拠タブ ----------
  function renderEvidence() {
    const wrap = clear(document.getElementById("evidence-list"));
    if (!report.evidence.length) { wrap.appendChild(h("p", "hint", "まだ証拠がありません。")); return; }
    const table = h("table", "data");
    const head = h("tr");
    ["採用", "クラス", "スコア", "タイトル / URL", "対象パラメータ", "発行日", "取得日", "対象時点", "方法", "根拠箇所"].forEach((s) => head.appendChild(h("th", "", s)));
    table.appendChild(head);
    report.evidence.slice().sort((a, b) => (b.evidence_score || 0) - (a.evidence_score || 0)).forEach((e) => {
      const tr = h("tr");
      const tdAccept = h("td");
      const checkbox = h("input");
      checkbox.type = "checkbox";
      checkbox.checked = e.accepted;
      checkbox.setAttribute("aria-label", `${e.title} を採用する`);
      checkbox.addEventListener("change", async () => {
        let reason = "";
        if (!checkbox.checked) {
          reason = prompt("不採用の理由を入力してください(監査ログに記録されます):", "") || "理由未記入";
        }
        try {
          report = await api(`/api/projects/${PID}/evidence/${e.id}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ accepted: checkbox.checked, rejection_reason: reason }),
          });
          renderAll();
        } catch (err) {
          alert(`変更できませんでした: ${err.message}`);
          checkbox.checked = !checkbox.checked;
        }
      });
      tdAccept.appendChild(checkbox);
      if (!e.accepted && e.rejection_reason) tdAccept.appendChild(h("div", "meta", e.rejection_reason));
      tr.appendChild(tdAccept);
      tr.appendChild(h("td", "", e.source_class));
      tr.appendChild(h("td", "num", e.evidence_score !== null ? String(e.evidence_score) : "—"));
      const tdTitle = h("td");
      const link = h("a", "", e.title || e.url);
      link.href = e.url; link.target = "_blank"; link.rel = "noopener noreferrer";
      tdTitle.appendChild(link);
      if (e.ai_assisted) tdTitle.appendChild(aiBadge());
      tdTitle.appendChild(h("div", "meta", e.url));
      tr.appendChild(tdTitle);
      const p = report.parameters.find((x) => x.id === e.parameter_id);
      tr.appendChild(h("td", "", p ? p.name : e.parameter_id));
      tr.appendChild(h("td", "", e.publication_date || "—"));
      tr.appendChild(h("td", "", (e.retrieval_date || "").slice(0, 10)));
      tr.appendChild(h("td", "", e.time_period || "—"));
      tr.appendChild(h("td", "", e.methodology_summary ? e.methodology_summary.slice(0, 40) : "記載なし"));
      tr.appendChild(h("td", "", (e.excerpt || "").slice(0, 60)));
      table.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  // ---------- 監査ログタブ ----------
  function renderAudit() {
    const wrap = clear(document.getElementById("audit-list"));
    const table = h("table", "data");
    const head = h("tr");
    ["時刻", "種別", "内容"].forEach((s) => head.appendChild(h("th", "", s)));
    table.appendChild(head);
    (report.audit_events || []).slice().reverse().forEach((ev) => {
      const tr = h("tr");
      tr.appendChild(h("td", "", (ev.timestamp || "").slice(0, 19).replace("T", " ")));
      tr.appendChild(h("td", "", ev.category));
      const td = h("td", "", ev.message);
      const dataStr = JSON.stringify(ev.data || {});
      if (dataStr !== "{}") {
        const det = h("details");
        det.appendChild(h("summary", "", "詳細"));
        det.appendChild(h("pre", "meta", dataStr));
        td.appendChild(det);
      }
      tr.appendChild(td);
      table.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  // ---------- エクスポート ----------
  function renderExportLinks() {
    document.getElementById("export-json").href = `/api/projects/${PID}/export/json`;
    document.getElementById("export-csv").href = `/api/projects/${PID}/export/csv`;
    document.getElementById("export-html").href = `/api/projects/${PID}/export/html`;
    document.getElementById("export-md").href = `/api/projects/${PID}/export/md`;
  }

  // ---------- 調査実行・SSE ----------
  function connectEvents() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/api/projects/${PID}/events`);
    eventSource.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.type === "hello") return;
      appendEvent(ev);
      if (ev.data && ev.data.stage !== undefined) renderProgress(Object.assign({ status: "running" }, ev.data));
      if (["done", "failed", "cancelled"].includes(ev.type)) {
        eventSource.close();
        setStatus(ev.type === "done" ? "調査が完了しました。" : ev.message);
        document.getElementById("research-btn").disabled = false;
        document.getElementById("cancel-btn").hidden = true;
        loadReport().then(() => { renderAll(); switchTab("result"); });
      }
    };
    eventSource.onerror = () => { /* keepalive切断時は静かに再接続待ち */ };
  }

  async function startResearch() {
    const btn = document.getElementById("research-btn");
    btn.disabled = true;
    document.getElementById("cancel-btn").hidden = false;
    setStatus("調査を実行しています…(実際の検索・取得件数が調査状況タブに表示されます)");
    switchTab("progress");
    connectEvents();
    try {
      await api(`/api/projects/${PID}/research/start`, { method: "POST" });
    } catch (err) {
      setStatus(`開始できませんでした: ${err.message}`);
      btn.disabled = false;
      document.getElementById("cancel-btn").hidden = true;
    }
  }

  document.getElementById("research-btn").addEventListener("click", startResearch);
  document.getElementById("cancel-btn").addEventListener("click", async () => {
    try {
      await api(`/api/projects/${PID}/research/cancel`, { method: "POST" });
      setStatus("キャンセルを要求しました…");
    } catch (err) { setStatus(err.message); }
  });
  document.getElementById("recalc-btn").addEventListener("click", async () => {
    setStatus("ローカル再計算中…");
    try {
      report = await api(`/api/projects/${PID}/recalculate`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
      });
      renderAll();
      setStatus("再計算が完了しました(Web検索は再実行していません)。");
    } catch (err) { setStatus(`再計算に失敗しました: ${err.message}`); }
  });

  // ---------- 全体描画 ----------
  function renderAll() {
    renderConclusion();
    renderScenarios();
    renderCaveats();
    renderValidationSummary();
    renderIrreducible();
    renderContradictions();
    renderScope();
    renderModels();
    renderProgress(null);
    renderFormula();
    renderCharts();
    renderParams();
    renderEvidence();
    renderAudit();
    renderExportLinks();
  }

  async function loadReport() {
    report = await api(`/api/projects/${PID}`);
  }

  loadReport().then(() => {
    renderAll();
    const params = new URLSearchParams(window.location.search);
    const hasResult = report.conclusion.central !== null && report.conclusion.central !== undefined;
    if (params.get("autostart") === "1" && !hasResult) startResearch();
  }).catch((err) => {
    document.getElementById("conclusion-card").textContent = `読み込みに失敗しました: ${err.message}`;
  });
})();
