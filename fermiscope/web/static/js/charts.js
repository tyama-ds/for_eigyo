/* charts.js — 依存ゼロの軽量SVGチャート(FermiScope同梱)。
 * CDNが使えないローカル環境向けに、棒・ヒストグラム・トルネード・区間比較を描画する。
 * すべてDOM APIで構築し、ラベルはtextノードとして挿入する(XSS防止)。 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }
  function text(x, y, str, opts) {
    const t = el("text", Object.assign({ x, y, "font-size": 11, fill: "#111827" }, opts || {}));
    t.textContent = str;
    return t;
  }
  function fmt(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + "兆";
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "億";
    if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
    if (a >= 100) return Math.round(v).toLocaleString("ja-JP");
    if (a >= 1) return v.toFixed(2);
    return v.toPrecision(3);
  }

  /** 横棒グラフ items: [{label, value, color?, note?}] */
  function barChart(container, items, opts) {
    container.textContent = "";
    if (!items || !items.length) { container.textContent = "データなし"; return; }
    const o = Object.assign({ width: 460, rowH: 30, labelW: 150 }, opts || {});
    const height = items.length * o.rowH + 18;
    const svg = el("svg", { viewBox: `0 0 ${o.width} ${height}`, class: "chart-svg", role: "img", width: "100%" });
    const maxV = Math.max(...items.map((d) => Math.abs(d.value || 0)), 1e-12);
    const plotW = o.width - o.labelW - 90;
    items.forEach((d, i) => {
      const y = i * o.rowH + 8;
      svg.appendChild(text(o.labelW - 6, y + 13, d.label, { "text-anchor": "end" }));
      const w = Math.max((Math.abs(d.value || 0) / maxV) * plotW, 1.5);
      svg.appendChild(el("rect", {
        x: o.labelW, y, width: w, height: o.rowH - 12, rx: 3,
        fill: d.color || "#1d4ed8",
      }));
      svg.appendChild(text(o.labelW + w + 6, y + 13, fmt(d.value) + (d.note ? ` ${d.note}` : "")));
    });
    container.appendChild(svg);
  }

  /** ヒストグラム edges: n+1境界, counts: n個, markers: [{value,label}] */
  function histogram(container, edges, counts, markers) {
    container.textContent = "";
    if (!edges || edges.length < 2) { container.textContent = "データなし"; return; }
    const W = 460, H = 220, padL = 40, padB = 34, padT = 8;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", width: "100%" });
    const maxC = Math.max(...counts, 1);
    const x0 = edges[0], x1 = edges[edges.length - 1];
    const sx = (v) => padL + ((v - x0) / (x1 - x0 || 1)) * (W - padL - 10);
    const sy = (c) => H - padB - (c / maxC) * (H - padB - padT);
    counts.forEach((c, i) => {
      const bx = sx(edges[i]);
      const bw = Math.max(sx(edges[i + 1]) - bx - 0.6, 0.8);
      svg.appendChild(el("rect", { x: bx, y: sy(c), width: bw, height: H - padB - sy(c), fill: "#60a5fa" }));
    });
    svg.appendChild(el("line", { x1: padL, y1: H - padB, x2: W - 8, y2: H - padB, stroke: "#475569" }));
    [x0, (x0 + x1) / 2, x1].forEach((v) => {
      svg.appendChild(text(sx(v), H - padB + 14, fmt(v), { "text-anchor": "middle" }));
    });
    (markers || []).forEach((m, idx) => {
      const mx = sx(m.value);
      svg.appendChild(el("line", { x1: mx, y1: padT, x2: mx, y2: H - padB, stroke: "#dc2626", "stroke-dasharray": "4 3", "stroke-width": 1.5 }));
      svg.appendChild(text(mx + 3, padT + 12 + idx * 13, `${m.label} ${fmt(m.value)}`, { fill: "#991b1b", "font-size": 10 }));
    });
    container.appendChild(svg);
  }

  /** トルネード items: [{label, low, high, base}](low/highはそのパラメータ変動時の出力) */
  function tornado(container, items, base) {
    container.textContent = "";
    if (!items || !items.length) { container.textContent = "データなし"; return; }
    const W = 460, rowH = 32, labelW = 165;
    const H = items.length * rowH + 30;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", width: "100%" });
    const values = items.flatMap((d) => [d.low, d.high]).filter((v) => v !== null && v !== undefined);
    const minV = Math.min(...values, base), maxV = Math.max(...values, base);
    const plotW = W - labelW - 60;
    const sx = (v) => labelW + ((v - minV) / (maxV - minV || 1)) * plotW;
    items.forEach((d, i) => {
      const y = i * rowH + 10;
      svg.appendChild(text(labelW - 6, y + 12, d.label, { "text-anchor": "end" }));
      if (d.low === null || d.high === null) return;
      const a = Math.min(sx(d.low), sx(d.high));
      const b = Math.max(sx(d.low), sx(d.high));
      // 出力を下げる側と上げる側を塗り分け(パターン差もつける)
      svg.appendChild(el("rect", { x: a, y, width: Math.max(sx(base) - a, 0), height: rowH - 14, fill: "#f59e0b", rx: 2 }));
      svg.appendChild(el("rect", { x: sx(base), y, width: Math.max(b - sx(base), 0), height: rowH - 14, fill: "#1d4ed8", rx: 2 }));
      svg.appendChild(text(b + 5, y + 12, fmt(Math.abs(d.high - d.low)), { "font-size": 10, fill: "#475569" }));
    });
    const bx = sx(base);
    svg.appendChild(el("line", { x1: bx, y1: 4, x2: bx, y2: H - 22, stroke: "#111827", "stroke-width": 1.4 }));
    svg.appendChild(text(bx, H - 8, `基準 ${fmt(base)}`, { "text-anchor": "middle", "font-weight": 700 }));
    container.appendChild(svg);
  }

  /** 区間比較 items: [{label, low, high, central}] */
  function intervalChart(container, items) {
    container.textContent = "";
    if (!items || !items.length) { container.textContent = "データなし"; return; }
    const W = 460, rowH = 44, labelW = 130;
    const H = items.length * rowH + 26;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", width: "100%" });
    const values = items.flatMap((d) => [d.low, d.high, d.central]).filter((v) => v != null);
    const minV = Math.min(...values), maxV = Math.max(...values);
    const plotW = W - labelW - 70;
    const sx = (v) => labelW + ((v - minV) / (maxV - minV || 1)) * plotW;
    items.forEach((d, i) => {
      const y = i * rowH + 16;
      svg.appendChild(text(labelW - 6, y + 5, d.label, { "text-anchor": "end" }));
      if (d.low != null && d.high != null) {
        svg.appendChild(el("line", { x1: sx(d.low), y1: y, x2: sx(d.high), y2: y, stroke: "#1d4ed8", "stroke-width": 6, "stroke-linecap": "round", opacity: 0.35 }));
        svg.appendChild(text(sx(d.low), y + 18, fmt(d.low), { "text-anchor": "middle", "font-size": 9.5, fill: "#475569" }));
        svg.appendChild(text(sx(d.high), y + 18, fmt(d.high), { "text-anchor": "middle", "font-size": 9.5, fill: "#475569" }));
      }
      if (d.central != null) {
        svg.appendChild(el("circle", { cx: sx(d.central), cy: y, r: 5.5, fill: "#1d4ed8" }));
        svg.appendChild(text(sx(d.central), y - 9, fmt(d.central), { "text-anchor": "middle", "font-weight": 700 }));
      }
    });
    container.appendChild(svg);
  }

  window.FermiCharts = { barChart, histogram, tornado, intervalChart, fmt };
})();
