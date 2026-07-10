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

  loadProjects();
})();
