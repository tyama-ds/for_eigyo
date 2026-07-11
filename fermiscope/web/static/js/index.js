/* index.js — 新規推定フォームとプロジェクト一覧。 */
(function () {
  "use strict";

  async function loadProjects() {
    const list = document.getElementById("project-list");
    try {
      const res = await fetch("/api/projects");
      const projects = await res.json();
      list.textContent = "";
      if (!projects.length) {
        const li = document.createElement("li");
        li.className = "hint";
        li.textContent = "まだ推定がありません。上のフォームからはじめてください。";
        list.appendChild(li);
        return;
      }
      for (const p of projects) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = `/projects/${encodeURIComponent(p.id)}`;
        a.textContent = p.question || p.name;
        const meta = document.createElement("div");
        meta.className = "project-meta";
        meta.textContent = `更新: ${p.updated_at ? p.updated_at.slice(0, 19).replace("T", " ") : "—"}`;
        li.appendChild(a);
        li.appendChild(meta);
        list.appendChild(li);
      }
    } catch (err) {
      list.textContent = "一覧の取得に失敗しました。ページを再読み込みしてください。";
    }
  }

  document.getElementById("new-project-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("form-status");
    const btn = document.getElementById("start-btn");
    const knownFacts = document.getElementById("known_facts").value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    const body = {
      question: document.getElementById("question").value.trim(),
      geography: document.getElementById("geography").value.trim(),
      reference_date: document.getElementById("reference_date").value.trim(),
      target_unit: document.getElementById("target_unit").value.trim(),
      known_facts: knownFacts,
      research_mode: document.getElementById("research_mode").value,
    };
    const maxSearches = document.getElementById("max_searches").value;
    if (maxSearches) body.max_searches = Number(maxSearches);
    const maxCost = document.getElementById("max_cost_usd").value;
    if (maxCost) body.max_cost_usd = Number(maxCost);

    btn.disabled = true;
    status.textContent = "問いを解析しています…";
    try {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `サーバーエラー (${res.status})`);
      }
      const report = await res.json();
      window.location.href = `/projects/${encodeURIComponent(report.project.id)}?autostart=1`;
    } catch (err) {
      status.textContent = `作成に失敗しました: ${err.message}。入力内容を確認して再度お試しください。`;
      btn.disabled = false;
    }
  });

  // ---------- LLM 接続設定 ----------
  let llmProviders = [];
  function providerMeta(value) {
    return llmProviders.find((p) => p.value === value) || { needs_key: false, needs_base: false };
  }
  function applyProviderVisibility() {
    const value = document.getElementById("llm-provider").value;
    const meta = providerMeta(value);
    const baseField = document.getElementById("llm-base-field");
    const modelField = document.getElementById("llm-model-field");
    const keyField = document.getElementById("llm-key-field");
    const proxyField = document.getElementById("llm-proxy-field");
    const showConn = value === "openai_compatible" || value === "anthropic";
    baseField.hidden = !showConn;
    modelField.hidden = !showConn;
    keyField.hidden = !meta.needs_key;
    proxyField.hidden = !showConn;
    document.getElementById("llm-base-hint").textContent = meta.base_hint || "";
    document.getElementById("llm-model-hint").textContent = meta.model_hint || "";
  }

  async function loadLlmSettings() {
    const details = document.getElementById("llm-settings-details");
    if (!details) return;
    let data;
    try {
      data = await (await fetch("/api/settings/llm")).json();
    } catch (err) { return; }
    llmProviders = data.providers || [];
    const sel = document.getElementById("llm-provider");
    sel.textContent = "";
    llmProviders.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.value;
      opt.textContent = p.label;
      sel.appendChild(opt);
    });
    const cur = data.current || {};
    sel.value = cur.provider || "noop";
    document.getElementById("llm-base").value = cur.api_base || "";
    document.getElementById("llm-model").value = cur.model || "";
    document.getElementById("llm-proxy").value = cur.proxy || "";
    document.getElementById("llm-key-state").textContent = cur.key_set ? "(設定済み)" : "(未設定)";
    sel.disabled = !data.editable;
    if (!data.editable) {
      document.getElementById("llm-settings-status").textContent =
        "このインスタンスではLLMが固定されており変更できません。";
      document.getElementById("llm-save-btn").disabled = true;
      document.getElementById("llm-test-btn").disabled = true;
    }
    applyProviderVisibility();
  }

  const llmSel = document.getElementById("llm-provider");
  if (llmSel) llmSel.addEventListener("change", applyProviderVisibility);

  const llmForm = document.getElementById("llm-settings-form");
  if (llmForm) {
    llmForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = document.getElementById("llm-settings-status");
      status.textContent = "保存中…";
      const body = {
        provider: document.getElementById("llm-provider").value,
        api_base: document.getElementById("llm-base").value.trim(),
        model: document.getElementById("llm-model").value.trim(),
        proxy: document.getElementById("llm-proxy").value.trim(),
      };
      const key = document.getElementById("llm-key").value;
      if (key) body.api_key = key;
      try {
        const res = await fetch("/api/settings/llm", {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `エラー (${res.status})`);
        }
        document.getElementById("llm-key").value = "";
        status.textContent = "保存しました。";
        loadLlmSettings();
      } catch (err) {
        status.textContent = `保存に失敗しました: ${err.message}`;
      }
    });
    document.getElementById("llm-test-btn").addEventListener("click", async () => {
      const status = document.getElementById("llm-settings-status");
      status.textContent = "接続テスト中…(先に保存してください)";
      try {
        const res = await fetch("/api/settings/llm/test", { method: "POST" });
        const data = await res.json();
        status.textContent = (data.ok ? "✓ " : "✗ ") + data.message;
      } catch (err) {
        status.textContent = `テストに失敗しました: ${err.message}`;
      }
    });
  }

  loadProjects();
  loadLlmSettings();
})();
