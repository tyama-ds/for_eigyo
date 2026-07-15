/* ============================================================
   Kaleido Agents — マルチエージェント・オーケストレータ
   ------------------------------------------------------------
   構成:
     1. ユーティリティ（DOM / escape / markdown / toast）
     2. サブエージェント定義
     3. ツールモジュール（レジストリ + 組み込みツール + カスタムツール）
     4. プランナー（ルールベース + LLM 併用）
     5. オーケストレータ（実行エンジン、イベント発火）
     6. UI（レジストリ表示 / チャット / インスペクタ / 履歴 / 設定）
   すべてブラウザ内で動作する。サーバーは /api/fetch と /api/llm のみ提供。
   ============================================================ */
"use strict";

/* ---------------- 1. ユーティリティ ---------------- */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const LS = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem("kaleido." + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("kaleido." + key, JSON.stringify(value)); } catch {}
  },
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtNum(n) {
  if (!Number.isFinite(n)) return String(n);
  if (Number.isInteger(n) && Math.abs(n) < 1e15) return n.toLocaleString("ja-JP");
  return String(Math.round(n * 1e8) / 1e8);
}

function truncate(s, n) {
  s = String(s);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/** 最小限の Markdown → HTML（先に全体をエスケープするので XSS 安全） */
function mdToHtml(src) {
  let text = escapeHtml(String(src ?? ""));
  const codeBlocks = [];
  text = text.replace(/```([\s\S]*?)```/g, (_, body) => {
    codeBlocks.push(`<pre><code>${body.replace(/^\n|\n$/g, "")}</code></pre>`);
    return `\u0000CB${codeBlocks.length - 1}\u0000`;
  });
  const lines = text.split("\n");
  const out = [];
  let listMode = null; // "ul" | "ol" | null
  const closeList = () => { if (listMode) { out.push(`</${listMode}>`); listMode = null; } };
  for (const line of lines) {
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-・*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.．)]\s+(.*)$/);
    const bq = line.match(/^&gt;\s?(.*)$/);
    if (h) {
      closeList();
      const lvl = Math.min(h[1].length + 1, 4);
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
    } else if (/^\s*(---|＊＊＊|___)\s*$/.test(line)) {
      closeList(); out.push("<hr>");
    } else if (ul) {
      if (listMode !== "ul") { closeList(); out.push("<ul>"); listMode = "ul"; }
      out.push(`<li>${inline(ul[1])}</li>`);
    } else if (ol) {
      if (listMode !== "ol") { closeList(); out.push("<ol>"); listMode = "ol"; }
      out.push(`<li>${inline(ol[1])}</li>`);
    } else if (bq) {
      closeList(); out.push(`<blockquote>${inline(bq[1])}</blockquote>`);
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList(); out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  let html = out.join("\n");
  html = html.replace(/\u0000CB(\d+)\u0000/g, (_, i) => codeBlocks[+i]);
  return html;

  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
}

function toast(msg, isError = false) {
  const box = $("#toast-box");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " err" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : undefined;
  const res = await fetch(path, opt);
  return res.json();
}

/* ---------------- 2. サブエージェント定義 ---------------- */

const AGENTS = [
  {
    id: "planner", name: "プランナー", icon: "🧭", color: "var(--c-planner)",
    desc: "依頼を解析してステップに分解し、担当エージェントとツールを割り当てる司令塔",
  },
  {
    id: "researcher", name: "リサーチャー", icon: "🔎", color: "var(--c-researcher)",
    desc: "Webページの取得・LLM への質問・メモの検索など、情報収集を担当",
  },
  {
    id: "analyst", name: "アナリスト", icon: "📐", color: "var(--c-analyst)",
    desc: "計算・日付・単位変換・JSON・図表作成など、確定的な処理を担当",
  },
  {
    id: "writer", name: "ライター", icon: "✍️", color: "var(--c-writer)",
    desc: "各ステップの結果を統合し最終回答に仕上げる。要約・図表も担当できる",
  },
  {
    id: "reviewer", name: "レビュアー", icon: "✅", color: "var(--c-reviewer)",
    desc: "全ステップの成否を検査し、品質チェックの結果を報告する",
  },
];
const agentById = (id) => AGENTS.find((a) => a.id === id) ||
  { id: "orchestrator", name: "オーケストレータ", icon: "🎛️", color: "var(--c-orchestra)" };

/* ---------------- 3. ツールモジュール ---------------- */
/* ツール = { id, name, icon, color, desc, agents: [担当候補...], run(input, ctx) }
   agents は担当できるサブエージェントの候補（得意順）。実際の割り当ては
   実行時にオーケストレータが動的に決める（assignAgents 参照）。
   ctx = { request, results, llmOn, callLLM, emit }                */

function normalizeExpr(s) {
  const z2h = (str) => str.replace(/[０-９．（）＋－＊／％＾]/g,
    (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
  return z2h(s)
    .replace(/[×✕✖]/g, "*").replace(/[÷➗]/g, "/")
    .replace(/[，,]/g, "").replace(/\^/g, "**");
}

function extractExpression(request) {
  const norm = normalizeExpr(request);
  const matches = norm.match(/[\d(][\d\s+\-*/%().]*[\d)]/g) || [];
  const candidates = matches
    .map((m) => m.trim())
    .filter((m) => /[+\-*/%]/.test(m) && (m.match(/\d+(\.\d+)?/g) || []).length >= 2);
  candidates.sort((a, b) => b.length - a.length);
  return candidates[0] || "";
}

function extractJson(request) {
  const start = Math.min(
    ...["{", "["].map((c) => {
      const i = request.indexOf(c);
      return i === -1 ? Infinity : i;
    }),
  );
  if (!Number.isFinite(start)) return "";
  const open = request[start];
  const close = open === "{" ? "}" : "]";
  const end = request.lastIndexOf(close);
  if (end <= start) return "";
  return request.slice(start, end + 1);
}

/** 「」『』内、または「◯◯して:」以降のテキストを取り出す */
function extractQuoted(request) {
  const q = request.match(/[「『]([\s\S]+?)[」』]/);
  if (q) return q[1];
  const colon = request.match(/[:：]\s*([\s\S]+)$/);
  if (colon) return colon[1].trim();
  return "";
}

const WEEKDAYS_JA = ["日", "月", "火", "水", "木", "金", "土"];
function fmtDate(d) {
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日(${WEEKDAYS_JA[d.getDay()]})`;
}
function parseDates(request) {
  const found = [];
  const re = /(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})日?|(\d{1,2})月(\d{1,2})日/g;
  let m;
  while ((m = re.exec(request))) {
    const now = new Date();
    const d = m[1]
      ? new Date(+m[1], +m[2] - 1, +m[3])
      : new Date(now.getFullYear(), +m[4] - 1, +m[5]);
    if (!isNaN(d)) found.push(d);
  }
  return found;
}

/** 抽出型サマライザ（LLM なしでも動くフォールバック） */
function extractiveSummary(text, maxSentences = 5) {
  const sentences = text
    .replace(/\s+/g, " ")
    .split(/(?<=[。．！？!?])\s*|\n+/)
    .map((s) => s.trim())
    .filter((s) => s.length >= 12 && s.length <= 400);
  if (sentences.length <= maxSentences) return sentences.join("\n");
  const freq = {};
  const tokenize = (s) => (s.match(/[一-龠ぁ-んァ-ヴa-zA-Z0-9]{2,}/g) || []);
  for (const s of sentences) for (const w of tokenize(s)) freq[w] = (freq[w] || 0) + 1;
  const scored = sentences.map((s, i) => {
    const words = tokenize(s);
    const score = words.reduce((acc, w) => acc + (freq[w] || 0), 0) / Math.sqrt(words.length || 1);
    return { s, i, score: score + (i < 3 ? 2 : 0) };
  });
  return scored
    .sort((a, b) => b.score - a.score)
    .slice(0, maxSentences)
    .sort((a, b) => a.i - b.i)
    .map((x) => "・" + x.s)
    .join("\n");
}

/* ---- 図表・表作成（SVG/HTML 生成、外部ライブラリなし） ----
   dataset = { labels: [...], series: [{name, values: [...]}], source }
   単一系列は series.length === 1。複数系列はグループ棒・積み上げ棒・
   複数折れ線として描画する。 */

const CHART_COLORS = ["#8b5cf6", "#06b6d4", "#f59e0b", "#ec4899", "#22c55e",
  "#f97316", "#0ea5e9", "#14b8a6", "#ef4444", "#a3e635"];

/** テキストから {label, value} ペア列を取り出す */
function parsePairs(text) {
  const segs = text.split(/[、,;\n]/).map((s) => s.trim()).filter(Boolean);
  const items = [];
  for (const seg of segs) {
    let m = seg.match(/^(.+?)[\s:：=]+(-?\d+(?:\.\d+)?)\s*(?:個|件|円|人|台|kg|km|%|％)?$/);
    if (m) { items.push({ label: truncate(m[1].trim(), 14), value: +m[2] }); continue; }
    m = seg.match(/^(-?\d+(?:\.\d+)?)$/);
    if (m) items.push({ label: String(items.length + 1), value: +m[1] });
  }
  return items;
}

/** グラフ/表の指示語をデータ解析の邪魔にならないよう取り除く */
function stripChartPhrases(text) {
  return text
    .replace(/を?(積み上げ|積上げ|グループ)?(棒|円|折れ線|パイ)?(グラフ|チャート|図表)+(に|で|へ|を)?(して|作成|作って|描いて|書いて|表示)?(して)?(ください)?/g, " ")
    .replace(/を?(表|テーブル|一覧表)(に|で|へ)(して|作成|作って|整理|まとめて|表示)?(して)?(ください)?/g, " ")
    .replace(/の?(推移|変化|トレンド|内訳|割合|比率|シェア)(を|に|で)?/g, " ");
}

function jsonToDataset(data) {
  if (Array.isArray(data)) {
    if (data.length && data.every((v) => typeof v === "number")) {
      const vals = data.slice(0, 24);
      return { labels: vals.map((_, i) => String(i + 1)), series: [{ name: "値", values: vals }] };
    }
    const rows = data.filter((r) => r && typeof r === "object" && !Array.isArray(r)).slice(0, 24);
    if (rows.length) {
      const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
      const labelKey = keys.find((k) => rows.some((r) => typeof r[k] === "string"));
      const numKeys = keys.filter((k) => rows.some((r) => typeof r[k] === "number")).slice(0, 8);
      if (numKeys.length) {
        return {
          labels: rows.map((r, i) => truncate(String(labelKey ? r[labelKey] ?? i + 1 : i + 1), 14)),
          series: numKeys.map((k) => ({ name: k, values: rows.map((r) => Number(r[k]) || 0) })),
        };
      }
    }
    return null;
  }
  if (data && typeof data === "object") {
    const entries = Object.entries(data).filter(([, v]) => typeof v === "number").slice(0, 24);
    if (entries.length) {
      return { labels: entries.map(([k]) => truncate(k, 14)), series: [{ name: "値", values: entries.map(([, v]) => v) }] };
    }
  }
  return null;
}

/** 依頼文・直前のJSON結果から系列つきデータセットを作る。
    複数系列は「A店: 1月 10、2月 20 / B店: 1月 15、2月 25」の形式
    （系列の区切りは / ; 改行、系列名の後に : ）。 */
function extractChartDataset(request, ctx) {
  const jsonPrev = [...ctx.results].reverse()
    .find((r) => r.ok && r.toolId === "json-tool" && r.data !== undefined);
  if (jsonPrev) {
    const ds = jsonToDataset(jsonPrev.data);
    if (ds) return { ...ds, source: "JSON整形の結果" };
  }
  const quoted = (request.match(/[「『]([\s\S]+?)[」』]/) || [])[1];
  const cleaned = stripChartPhrases(quoted || request);
  const segs = cleaned.split(/[\n/;／]/).map((s) => s.trim()).filter(Boolean);
  const named = [];
  for (const seg of segs) {
    const m = seg.match(/^([^:：]{1,24})[:：]\s*(.+)$/s);
    if (!m) continue;
    const items = parsePairs(m[2]);
    if (items.length) named.push({ name: truncate(m[1].trim(), 12), items });
  }
  if (named.length >= 2) {   // 複数系列: ラベルは出現順の和集合、欠損は 0
    const labels = [];
    for (const s of named.slice(0, 8)) {
      for (const it of s.items) if (!labels.includes(it.label)) labels.push(it.label);
    }
    const use = labels.slice(0, 24);
    return {
      labels: use,
      series: named.slice(0, 8).map((s) => ({
        name: s.name,
        values: use.map((l) => s.items.find((it) => it.label === l)?.value ?? 0),
      })),
      source: "依頼文のデータ（複数系列）",
    };
  }
  const single = (named.length === 1 ? named[0].items : parsePairs(cleaned)).slice(0, 24);
  if (!single.length) return null;
  return {
    labels: single.map((d) => d.label),
    series: [{ name: named.length === 1 ? named[0].name : "値", values: single.map((d) => d.value) }],
    source: "依頼文のデータ",
  };
}

function svgTextW(s) {   // 凡例レイアウト用のざっくり文字幅（px, font-size 11）
  return [...String(s)].reduce((a, c) => a + (c.charCodeAt(0) < 256 ? 6.5 : 11), 0);
}

function renderChartSVG(type, ds) {
  const W = 560; const H = 340;
  const multi = ds.series.length > 1;
  const padT = multi && type !== "pie" ? 44 : 20;
  let legend = "";
  if (multi && type !== "pie") {
    let x = 14;
    ds.series.forEach((s, i) => {
      const name = truncate(s.name, 8);
      legend += `<rect x="${x}" y="12" width="11" height="11" rx="3" fill="${CHART_COLORS[i % CHART_COLORS.length]}"/>`;
      legend += `<text x="${x + 16}" y="22" font-size="11" fill="currentColor">${escapeHtml(name)}</text>`;
      x += 16 + svgTextW(name) + 16;
    });
  }
  const body = type === "pie" ? pieBody(ds, W, H, padT) : xyBody(type, ds, W, H, padT);
  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" font-family="inherit">${legend}${body}</svg>`;
}

function xyBody(type, ds, W, H, padT) {
  const padL = 56; const padR = 18; const padB = 44;
  const plotW = W - padL - padR; const plotH = H - padT - padB;
  const { labels, series } = ds;
  const n = labels.length;
  const stacked = type === "sbar";
  let minV = 0; let maxV = 0;
  if (stacked) {   // 積み上げは各ラベルの正負それぞれの合計がスケール
    labels.forEach((_, i) => {
      maxV = Math.max(maxV, series.reduce((a, s) => a + Math.max(s.values[i] || 0, 0), 0));
      minV = Math.min(minV, series.reduce((a, s) => a + Math.min(s.values[i] || 0, 0), 0));
    });
  } else {
    for (const s of series) for (const v of s.values) { maxV = Math.max(maxV, v); minV = Math.min(minV, v); }
  }
  const span = (maxV - minV) || 1;
  const y = (v) => padT + plotH - ((v - minV) / span) * plotH;
  const slot = plotW / n;
  const xs = labels.map((_, i) => type === "line"
    ? padL + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1))
    : padL + slot * i + slot / 2);
  let out = "";
  for (let i = 0; i <= 4; i++) {   // グリッド + 目盛
    const v = minV + (span * i) / 4;
    const yy = y(v);
    out += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="currentColor" stroke-opacity="${v === 0 ? 0.45 : 0.12}"/>`;
    out += `<text x="${padL - 8}" y="${yy + 4}" text-anchor="end" font-size="10" fill="currentColor" fill-opacity="0.65">${fmtNum(Math.round(v * 100) / 100)}</text>`;
  }
  const labelEvery = Math.ceil(n / 12);   // ラベルが密になりすぎたら間引く
  labels.forEach((label, i) => {
    if (i % labelEvery === 0) {
      out += `<text x="${xs[i]}" y="${H - padB + 16}" text-anchor="middle" font-size="10" fill="currentColor" fill-opacity="0.75">${escapeHtml(truncate(label, 6))}</text>`;
    }
  });
  const showValues = series.length === 1 || (!stacked && n * series.length <= 12);
  if (type === "line") {
    series.forEach((s, si) => {
      const col = CHART_COLORS[si % CHART_COLORS.length];
      const pts = labels.map((_, i) => [Math.round(xs[i] * 10) / 10, Math.round(y(s.values[i] || 0) * 10) / 10]);
      out += `<polyline points="${pts.map((p) => p.join(",")).join(" ")}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`;
      pts.forEach((p, i) => {
        out += `<circle cx="${p[0]}" cy="${p[1]}" r="${series.length > 1 ? 3.2 : 4}" fill="${series.length > 1 ? col : CHART_COLORS[i % CHART_COLORS.length]}"/>`;
        if (showValues) {
          out += `<text x="${p[0]}" y="${p[1] - 9}" text-anchor="middle" font-size="10" font-weight="700" fill="currentColor">${fmtNum(s.values[i] || 0)}</text>`;
        }
      });
    });
  } else if (stacked) {
    const barW = Math.min(slot * 0.62, 60);
    labels.forEach((_, i) => {
      let accPos = 0; let accNeg = 0;
      series.forEach((s, si) => {
        const v = s.values[i] || 0;
        if (!v) return;
        const col = CHART_COLORS[si % CHART_COLORS.length];
        let top; let bottom;
        if (v > 0) { top = y(accPos + v); bottom = y(accPos); accPos += v; }
        else { top = y(accNeg); bottom = y(accNeg + v); accNeg += v; }
        out += `<rect x="${xs[i] - barW / 2}" y="${top}" width="${barW}" height="${Math.max(bottom - top, 1)}" fill="${col}"/>`;
      });
      out += `<text x="${xs[i]}" y="${y(accPos) - 5}" text-anchor="middle" font-size="10" font-weight="700" fill="currentColor">${fmtNum(Math.round(accPos * 100) / 100)}</text>`;
    });
  } else {   // bar（単一系列）/ グループ棒（複数系列）
    const groupW = Math.min(slot * 0.72, 64 * series.length);
    const eachW = groupW / series.length;
    const barW = Math.max(eachW - 2, 3);
    labels.forEach((_, i) => {
      series.forEach((s, si) => {
        const v = s.values[i] || 0;
        const col = CHART_COLORS[(series.length > 1 ? si : i) % CHART_COLORS.length];
        const x = xs[i] - groupW / 2 + si * eachW + 1;
        const yv = y(v); const y0 = y(0);
        const top = Math.min(yv, y0); const h = Math.max(Math.abs(yv - y0), 1);
        out += `<rect x="${x}" y="${top}" width="${barW}" height="${h}" rx="3" fill="${col}"/>`;
        if (showValues) {
          out += `<text x="${x + barW / 2}" y="${top - 5}" text-anchor="middle" font-size="10" font-weight="700" fill="currentColor">${fmtNum(v)}</text>`;
        }
      });
    });
  }
  return out;
}

/** 表（テーブル）HTML を生成。foot は合計などの強調行 */
function buildTableHTML(headers, rows, foot) {
  const cell = (c, tag = "td") => {
    const num = typeof c === "number";
    const body = num ? fmtNum(c) : escapeHtml(String(c ?? ""));
    return `<${tag}${num ? ' class="num"' : ""}>${body}</${tag}>`;
  };
  const thead = `<thead><tr>${headers.map((h) => cell(h, "th")).join("")}</tr></thead>`;
  const tbody = `<tbody>${rows.map((r) => `<tr>${r.map((c) => cell(c)).join("")}</tr>`).join("")}</tbody>`;
  const tfoot = foot ? `<tfoot><tr>${foot.map((c) => cell(c)).join("")}</tr></tfoot>` : "";
  return `<figure class="table-box"><table>${thead}${tbody}${tfoot}</table></figure>`;
}

function pieBody(ds, W, H, padT) {
  // 円グラフは先頭の系列を表示する
  const items = ds.labels.map((label, i) => ({ label, value: ds.series[0].values[i] || 0 }));
  const pos = items.filter((d) => d.value > 0).slice(0, 10);
  if (!pos.length) return `<text x="20" y="60" fill="currentColor">正の値のデータがありません</text>`;
  const total = pos.reduce((a, d) => a + d.value, 0);
  const cx = 150; const cy = padT + (H - padT) / 2;
  const R = Math.min(105, (H - padT) / 2 - 12); const r = R * 0.55;
  let a0 = -Math.PI / 2; let out = "";
  pos.forEach((d, i) => {
    const frac = d.value / total;
    const a1 = a0 + frac * Math.PI * 2;
    const col = CHART_COLORS[i % CHART_COLORS.length];
    if (frac >= 0.999) {
      out += `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="${col}" stroke-width="${R - r}"/>`;
    } else {
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const p = (ang, rad) => `${Math.round((cx + rad * Math.cos(ang)) * 10) / 10} ${Math.round((cy + rad * Math.sin(ang)) * 10) / 10}`;
      out += `<path d="M ${p(a0, R)} A ${R} ${R} 0 ${large} 1 ${p(a1, R)} L ${p(a1, r)} A ${r} ${r} 0 ${large} 0 ${p(a0, r)} Z" fill="${col}"/>`;
    }
    a0 = a1;
  });
  pos.forEach((d, i) => {   // 凡例
    const ly = padT + 20 + i * 26;
    if (ly > H - 8) return;
    const pct = Math.round((d.value / total) * 1000) / 10;
    out += `<rect x="300" y="${ly - 10}" width="12" height="12" rx="3" fill="${CHART_COLORS[i % CHART_COLORS.length]}"/>`;
    out += `<text x="320" y="${ly}" font-size="11" fill="currentColor">${escapeHtml(truncate(d.label, 10))} — ${fmtNum(d.value)}（${pct}%）</text>`;
  });
  return out;
}

const BUILTIN_TOOLS = [
  {
    id: "calculator", name: "計算機", icon: "🧮", color: "#f59e0b", agents: ["analyst", "researcher"],
    desc: "四則演算・%・べき乗(^)の式を安全に評価する",
    run(input) {
      const expr = extractExpression(input) || normalizeExpr(input).trim();
      if (!/^[\d\s+\-*/%().]+$|^[\d\s+\-*/%().*]+$/.test(expr.replace(/\*\*/g, "*")) || !expr) {
        throw new Error("計算式を見つけられませんでした（使える記号: + - × ÷ % ^ かっこ）");
      }
      let value;
      try { value = Function('"use strict"; return (' + expr + ")")(); }
      catch { throw new Error("式の評価に失敗しました: " + expr); }
      if (typeof value !== "number" || !Number.isFinite(value)) {
        throw new Error("計算結果が数値になりませんでした");
      }
      return { text: `\`${expr.replace(/\*\*/g, "^")}\` = **${fmtNum(value)}**`, data: value };
    },
  },
  {
    id: "datetime", name: "日付・時刻", icon: "📅", color: "#0ea5e9", agents: ["analyst", "researcher"],
    desc: "今日の日付、◯日後/前、曜日、日付間の日数を計算する",
    run(input) {
      const now = new Date();
      const lines = [];
      const dates = parseDates(input);
      const rel = input.match(/(\d+)\s*(日|週間|か月|ヶ月|年)(後|前)/);
      if (rel) {
        const n = +rel[1] * (rel[3] === "前" ? -1 : 1);
        const base = dates[0] || now;
        const d = new Date(base);
        if (rel[2] === "日") d.setDate(d.getDate() + n);
        else if (rel[2] === "週間") d.setDate(d.getDate() + n * 7);
        else if (rel[2] === "年") d.setFullYear(d.getFullYear() + n);
        else d.setMonth(d.getMonth() + n);
        lines.push(`${fmtDate(base)} の ${rel[1]}${rel[2]}${rel[3]}は **${fmtDate(d)}** です`);
      } else if (dates.length >= 2) {
        const diff = Math.round(Math.abs(dates[1] - dates[0]) / 86400000);
        lines.push(`${fmtDate(dates[0])} と ${fmtDate(dates[1])} の間は **${fmtNum(diff)}日** です`);
      } else if (dates.length === 1) {
        const d = dates[0];
        const diff = Math.round((d - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
        const relTxt = diff === 0 ? "今日" : diff > 0 ? `今日から ${diff}日後` : `今日の ${-diff}日前`;
        lines.push(`**${fmtDate(d)}** — ${relTxt}です`);
      } else if (/明後日/.test(input)) {
        const d = new Date(now); d.setDate(d.getDate() + 2);
        lines.push(`明後日は **${fmtDate(d)}** です`);
      } else if (/明日/.test(input)) {
        const d = new Date(now); d.setDate(d.getDate() + 1);
        lines.push(`明日は **${fmtDate(d)}** です`);
      } else if (/昨日/.test(input)) {
        const d = new Date(now); d.setDate(d.getDate() - 1);
        lines.push(`昨日は **${fmtDate(d)}** でした`);
      } else {
        lines.push(`今日は **${fmtDate(now)}**、現在時刻は ${now.getHours()}時${String(now.getMinutes()).padStart(2, "0")}分です`);
      }
      return { text: lines.join("\n") };
    },
  },
  {
    id: "unit-convert", name: "単位変換", icon: "⚖️", color: "#84cc16", agents: ["analyst", "researcher"],
    desc: "長さ・重さ・温度・データ量・速度の単位を変換する",
    run(input) {
      const UNITS = {
        km: ["length", 1000], m: ["length", 1], cm: ["length", 0.01], mm: ["length", 0.001],
        mi: ["length", 1609.344], "マイル": ["length", 1609.344],
        ft: ["length", 0.3048], "フィート": ["length", 0.3048],
        in: ["length", 0.0254], "インチ": ["length", 0.0254],
        t: ["mass", 1000], "トン": ["mass", 1000], kg: ["mass", 1], g: ["mass", 0.001],
        lb: ["mass", 0.45359237], "ポンド": ["mass", 0.45359237], oz: ["mass", 0.0283495],
        tb: ["data", 1024 ** 4], gb: ["data", 1024 ** 3], mb: ["data", 1024 ** 2], kb: ["data", 1024], b: ["data", 1],
        "km/h": ["speed", 1], "キロ毎時": ["speed", 1], mph: ["speed", 1.609344], "m/s": ["speed", 3.6],
      };
      const norm = normalizeExpr(input).toLowerCase();
      const unitPat = "(km/h|m/s|mph|マイル|フィート|インチ|ポンド|トン|キロ毎時|mm|cm|km|mi|ft|in|kg|oz|lb|tb|gb|mb|kb|[mgtb℃℉])";
      const m = norm.match(new RegExp(`(-?\\d+(?:\\.\\d+)?)\\s*${unitPat}\\s*(?:を|から|は|\\s|=|→|to)+\\s*${unitPat}`, "i"));
      if (!m) throw new Error("「42.195km をマイルに」のような形式で書いてください");
      const [, numStr, fromU, toU] = m;
      const val = parseFloat(numStr);
      const tempConv = (v, f, t) => {
        const isC = (u) => u === "℃" || u === "c";
        const isF = (u) => u === "℉" || u === "f";
        if (isC(f) && isF(t)) return v * 9 / 5 + 32;
        if (isF(f) && isC(t)) return (v - 32) * 5 / 9;
        return null;
      };
      const tv = tempConv(val, fromU, toU);
      if (tv !== null) {
        return { text: `**${fmtNum(val)}${fromU} = ${fmtNum(Math.round(tv * 100) / 100)}${toU}**`, data: tv };
      }
      const from = UNITS[fromU]; const to = UNITS[toU];
      if (!from || !to || from[0] !== to[0]) {
        throw new Error(`「${fromU}」から「${toU}」への変換には対応していません`);
      }
      const result = (val * from[1]) / to[1];
      const rounded = Math.round(result * 1e6) / 1e6;
      return { text: `**${fmtNum(val)} ${fromU} = ${fmtNum(rounded)} ${toU}**`, data: result };
    },
  },
  {
    id: "text-stats", name: "テキスト統計", icon: "🔤", color: "#8b5cf6", agents: ["analyst", "writer"],
    desc: "文字数・行数・単語数・頻出語をカウントする",
    run(input, ctx) {
      const target = extractQuoted(ctx.request) || input;
      const chars = [...target].length;
      const noSpace = [...target.replace(/\s/g, "")].length;
      const linesN = target.split("\n").length;
      const words = (target.match(/[a-zA-Z0-9_]+|[一-龠ぁ-んァ-ヴー]+/g) || []);
      const freq = {};
      for (const w of words) if (w.length >= 2) freq[w] = (freq[w] || 0) + 1;
      const top = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 5)
        .map(([w, n]) => `\`${w}\`(${n})`).join(" ");
      return {
        text: [
          `対象: 「${truncate(target, 40)}」`,
          `- 文字数: **${fmtNum(chars)}**（空白除く: ${fmtNum(noSpace)}）`,
          `- 行数: ${fmtNum(linesN)} / 語数: ${fmtNum(words.length)}`,
          top ? `- 頻出語: ${top}` : "",
        ].filter(Boolean).join("\n"),
      };
    },
  },
  {
    id: "json-tool", name: "JSON 整形", icon: "🧩", color: "#f43f5e", agents: ["analyst", "researcher"],
    desc: "依頼に含まれる JSON を検証して整形表示する",
    run(input, ctx) {
      const raw = extractJson(ctx.request) || extractJson(input);
      if (!raw) throw new Error("JSON らしき部分が見つかりませんでした");
      let obj;
      try { obj = JSON.parse(raw); }
      catch (e) { throw new Error("JSON の構文エラー: " + e.message); }
      const pretty = JSON.stringify(obj, null, 2);
      const kind = Array.isArray(obj) ? `配列（${obj.length}件）` :
        typeof obj === "object" && obj ? `オブジェクト（キー ${Object.keys(obj).length}個）` : typeof obj;
      return { text: `✔ 正しい JSON です — ${kind}\n\`\`\`\n${pretty}\n\`\`\``, data: obj };
    },
  },
  {
    id: "web-fetch", name: "Web 取得", icon: "🌐", color: "#06b6d4", agents: ["researcher", "analyst"],
    desc: "URL のページを取得して本文テキストを抽出する（サーバー経由・SSRF対策つき）",
    async run(input) {
      const url = (input.match(/https?:\/\/[^\s、。」』)>"']+/) || [])[0];
      if (!url) throw new Error("URL が見つかりませんでした");
      const res = await fetch("/api/fetch?url=" + encodeURIComponent(url));
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const title = data.title || "(タイトルなし)";
      return {
        text: `**${title}**\n[${url}](${url}) から本文 ${fmtNum([...data.text].length)} 文字を取得しました${data.truncated ? "（長いため先頭のみ）" : ""}`,
        data,
      };
    },
  },
  {
    id: "summarize", name: "要約", icon: "📝", color: "#ec4899", agents: ["writer", "researcher"],
    desc: "取得したページや与えられた文章を要約する（LLM 接続時は LLM、未接続時は抽出型）",
    async run(input, ctx) {
      const fetched = [...ctx.results].reverse().find((r) => r.toolId === "web-fetch" && r.data?.text);
      const source = fetched ? fetched.data.text : (extractQuoted(ctx.request) || input);
      if (!source || source.length < 30) throw new Error("要約する対象のテキストが見つかりませんでした");
      const label = fetched ? `ページ「${fetched.data.title || fetched.data.url}」` : "与えられたテキスト";
      if (ctx.llmOn) {
        const res = await ctx.callLLM(
          `次のテキストを日本語で簡潔に要約してください。重要な点を箇条書き3〜6項目で。\n\n---\n${source.slice(0, 16000)}`,
          "あなたは要約の専門家です。事実だけを簡潔にまとめます。",
        );
        if (res.text) return { text: `${label}の要約（LLM）:\n\n${res.text}` };
        ctx.emit("note", "LLM 要約に失敗したため抽出型にフォールバックします");
      }
      return { text: `${label}の要約（抽出型）:\n\n${extractiveSummary(source)}` };
    },
  },
  {
    id: "memory", name: "メモリ", icon: "🗂️", color: "#14b8a6", agents: ["researcher", "writer"],
    desc: "「覚えて」で保存、「思い出して」で検索。ブラウザ内に永続化されるメモ帳",
    run(input, ctx) {
      const notes = LS.get("memory", []);
      const req = ctx.request;
      if (/(覚えて|記憶して|メモして|保存して)/.test(req)) {
        let body = extractQuoted(req) ||
          req.replace(/^.*?(覚えて|記憶して|メモして|保存して)[:：、\s]*/s, "").trim();
        if (/(結果|answer|それ)を?(メモ|保存|覚)/.test(req)) {
          const prev = [...ctx.results].reverse().find((r) => r.data !== undefined || r.text);
          if (prev) {
            const prevTxt = prev.text ? prev.text.replace(/[*`]/g, "") : String(prev.data);
            body = body && !/^(結果|それ)$/.test(body) ? `${body} → ${prevTxt}` : prevTxt;
          }
        }
        if (!body) throw new Error("保存する内容が見つかりませんでした（「覚えて: ◯◯」の形式で書いてください）");
        notes.push({ id: Date.now(), text: body, ts: new Date().toISOString() });
        LS.set("memory", notes);
        return { text: `🗂️ メモに保存しました（合計 ${notes.length} 件）:\n> ${truncate(body, 200)}` };
      }
      if (/(忘れて|全部?削除|メモをクリア)/.test(req)) {
        LS.set("memory", []);
        return { text: `🗂️ メモを全件削除しました（${notes.length} 件）` };
      }
      // 検索 / 一覧
      let kw = extractQuoted(req) ||
        req.replace(/(思い出して|メモを?(検索|一覧|見せて|表示|全部)|について|ください|して|全部|一覧)/g, " ")
          .replace(/[、。?？\s]+/g, " ").trim();
      if (kw.length < 2) kw = "";
      const hits = kw
        ? notes.filter((n) => n.text.toLowerCase().includes(kw.toLowerCase()))
        : notes;
      const list = (hits.length ? hits : notes).slice(-8).reverse();
      if (!notes.length) return { text: "🗂️ メモはまだ空です。「覚えて: ◯◯」で保存できます" };
      const bodyMd = list.map((n) =>
        `- ${truncate(n.text, 120)} — ${new Date(n.ts).toLocaleDateString("ja-JP")}`).join("\n");
      const head = kw && hits.length ? `「${kw}」に一致するメモ ${hits.length} 件:` :
        kw ? `「${kw}」に一致するメモはありません。最近のメモ:` : `保存済みメモ（全 ${notes.length} 件、最新から）:`;
      return { text: `🗂️ ${head}\n${bodyMd}` };
    },
  },
  {
    id: "random", name: "ランダム", icon: "🎲", color: "#f97316", agents: ["analyst", "researcher"],
    desc: "サイコロ・乱数・UUID 生成・選択肢からのランダム選択",
    run(input, ctx) {
      const req = ctx.request;
      if (/uuid/i.test(req)) {
        return { text: `UUID: \`${crypto.randomUUID()}\`` };
      }
      const dice = req.match(/サイコロ.*?(\d+)\s*(?:個|回)|(\d+)\s*(?:個|回).*?サイコロ|(\d+)d(\d+)/i);
      if (dice || /サイコロ|ダイス/.test(req)) {
        const n = Math.min(+(dice?.[1] || dice?.[2] || dice?.[3] || 1) || 1, 20);
        const faces = Math.min(+(dice?.[4] || 6) || 6, 1000);
        const rolls = Array.from({ length: n }, () => 1 + Math.floor(Math.random() * faces));
        const sum = rolls.reduce((a, b) => a + b, 0);
        return { text: `🎲 ${n}個の${faces}面サイコロ: **${rolls.join(", ")}**${n > 1 ? `（合計 ${sum}）` : ""}`, data: sum };
      }
      if (/(選んで|どれ|どっち)/.test(req)) {
        const src = extractQuoted(req) || req;
        const opts = src.split(/[、,\/]|か(?=[^ら])|または|それとも/)
          .map((s) => s.replace(/(選んで|どれ|どっち|から|で|？|\?|。)/g, "").trim())
          .filter((s) => s.length >= 1 && s.length <= 30);
        if (opts.length >= 2) {
          const pick = opts[Math.floor(Math.random() * opts.length)];
          return { text: `候補 ${opts.length} 件（${opts.join(" / ")}）から選びました → **${pick}**` };
        }
      }
      const range = req.match(/(\d+)\s*(?:〜|~|から)\s*(\d+)/);
      const lo = +(range?.[1] ?? 1), hi = +(range?.[2] ?? 100);
      const v = lo + Math.floor(Math.random() * (Math.max(hi, lo) - lo + 1));
      return { text: `🎲 ${lo}〜${Math.max(hi, lo)} の乱数: **${v}**`, data: v };
    },
  },
  {
    id: "chart", name: "図表作成", icon: "📊", color: "#a855f7", agents: ["analyst", "writer"],
    desc: "棒・グループ棒・積み上げ棒・折れ線・円グラフを SVG で作成。複数系列は「A店: 1月 10、2月 20 / B店: 1月 15、2月 25」",
    run(input, ctx) {
      const req = ctx.request;
      const ds = extractChartDataset(req, ctx);
      if (!ds || !ds.labels.length) {
        throw new Error("グラフにするデータが見つかりませんでした（例: 「A 10、B 20、C 30 を棒グラフにして」、複数系列は「A店: 1月 10、2月 20 / B店: 1月 15、2月 25」）");
      }
      const multi = ds.series.length > 1;
      const type = /積み上げ|積上げ|スタック/.test(req) ? "sbar"
        : /円グラフ|パイ|割合|比率|シェア/.test(req) ? "pie"
        : /折れ線|推移|トレンド|ライン/.test(req) ? "line" : "bar";
      const names = {
        bar: multi ? "グループ棒グラフ" : "棒グラフ",
        sbar: "積み上げ棒グラフ", line: "折れ線グラフ", pie: "円グラフ",
      };
      const notes = [`${ds.labels.length} 項目`];
      if (multi) notes.push(`${ds.series.length} 系列（${ds.series.map((s) => s.name).join(" / ")}）`);
      else {
        const sum = ds.series[0].values.reduce((a, v) => a + (v || 0), 0);
        notes.push(`合計 ${fmtNum(Math.round(sum * 100) / 100)}`);
      }
      if (type === "pie" && multi) notes.push(`円グラフは系列「${ds.series[0].name}」を表示`);
      return {
        text: `📊 **${names[type]}** を作成しました — ${notes.join("、")}（データ: ${ds.source}）`,
        html: `<figure class="chart-box">${renderChartSVG(type, ds)}</figure>`,
        data: ds,
      };
    },
  },
  {
    id: "table", name: "表作成", icon: "📋", color: "#38bdf8", agents: ["analyst", "writer"],
    desc: "データを表（テーブル）に整形。複数系列や JSON 整形の結果にも対応、数値列は合計行つき",
    run(input, ctx) {
      const jsonPrev = [...ctx.results].reverse()
        .find((r) => r.ok && r.toolId === "json-tool" && r.data !== undefined);
      // 1) オブジェクト配列の JSON → 汎用テーブル（キー = 列）
      if (jsonPrev && Array.isArray(jsonPrev.data)
          && jsonPrev.data.some((r) => r && typeof r === "object" && !Array.isArray(r))) {
        const rows = jsonPrev.data
          .filter((r) => r && typeof r === "object" && !Array.isArray(r)).slice(0, 50);
        const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))].slice(0, 8);
        const body = rows.map((r) => keys.map((k) => {
          const v = r[k];
          if (v === undefined || v === null) return "";
          return typeof v === "object" ? JSON.stringify(v) : v;
        }));
        return {
          text: `📋 **表** を作成しました — ${body.length} 行 × ${keys.length} 列（データ: JSON整形の結果）`,
          html: buildTableHTML(keys, body),
          data: { headers: keys, rows: body },
        };
      }
      // 2) 系列データ / 「ラベル 値」の列挙 → 項目×系列の表 + 合計行
      const ds = extractChartDataset(ctx.request, ctx);
      if (ds && ds.labels.length) {
        const headers = ["項目", ...ds.series.map((s) => s.name)];
        const body = ds.labels.map((l, i) => [l, ...ds.series.map((s) => s.values[i] ?? 0)]);
        const foot = ["合計", ...ds.series.map(
          (s) => Math.round(s.values.reduce((a, v) => a + (v || 0), 0) * 100) / 100)];
        return {
          text: `📋 **表** を作成しました — ${body.length} 行 × ${headers.length} 列 + 合計行（データ: ${ds.source}）`,
          html: buildTableHTML(headers, body, foot),
          data: { headers, rows: body, foot },
        };
      }
      // 3) 単純なオブジェクト JSON → キー / 値の2列
      if (jsonPrev && jsonPrev.data && typeof jsonPrev.data === "object" && !Array.isArray(jsonPrev.data)) {
        const body = Object.entries(jsonPrev.data).slice(0, 50)
          .map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : v]);
        return {
          text: `📋 **表** を作成しました — ${body.length} 行 × 2 列（データ: JSON整形の結果）`,
          html: buildTableHTML(["キー", "値"], body),
          data: { headers: ["キー", "値"], rows: body },
        };
      }
      throw new Error("表にするデータが見つかりませんでした（例: 「A 10、B 20、C 30 を表にして」）");
    },
  },
  {
    id: "llm-chat", name: "LLM 質問", icon: "✨", color: "#6366f1", agents: ["researcher", "writer"],
    desc: "設定した LLM に質問・生成を依頼する（設定画面で接続先を登録）",
    async run(input, ctx) {
      if (!ctx.llmOn) throw new Error("LLM が未設定です（⚙️ 設定から接続してください）");
      const prior = ctx.results
        .filter((r) => r.text)
        .map((r) => `【${r.toolName} の結果】\n${r.text.replace(/\*\*/g, "")}`)
        .join("\n\n");
      const prompt = prior
        ? `これまでのツール実行結果:\n${prior}\n\n上記を踏まえて、次の依頼に答えてください:\n${input}`
        : input;
      const res = await ctx.callLLM(prompt,
        "あなたは Kaleido Agents のリサーチャーです。簡潔かつ正確に日本語で答えます。Markdown 使用可。");
      if (res.error) throw new Error(res.error);
      return { text: res.text || "(空の応答)" };
    },
  },
];

/* ---- カスタムツール（localStorage 保存、new Function で実行） ---- */

function loadCustomTools() {
  return LS.get("customTools", []).map((def) => ({
    ...def,
    custom: true,
    agents: def.agents || (def.agent ? [def.agent] : ["analyst"]),
    color: def.color || "#a3a3a3",
    run: async (input, ctx) => {
      const fn = new AsyncFunction("input", "ctx", def.body);
      const out = await fn(input, ctx);
      if (out === undefined || out === null) return { text: "(出力なし)" };
      return typeof out === "object" && out.text !== undefined
        ? out
        : { text: typeof out === "string" ? out : "```\n" + JSON.stringify(out, null, 2) + "\n```", data: out };
    },
  }));
}

let TOOLS = [];
function rebuildToolRegistry() {
  TOOLS = [...BUILTIN_TOOLS, ...loadCustomTools()];
}
const toolById = (id) => TOOLS.find((t) => t.id === id);
/** ツールを担当できるエージェント候補（得意順） */
const toolAgents = (tool) => tool.agents || (tool.agent ? [tool.agent] : []);

/* 有効/無効の管理 */
const enabledTools = () => LS.get("toolsEnabled", {});
const enabledAgents = () => LS.get("agentsEnabled", {});
const isToolOn = (id) => enabledTools()[id] !== false;
const isAgentOn = (id) => enabledAgents()[id] !== false;

/* ---------------- 4. プランナー ---------------- */

/** ルールベースの計画立案。依頼文から「どのツールを使うか」のステップ列を作る。
    担当エージェントはここでは決めない（実行時に assignAgents が動的に決定）。 */
function planWithRules(request) {
  const steps = [];
  const add = (toolId, input, title) => {
    const tool = toolById(toolId);
    if (!tool || !isToolOn(toolId)) return;
    if (!toolAgents(tool).some((id) => isAgentOn(id))) return;   // 担当候補が全滅なら選ばない
    steps.push({ toolId, input, title: title || tool.name });
  };

  const urls = request.match(/https?:\/\/[^\s、。」』)>"']+/g) || [];
  for (const u of urls.slice(0, 3)) add("web-fetch", u, "ページ取得");

  // 「表にまとめて」「グラフにまとめて」は要約ではなく表/図表の依頼
  if (/(要約|サマリ|summariz)/i.test(request)
      || (/まとめて/.test(request) && !/(表|テーブル|グラフ|チャート|図)に?まとめ/.test(request))) {
    add("summarize", request, "要約");
  }

  const expr = extractExpression(request);
  if (expr && (/[+\-*/%^×÷]/.test(expr)) &&
      (/(計算|いくつ|いくら|求めて|=\s*\?|＝)/.test(request) || expr.length >= 5)) {
    add("calculator", request, "計算");
  }

  if (/(\d+\s*(日|週間|か月|ヶ月|年)(後|前))|何曜日|何日間?|日数|今日の日付|明日|明後日|昨日|今何時/.test(request)) {
    add("datetime", request, "日付計算");
  }

  if (/(変換|に直して|は何(km|m|kg|マイル|ポンド|フィート|インチ|℃|℉|gb|mb))/i.test(request) &&
      /\d/.test(request)) {
    add("unit-convert", request, "単位変換");
  }

  if (/(json|ｊｓｏｎ)/i.test(request) && extractJson(request)) add("json-tool", request, "JSON 整形");

  if (/(文字数|単語数|行数|カウント|何文字)/.test(request)) add("text-stats", request, "テキスト統計");

  if (/(サイコロ|ダイス|乱数|uuid|ランダム|選んで|どっちか|どれか)/i.test(request)) {
    add("random", request, "ランダム");
  }

  if (/(グラフ|チャート|図表|可視化|プロット|plot)/i.test(request)) {
    add("chart", request, "図表作成");
  }

  // 「図表」で誤起動しないよう、「表」は助詞つきの言い回しだけ拾う
  if (/(?<!図)表(にして|に整理|にまとめ|でまとめ|を作|形式)|テーブル|一覧表/.test(request)) {
    add("table", request, "表作成");
  }

  if (/(覚えて|記憶して|メモして|保存して|思い出して|メモを|忘れて)/.test(request)) {
    add("memory", request, "メモリ操作");
  }

  // カスタムツール: キーワード一致で起動
  for (const t of TOOLS.filter((t) => t.custom && isToolOn(t.id))) {
    const kws = (t.keywords || "").split(/[、,]/).map((s) => s.trim()).filter(Boolean);
    if (kws.some((k) => request.includes(k))) {
      add(t.id, request, t.name);
    }
  }
  return steps;
}

/** オーケストレータによる担当エージェントの動的割り当て。
    各ステップについて、ツールの担当候補（tool.agents、得意順）のうち
    有効なエージェントから「現在の計画内で負荷が最小」のものを選ぶ。
    LLM プランナーが指名したエージェントは、担当可能かつ有効なら尊重する。 */
function assignAgents(steps) {
  const load = {};
  AGENTS.forEach((a) => { load[a.id] = 0; });
  const out = [];
  const notes = [];
  for (const s of steps) {
    const tool = toolById(s.toolId);
    if (!tool) continue;
    const caps = toolAgents(tool).filter((id) => isAgentOn(id));
    if (!caps.length) {
      notes.push(`「${s.title}」は担当できるエージェントがすべて無効のためスキップ`);
      continue;
    }
    let chosen; let why;
    if (s.agentId && caps.includes(s.agentId)) {
      chosen = s.agentId;
      why = "LLMプランナーの指名";
    } else {
      chosen = caps.reduce((best, id) => (load[id] < load[best] ? id : best), caps[0]);
      why = caps.length > 1
        ? `候補 ${caps.map((id) => agentById(id).name).join("・")} から負荷最小を選択`
        : "唯一の担当候補";
    }
    load[chosen] += 1;
    notes.push(`「${s.title}」→ ${agentById(chosen).name} に割り当て（${why}）`);
    out.push({ ...s, agentId: chosen });
  }
  return { steps: out, notes };
}

/** LLM がいる場合は LLM に計画を作らせてみる（失敗時は null）。
    LLM はツールに加えて担当エージェントも指名できる。指名が不正なら
    未指名として扱い、assignAgents の動的割り当てに委ねる。 */
async function planWithLLM(request) {
  const available = TOOLS.filter(
    (t) => isToolOn(t.id) && toolAgents(t).some((id) => isAgentOn(id)),
  );
  if (!available.length) return null;
  const toolDocs = available.map(
    (t) => `- "${t.id}": ${t.desc}（担当可能: ${toolAgents(t).join(", ")}）`,
  ).join("\n");
  const agentDocs = AGENTS
    .filter((a) => !["planner", "reviewer"].includes(a.id) && isAgentOn(a.id))
    .map((a) => `- "${a.id}": ${a.name} — ${a.desc}`).join("\n");
  const res = await api("/api/llm", {
    system: "あなたはタスクプランナーです。JSON配列のみを返してください。説明文は不要です。",
    prompt: `ユーザーの依頼を、以下のエージェントとツールを使うステップに分解してください。
各ステップには、そのツールの「担当可能」に含まれるエージェントから最適な1人を割り当ててください。
使うべきツールがなければ "llm-chat" を1つ選んでください。最大4ステップ。

エージェント:
${agentDocs}

ツール:
${toolDocs}

依頼: ${request}

次の形式の JSON 配列のみを出力:
[{"agent": "エージェントid", "tool": "ツールid", "input": "そのツールに渡す入力", "title": "ステップ名(8文字以内)"}]`,
    max_tokens: 700,
  });
  if (res.error || !res.text) return null;
  try {
    const m = res.text.match(/\[[\s\S]*\]/);
    if (!m) return null;
    const arr = JSON.parse(m[0]);
    if (!Array.isArray(arr) || !arr.length) return null;
    const steps = [];
    for (const s of arr.slice(0, 4)) {
      const tool = toolById(String(s.tool));
      if (!tool || !isToolOn(tool.id)) continue;
      const aid = String(s.agent || "");
      steps.push({
        toolId: tool.id,
        // 指名が担当可能かつ有効なときだけ採用（それ以外は動的割り当てに委ねる）
        agentId: toolAgents(tool).includes(aid) && isAgentOn(aid) ? aid : undefined,
        input: String(s.input || request),
        title: truncate(String(s.title || tool.name), 12),
      });
    }
    return steps.length ? steps : null;
  } catch { return null; }
}

/* ---------------- 5. オーケストレータ（実行エンジン） ---------------- */

const state = {
  llm: { provider: "none", has_key: false },
  running: false,
  currentTraces: [],
};

const llmOn = () => state.llm.provider && state.llm.provider !== "none";

async function callLLM(prompt, system, max_tokens = 1200) {
  try { return await api("/api/llm", { prompt, system, max_tokens }); }
  catch (e) { return { error: String(e) }; }
}

async function orchestrate(request) {
  state.running = true;
  $("#btn-run").disabled = true;
  removeHero();
  appendUserMsg(request);

  const run = {
    id: "run" + Date.now(),
    request,
    t0: performance.now(),
    steps: [],
    traces: [],
    ok: true,
  };
  const card = appendRunCard(run);
  const emitLog = (agentId, text) => appendLog(card, agentId, text);
  clearInspector();

  try {
    /* --- 計画フェーズ --- */
    setAgentActive("planner", true);
    emitLog("orchestrator", "依頼を受理。プランナーに計画を依頼します");
    await sleep(350);

    let steps = null;
    let planSource = "ルールベース";
    if (llmOn() && isAgentOn("planner")) {
      emitLog("planner", "LLM で計画を立案中…");
      steps = await planWithLLM(request);
      if (steps) planSource = "LLM";
      else emitLog("planner", "LLM 計画に失敗 → ルールベースにフォールバック");
    }
    if (!steps) steps = planWithRules(request);

    if (!steps.length && llmOn() && isToolOn("llm-chat")) {
      steps = [{ toolId: "llm-chat", input: request, title: "LLM 回答" }];
    }
    // 動的割り当て: 担当エージェントは固定ではなく、候補・有効状態・負荷から
    // オーケストレータがここで決定する
    const assigned = assignAgents(steps);
    for (const note of assigned.notes) emitLog("orchestrator", note);
    steps = assigned.steps;

    // ライター/レビュアーの後工程を追加
    const pipeline = [...steps];
    if (isAgentOn("writer")) pipeline.push({ toolId: "_compose", agentId: "writer", input: "", title: "回答作成" });
    if (isAgentOn("reviewer")) pipeline.push({ toolId: "_review", agentId: "reviewer", input: "", title: "品質チェック" });

    run.steps = pipeline;
    renderPlanSteps(card, pipeline);
    emitLog("planner", `${planSource}で計画を作成 — 全 ${pipeline.length} ステップ`);
    setAgentActive("planner", false);
    await sleep(400);

    /* --- 実行フェーズ --- */
    const ctx = {
      request,
      results: [],
      llmOn: llmOn(),
      callLLM,
      emit: (type, text) => emitLog("orchestrator", text),
    };

    let finalMd = "";
    for (let i = 0; i < pipeline.length; i++) {
      const step = pipeline[i];
      const agent = agentById(step.agentId);
      setStepState(card, i, "running");
      setAgentActive(agent.id, true);
      const t0 = performance.now();

      try {
        if (step.toolId === "_compose") {
          emitLog("writer", "各ステップの結果を統合して回答を作成中…");
          finalMd = await composeAnswer(request, ctx);
        } else if (step.toolId === "_review") {
          emitLog("reviewer", "全ステップの結果を検査中…");
          const rv = reviewRun(ctx, run);
          finalMd += "\n\n---\n" + rv.text;
          if (!rv.ok) run.ok = false;
        } else {
          const tool = toolById(step.toolId);
          emitLog(agent.id, `${tool.icon} ${tool.name} を実行 — 入力: 「${truncate(step.input, 60)}」`);
          await sleep(250);
          const result = await tool.run(step.input, ctx);
          ctx.results.push({
            toolId: tool.id, toolName: tool.name, agentId: agent.id,
            text: result.text, data: result.data, html: result.html, ok: true,
          });
          emitLog(agent.id, `→ 完了: ${truncate(result.text.replace(/[*`\n#>]/g, " "), 90)}`);
          addTrace(run, { step, agent, tool, input: step.input, output: result.text, ms: performance.now() - t0, ok: true });
        }
        setStepState(card, i, "done");
      } catch (err) {
        const tool = toolById(step.toolId) || { id: step.toolId, name: step.title, icon: "⚠️" };
        ctx.results.push({ toolId: tool.id, toolName: tool.name, agentId: agent.id, text: String(err.message || err), ok: false });
        emitLog(agent.id, `⚠️ エラー: ${err.message || err}`);
        addTrace(run, { step, agent, tool, input: step.input, output: "ERROR: " + (err.message || err), ms: performance.now() - t0, ok: false });
        setStepState(card, i, "error");
        run.ok = false;
      }
      setAgentActive(agent.id, false);
      await sleep(300);
    }

    if (!finalMd) finalMd = fallbackAnswer(request, ctx);

    /* --- 完了 --- */
    // 図表などツールが生成したHTML（自前のSVG生成のみ）は回答の下に併載する
    const extraHtml = ctx.results.filter((r) => r.ok && r.html).map((r) => r.html).join("");
    const secs = ((performance.now() - run.t0) / 1000).toFixed(1);
    finishRunCard(card, run.ok, secs);
    appendFinalMsg(finalMd, extraHtml);
    saveHistory({
      q: request, ok: run.ok, ts: Date.now(), final: finalMd, secs,
      chart: extraHtml.slice(0, 60_000),
    });
    renderHistory();
  } catch (err) {
    finishRunCard(card, false, "—");
    appendFinalMsg("⚠️ 実行中に予期しないエラーが発生しました: " + escapeHtml(String(err.message || err)));
  } finally {
    AGENTS.forEach((a) => setAgentActive(a.id, false));
    state.running = false;
    $("#btn-run").disabled = false;
  }
}

/** ライター: ツール結果を最終回答にまとめる */
async function composeAnswer(request, ctx) {
  const okResults = ctx.results.filter((r) => r.ok);
  const ngResults = ctx.results.filter((r) => !r.ok);

  if (ctx.llmOn && okResults.length) {
    const material = okResults
      .map((r) => `【${r.toolName}】\n${r.text}`).join("\n\n");
    const res = await callLLM(
      `依頼: ${request}\n\nツール実行結果:\n${material}\n\n上記の結果を使って、依頼への最終回答を日本語のMarkdownで簡潔に書いてください。結果の数値・事実は改変しないこと。`,
      "あなたは Kaleido Agents のライターです。ツール結果を正確に、読みやすく整えます。",
    );
    if (res.text) return res.text;
  }
  return fallbackAnswer(request, ctx);
}

/** テンプレートによる回答合成（LLM なし・LLM 失敗時） */
function fallbackAnswer(request, ctx) {
  const ok = ctx.results.filter((r) => r.ok);
  const ng = ctx.results.filter((r) => !r.ok);
  const parts = [];
  if (!ok.length && !ng.length) {
    return [
      "この依頼に合うツールが見つかりませんでした。次のような依頼を処理できます:",
      "- 🧮 「1280 × (12 + 8) ÷ 4 を計算して」",
      "- 📅 「今日から100日後は何曜日？」",
      "- ⚖️ 「42.195km をマイルに変換」",
      "- 🌐 「https://example.com/ を取得して要約して」",
      "- 🗂️ 「覚えて: 定例会議は毎週金曜15時」→「メモを一覧して」",
      "- 🧩 JSON の整形、🔤 文字数カウント、🎲 サイコロ・ランダム選択",
      "",
      "⚙️ 設定から LLM を接続すると、自由な質問・要約・文章生成にも答えられるようになります。",
    ].join("\n");
  }
  if (ok.length === 1 && !ng.length) {
    parts.push(ok[0].text);
  } else {
    for (const r of ok) parts.push(`### ${toolById(r.toolId)?.icon || "🔧"} ${r.toolName}\n${r.text}`);
  }
  for (const r of ng) parts.push(`### ⚠️ ${r.toolName}（失敗）\n${r.text}`);
  return parts.join("\n\n");
}

/** レビュアー: 実行全体の検査 */
function reviewRun(ctx, run) {
  const total = ctx.results.length;
  const okN = ctx.results.filter((r) => r.ok).length;
  const toolNames = [...new Set(ctx.results.map((r) => r.toolName))].join("、") || "なし";
  const secs = ((performance.now() - run.t0) / 1000).toFixed(1);
  if (total === 0) {
    return { ok: true, text: `🟢 **レビュアー検査**: 実行ツールなし（利用案内を回答） · ${secs}秒` };
  }
  const allOk = okN === total;
  const icon = allOk ? "🟢" : okN > 0 ? "🟡" : "🔴";
  const verdict = allOk ? "すべてのステップが成功しました"
    : okN > 0 ? `${total - okN} 件のステップが失敗しました（結果は上記参照）`
    : "すべてのステップが失敗しました";
  return {
    ok: allOk,
    text: `${icon} **レビュアー検査**: ${verdict} — 成功 ${okN}/${total} · 使用ツール: ${toolNames} · ${secs}秒`,
  };
}

/* ---------------- 6. UI ---------------- */

/* ---- レジストリ（左パネル） ---- */

function renderAgents() {
  const box = $("#agent-list");
  box.innerHTML = "";
  for (const a of AGENTS) {
    const on = isAgentOn(a.id);
    const skills = TOOLS.filter((t) => toolAgents(t).includes(a.id)).map((t) => t.name);
    const card = document.createElement("div");
    card.className = "agent-card" + (on ? "" : " disabled");
    card.style.setProperty("--ac", a.color);
    card.dataset.agent = a.id;
    if (skills.length) card.title = `担当できるツール: ${skills.join("、")}`;
    card.innerHTML = `
      <div class="agent-avatar">${a.icon}</div>
      <div class="agent-info">
        <div class="agent-name">${escapeHtml(a.name)}</div>
        <div class="agent-desc">${escapeHtml(a.desc)}</div>
      </div>
      <div class="agent-status" data-status>待機</div>
      <label class="switch" title="有効/無効">
        <input type="checkbox" ${on ? "checked" : ""}>
        <span class="track"></span>
      </label>`;
    $("input", card).addEventListener("change", (e) => {
      const map = enabledAgents();
      map[a.id] = e.target.checked;
      LS.set("agentsEnabled", map);
      card.classList.toggle("disabled", !e.target.checked);
    });
    box.appendChild(card);
  }
}

function setAgentActive(agentId, active) {
  const card = $(`.agent-card[data-agent="${agentId}"]`);
  if (!card) return;
  card.classList.toggle("active", active);
  const st = $("[data-status]", card);
  if (st) st.innerHTML = active ? `<span class="pulse"></span>実行中` : "待機";
}

function renderTools() {
  const box = $("#tool-list");
  box.innerHTML = "";
  for (const t of TOOLS) {
    const on = isToolOn(t.id);
    const caps = toolAgents(t).map(agentById);
    const capNames = caps.map((a) => a.name).join("・");
    const capDots = caps.map((a) =>
      `<span class="cap-dot" style="background:${a.color}" title="${escapeHtml(a.name)}"></span>`).join("");
    const card = document.createElement("div");
    card.className = "tool-card" + (on ? "" : " disabled");
    card.style.setProperty("--tc", t.color);
    card.innerHTML = `
      <div class="tool-icon">${t.icon}</div>
      <div class="tool-info">
        <div class="tool-name">${escapeHtml(t.name)}
          <span class="cap-dots" title="担当候補: ${escapeHtml(capNames)}">${capDots}</span>
          ${t.custom ? '<span class="tool-badge">CUSTOM</span>' : ""}</div>
        <div class="tool-desc" title="${escapeHtml(t.desc)}">${escapeHtml(t.desc)}</div>
      </div>
      <div class="tool-actions">
        <button type="button" class="tool-btn" data-act="run" title="単体実行">▶</button>
        ${t.custom ? '<button type="button" class="tool-btn" data-act="del" title="削除">🗑</button>' : ""}
        <label class="switch" title="有効/無効（担当候補: ${escapeHtml(capNames)}）">
          <input type="checkbox" ${on ? "checked" : ""}>
          <span class="track"></span>
        </label>
      </div>`;
    $('input[type="checkbox"]', card).addEventListener("change", (e) => {
      const map = enabledTools();
      map[t.id] = e.target.checked;
      LS.set("toolsEnabled", map);
      card.classList.toggle("disabled", !e.target.checked);
    });
    $('[data-act="run"]', card).addEventListener("click", () => openRunToolDialog(t));
    const del = $('[data-act="del"]', card);
    if (del) del.addEventListener("click", () => {
      if (!confirm(`カスタムツール「${t.name}」を削除しますか？`)) return;
      LS.set("customTools", LS.get("customTools", []).filter((d) => d.id !== t.id));
      rebuildToolRegistry(); renderTools();
      toast(`「${t.name}」を削除しました`);
    });
    box.appendChild(card);
  }
}

/* ---- チャット / 実行カード ---- */

function removeHero() { $("#hero")?.remove(); }

function appendUserMsg(text) {
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.innerHTML = `<div class="bubble"></div>`;
  $(".bubble", el).textContent = text;
  $("#chat").appendChild(el);
  scrollChat();
}

function appendRunCard(run) {
  const el = document.createElement("div");
  el.className = "run-card";
  el.id = run.id;
  el.innerHTML = `
    <div class="run-head">
      <span class="spin" data-spin></span>
      <span data-state>オーケストレータ実行中…</span>
      <time>${new Date().toLocaleTimeString("ja-JP")}</time>
    </div>
    <div class="plan-steps" data-plan></div>
    <div class="run-log" data-log></div>
    <button type="button" class="run-toggle-log">ログを隠す ▲</button>`;
  $(".run-toggle-log", el).addEventListener("click", (e) => {
    const log = $("[data-log]", el);
    const hidden = log.style.display === "none";
    log.style.display = hidden ? "" : "none";
    e.target.textContent = hidden ? "ログを隠す ▲" : "ログを表示 ▼";
  });
  $("#chat").appendChild(el);
  scrollChat();
  return el;
}

function renderPlanSteps(card, steps) {
  const box = $("[data-plan]", card);
  box.innerHTML = "";
  steps.forEach((s, i) => {
    if (i > 0) {
      const arrow = document.createElement("span");
      arrow.className = "plan-arrow";
      arrow.textContent = "→";
      box.appendChild(arrow);
    }
    const agent = agentById(s.agentId);
    const tool = toolById(s.toolId);
    const el = document.createElement("span");
    el.className = "plan-step pending";
    el.dataset.step = i;
    el.style.setProperty("--sc", agent.color);
    el.innerHTML = `<span class="step-icon">${tool?.icon || agent.icon}</span>
      <span>${escapeHtml(s.title)}</span><span class="step-state">○</span>`;
    el.title = `${agent.name} / ${tool?.name || s.title}`;
    box.appendChild(el);
  });
}

function setStepState(card, index, stateName) {
  const el = $(`[data-step="${index}"]`, card);
  if (!el) return;
  el.classList.remove("pending", "running", "done", "error");
  el.classList.add(stateName);
  $(".step-state", el).textContent =
    stateName === "running" ? "…" : stateName === "done" ? "✓" : stateName === "error" ? "✗" : "○";
  scrollChat();
}

function appendLog(card, agentId, text) {
  const agent = agentById(agentId);
  const log = $("[data-log]", card);
  const line = document.createElement("div");
  line.className = "log-line";
  line.innerHTML = `<span class="log-agent" style="--lc:${agent.color}">${escapeHtml(agent.name)}</span>
    <span class="log-text">${escapeHtml(text)}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  scrollChat();
}

function finishRunCard(card, ok, secs) {
  $("[data-spin]", card)?.remove();
  const st = $("[data-state]", card);
  st.textContent = ok ? `完了 — ${secs}秒` : `完了（一部失敗）— ${secs}秒`;
  st.className = ok ? "state-ok" : "state-err";
  st.insertAdjacentText("afterbegin", ok ? "✓ " : "⚠ ");
}

function appendFinalMsg(md, extraHtml = "") {
  const el = document.createElement("div");
  el.className = "msg msg-final";
  el.innerHTML = `<div class="bubble"><div class="md">${mdToHtml(md)}</div>${extraHtml}</div>`;
  $("#chat").appendChild(el);
  scrollChat();
}

function scrollChat() {
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

/* ---- インスペクタ（右パネル） ---- */

function clearInspector() {
  state.currentTraces = [];
  $("#inspector").innerHTML = '<p class="placeholder">実行中…</p>';
}

function addTrace(run, trace) {
  state.currentTraces.push(trace);
  const box = $("#inspector");
  if ($(".placeholder", box)) box.innerHTML = "";
  const el = document.createElement("div");
  el.className = "trace-item" + (trace.ok ? "" : " open");
  el.style.setProperty("--tc", trace.agent.color);
  el.innerHTML = `
    <button type="button" class="trace-head">
      <span>${trace.tool.icon || "🔧"}</span>
      <span>${escapeHtml(trace.tool.name)}</span>
      <span>${trace.ok ? "✓" : "✗"}</span>
      <span class="trace-ms">${Math.round(trace.ms)}ms</span>
    </button>
    <div class="trace-body">
      <h4>INPUT — ${escapeHtml(trace.agent.name)}</h4>
      <pre>${escapeHtml(truncate(trace.input, 1200))}</pre>
      <h4>OUTPUT</h4>
      <pre>${escapeHtml(truncate(trace.output, 2400))}</pre>
    </div>`;
  $(".trace-head", el).addEventListener("click", () => el.classList.toggle("open"));
  box.appendChild(el);
}

/* ---- 履歴 ---- */

function saveHistory(item) {
  const hist = LS.get("history", []);
  hist.unshift(item);
  LS.set("history", hist.slice(0, 30));
}

function renderHistory() {
  const box = $("#history");
  const hist = LS.get("history", []);
  box.innerHTML = hist.length ? "" :
    '<p class="placeholder">まだ実行履歴はありません。</p>';
  for (const h of hist) {
    const el = document.createElement("div");
    el.className = "history-item";
    el.innerHTML = `
      <div class="history-q"></div>
      <div class="history-meta">
        <span class="${h.ok ? "ok" : "err"}">${h.ok ? "✓ 成功" : "⚠ 一部失敗"}</span>
        <span>${h.secs || "?"}秒</span>
        <span>${new Date(h.ts).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
      </div>`;
    $(".history-q", el).textContent = h.q;
    el.title = "クリックで結果を再表示";
    el.addEventListener("click", () => {
      removeHero();
      appendUserMsg(h.q + "（履歴から再表示）");
      appendFinalMsg(h.final || "(記録なし)", h.chart || "");
    });
    box.appendChild(el);
  }
}

/* ---- サンプルチップ ---- */

const SAMPLES = [
  "1280 × (12 + 8) ÷ 4 を計算して",
  "今日から100日後は何曜日？",
  "42.195km をマイルに変換して",
  "https://www.un.org/en/ を取得して要約して",
  "覚えて: 定例会議は毎週金曜 15:00",
  "メモを一覧して",
  "サイコロを3個振って",
  "1月 120、2月 150、3月 300、4月 210 を棒グラフにして",
  "A店: 1月 10、2月 25、3月 40 / B店: 1月 20、2月 15、3月 30 をグラフにして",
  "国内: Q1 40、Q2 55 / 海外: Q1 25、Q2 35 を積み上げグラフにして",
  "シェア: A社 45、B社 30、C社 25 を円グラフにして",
  "製品X 120、製品Y 80、製品Z 200 を表にして",
  "このJSONを整形して: {\"name\":\"kaleido\",\"tools\":[1,2,3]}",
  "カレー か 寿司 か ラーメン から選んで",
];

function renderSampleChips() {
  const box = $("#sample-chips");
  if (!box) return;
  for (const s of SAMPLES) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sample-chip";
    b.textContent = s;
    b.addEventListener("click", () => {
      $("#composer-input").value = s;
      $("#composer-input").focus();
    });
    box.appendChild(b);
  }
}

/* ---- 設定モーダル ---- */

const PROVIDER_HINTS = {
  none: "",
  local: "既定: http://localhost:11434/v1（Ollama）/ LM Studio は http://localhost:1234/v1",
  openai: "既定: https://api.openai.com/v1",
  anthropic: "既定: https://api.anthropic.com",
};
const PROVIDER_LABELS = { local: "ローカルLLM", openai: "OpenAI互換", anthropic: "Claude" };

function updateLlmBadge() {
  const badge = $("#llm-badge");
  if (llmOn()) {
    const label = PROVIDER_LABELS[state.llm.provider] || state.llm.provider;
    badge.textContent = `LLM: ${label}${state.llm.model ? " · " + state.llm.model : ""}`;
    badge.className = "llm-badge on";
  } else {
    badge.textContent = "LLM 未接続（ルールベースで動作中）";
    badge.className = "llm-badge off";
  }
}

async function loadConfig() {
  try {
    state.llm = await api("/api/config");
  } catch { state.llm = { provider: "none" }; }
  updateLlmBadge();
}

function setupSettingsDialog() {
  const dlg = $("#dlg-settings");
  $("#btn-settings").addEventListener("click", () => {
    $("#cfg-provider").value = state.llm.provider || "none";
    $("#cfg-base").value = state.llm.base_url || "";
    $("#cfg-model").value = state.llm.model || "";
    $("#cfg-key").value = "";
    $("#cfg-key-state").textContent = state.llm.has_key ? "（保存済み）" : "（未設定）";
    $("#cfg-base-hint").textContent = PROVIDER_HINTS[state.llm.provider] || "";
    $("#cfg-use-proxy").checked = state.llm.use_proxy !== false;
    $("#cfg-proxy-url").value = state.llm.proxy_url || "";
    $("#cfg-ca").value = state.llm.ca_bundle || "";
    $("#test-result").textContent = "";
    dlg.showModal();
  });
  $("#cfg-provider").addEventListener("change", (e) => {
    $("#cfg-base-hint").textContent = PROVIDER_HINTS[e.target.value] || "";
  });
  const readSettingsForm = () => ({
    provider: $("#cfg-provider").value,
    base_url: $("#cfg-base").value,
    model: $("#cfg-model").value,
    api_key: $("#cfg-key").value,
    use_proxy: $("#cfg-use-proxy").checked,
    proxy_url: $("#cfg-proxy-url").value,
    ca_bundle: $("#cfg-ca").value,
  });
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = readSettingsForm();
    try {
      state.llm = await api("/api/config", body);
      updateLlmBadge();
      toast("設定を保存しました");
      dlg.close();
    } catch { toast("設定の保存に失敗しました", true); }
  });
  $("#btn-test-llm").addEventListener("click", async () => {
    const out = $("#test-result");
    out.textContent = "テスト中…"; out.className = "test-result";
    // 現在のフォーム内容を一旦保存してからテスト
    state.llm = await api("/api/config", readSettingsForm());
    updateLlmBadge();
    const res = await api("/api/llm/test", {});
    if (res.text) { out.textContent = "✓ 接続OK: " + truncate(res.text, 40); out.className = "test-result ok"; }
    else { out.textContent = "✗ " + truncate(res.error || "失敗", 80); out.className = "test-result err"; }
  });
}

/* ---- カスタムツールモーダル ---- */

function setupToolDialog() {
  const dlg = $("#dlg-tool");
  const agentSel = $("#tool-agent");
  agentSel.innerHTML = AGENTS.filter((a) => a.id !== "planner")
    .map((a) => `<option value="${a.id}">${a.icon} ${a.name}</option>`).join("");
  $("#btn-add-tool").addEventListener("click", () => {
    $("#tool-form").reset();
    dlg.showModal();
  });
  $("#tool-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#tool-name").value.trim();
    const body = $("#tool-body").value;
    if (!name || !body.trim()) { toast("ツール名と関数本体は必須です", true); return; }
    try { new AsyncFunction("input", "ctx", body); }
    catch (err) { toast("構文エラー: " + err.message, true); return; }
    const defs = LS.get("customTools", []);
    defs.push({
      id: "custom-" + Date.now(),
      name,
      icon: $("#tool-icon").value.trim() || "🛠️",
      desc: $("#tool-desc").value.trim() || "カスタムツール",
      keywords: $("#tool-keywords").value.trim(),
      agent: $("#tool-agent").value,
      color: ["#8b5cf6", "#06b6d4", "#f59e0b", "#ec4899", "#22c55e", "#f97316"][defs.length % 6],
      body,
    });
    LS.set("customTools", defs);
    rebuildToolRegistry(); renderTools();
    toast(`カスタムツール「${name}」を登録しました`);
    dlg.close();
  });
}

/* ---- ツール単体実行モーダル ---- */

let runToolTarget = null;
function openRunToolDialog(tool) {
  runToolTarget = tool;
  $("#run-tool-title").textContent = `${tool.icon} ${tool.name} — 単体実行`;
  $("#run-tool-input").value = "";
  const out = $("#run-tool-output");
  out.hidden = true; out.textContent = "";
  $("#dlg-run-tool").showModal();
}

function setupRunToolDialog() {
  $("#run-tool-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!runToolTarget) return;
    const input = $("#run-tool-input").value;
    const out = $("#run-tool-output");
    out.hidden = false;
    out.textContent = "実行中…";
    const ctx = { request: input, results: [], llmOn: llmOn(), callLLM, emit: () => {} };
    try {
      const res = await runToolTarget.run(input, ctx);
      out.textContent = res.text + (res.data !== undefined ? `\n\n[data] ${JSON.stringify(res.data).slice(0, 500)}` : "");
    } catch (err) {
      out.textContent = "⚠️ " + (err.message || err);
    }
  });
}

/* ---- テーマ / コンポーザ / 初期化 ---- */

function setupTheme() {
  const saved = LS.get("theme", "dark");
  document.documentElement.dataset.theme = saved;
  $("#btn-theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    LS.set("theme", next);
  });
}

function setupComposer() {
  const form = $("#composer");
  const input = $("#composer-input");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || state.running) return;
    input.value = "";
    orchestrate(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
}

function setupDialogCloseButtons() {
  $$("[data-close]").forEach((btn) =>
    btn.addEventListener("click", () => btn.closest("dialog").close()));
}

function init() {
  rebuildToolRegistry();
  setupTheme();
  renderAgents();
  renderTools();
  renderSampleChips();
  renderHistory();
  setupComposer();
  setupSettingsDialog();
  setupToolDialog();
  setupRunToolDialog();
  setupDialogCloseButtons();
  $("#btn-clear-history").addEventListener("click", () => {
    LS.set("history", []);
    renderHistory();
  });
  loadConfig();
}

document.addEventListener("DOMContentLoaded", init);
