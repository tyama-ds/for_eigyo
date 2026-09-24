/* Tensorium — Canvas チャート（依存なし）。
   線 / 散布図 / 棒 / ヒストグラム / ヒートマップ。DPR 対応・テーマ連動・ホバーツールチップ付き。 */
(function () {
  "use strict";
  const registry = new Map();     // canvas -> redraw()
  const tip = () => document.getElementById("tip");

  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const theme = () => ({
    grid: cssVar("--chart-grid"), axis: cssVar("--chart-axis"), ink: cssVar("--chart-ink"),
    muted: cssVar("--chart-muted"), surface: cssVar("--chart-surface"),
    seqLo: cssVar("--seq-lo"), seqHi: cssVar("--seq-hi"),
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => cssVar("--s" + i)),
    font: '12px "Inter", system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif',
  });

  function setup(canvas) {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(10, r.width), h = Math.max(10, r.height);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function niceTicks(min, max, n = 5) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { min -= 1; max += 1; }
    const span = max - min, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const cands = [1, 2, 2.5, 5, 10].map((c) => c * mag);
    const step = cands.find((c) => span / c <= n + 0.5) || cands[cands.length - 1];
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }
  function fmt(v) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    const a = Math.abs(v);
    if (a >= 1e6) return v.toLocaleString("ja-JP", { maximumFractionDigits: 0 });
    if (a >= 1000) return v.toLocaleString("ja-JP", { maximumFractionDigits: 1 });
    if (a >= 100) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    if (a === 0) return "0";
    if (a >= 0.01) return v.toFixed(3);
    return v.toExponential(2);
  }
  function fmtTick(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
    if (a >= 1000) return v.toLocaleString("ja-JP", { maximumFractionDigits: 0 });
    return +v.toFixed(6) + "";
  }

  function showTip(x, y, html) {
    const t = tip(); if (!t) return;
    t.innerHTML = html; t.classList.remove("hidden");
    const r = t.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > innerWidth - 8) left = x - r.width - 12;
    if (top + r.height > innerHeight - 8) top = y - r.height - 12;
    t.style.left = left + "px"; t.style.top = top + "px";
  }
  function hideTip() { const t = tip(); if (t) t.classList.add("hidden"); }

  function frame(ctx, w, h, pad, xr, yr, th, opts) {
    // 目盛り・グリッド・軸ラベル。戻り値は座標変換関数。
    if (opts.yLabel) pad.t = Math.max(pad.t, 26);
    const X = (v) => pad.l + (v - xr[0]) / (xr[1] - xr[0] || 1) * (w - pad.l - pad.r);
    const Y = (v) => h - pad.b - (v - yr[0]) / (yr[1] - yr[0] || 1) * (h - pad.t - pad.b);
    ctx.font = th.font; ctx.lineWidth = 1;
    const yt = niceTicks(yr[0], yr[1], 5);
    ctx.strokeStyle = th.grid; ctx.fillStyle = th.muted; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    yt.forEach((v) => { const y = Math.round(Y(v)) + 0.5; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); ctx.fillText(fmtTick(v), pad.l - 6, y); });
    if (!opts.noXTicks) {
      const xt = opts.xTicks || niceTicks(xr[0], xr[1], Math.max(3, Math.min(8, Math.floor((w - pad.l - pad.r) / 70))));
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      xt.forEach((v) => { const x = Math.round(X(v)) + 0.5; ctx.fillText(opts.xFmt ? opts.xFmt(v) : fmtTick(v), x, h - pad.b + 6); });
    }
    ctx.strokeStyle = th.axis; ctx.beginPath(); ctx.moveTo(pad.l, h - pad.b + 0.5); ctx.lineTo(w - pad.r, h - pad.b + 0.5); ctx.stroke();
    if (opts.xLabel) { ctx.fillStyle = th.muted; ctx.textAlign = "right"; ctx.textBaseline = "bottom"; ctx.fillText(opts.xLabel, w - pad.r, h - 2); }
    if (opts.yLabel) { ctx.fillStyle = th.muted; ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillText(opts.yLabel, 4, 4); }
    return { X, Y };
  }

  function bind(canvas, draw, onMove) {
    registry.set(canvas, draw);
    canvas.onmousemove = onMove ? (e) => onMove(e) : null;
    canvas.onmouseleave = hideTip;
    draw();
  }
  function extent(vals) { let lo = Infinity, hi = -Infinity; for (const v of vals) { if (v < lo) lo = v; if (v > hi) hi = v; } return [lo, hi]; }
  function padRange([lo, hi], f = 0.06) { if (!isFinite(lo)) return [0, 1]; if (lo === hi) return [lo - 1, hi + 1]; const d = (hi - lo) * f; return [lo - d, hi + d]; }

  /* ---------- 折れ線 ---------- */
  function line(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const series = (opts.series || []).filter((s) => s.points && s.points.length);
      const pad = { l: 52, r: 14, t: 14, b: 30 };
      if (!series.length) { ctx.fillStyle = th.muted; ctx.font = th.font; ctx.textAlign = "center"; ctx.fillText(opts.empty || "データなし", w / 2, h / 2); return; }
      const xs = [].concat(...series.map((s) => s.points.map((p) => p[0])));
      const ys = [].concat(...series.map((s) => s.points.map((p) => p[1]))).filter((v) => isFinite(v));
      let xr = extent(xs); if (xr[0] === xr[1]) xr = [xr[0] - 1, xr[1] + 1];
      let yr = padRange(extent(ys)); if (opts.yMin !== undefined) yr[0] = Math.min(opts.yMin, yr[0]); if (opts.yMax !== undefined) yr[1] = Math.max(opts.yMax, yr[1]);
      const { X, Y } = frame(ctx, w, h, pad, xr, yr, th, opts);
      series.forEach((s, i) => {
        const color = s.color || th.series[i % 8];
        ctx.globalAlpha = s.alpha ?? 1; ctx.strokeStyle = color; ctx.lineWidth = s.width || 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
        if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
        ctx.beginPath(); let started = false;
        s.points.forEach((p) => { if (!isFinite(p[1])) return; const x = X(p[0]), y = Y(p[1]); if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
        ctx.stroke(); ctx.setLineDash([]);
        if (s.dots || s.points.length < 40) {
          s.points.forEach((p) => { if (!isFinite(p[1])) return; ctx.beginPath(); ctx.arc(X(p[0]), Y(p[1]), 3.5, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill(); ctx.strokeStyle = th.surface; ctx.lineWidth = 2; ctx.stroke(); });
        }
        ctx.globalAlpha = 1;
      });
      canvas._geo = { X, Y, pad, w, h, series, th };
    };
    // ホバー時は再描画してからクロスヘアを重ねる
    bind(canvas, draw, (e) => { draw(); onMoveWrapped(e); });
    function onMoveWrapped(e) {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left;
      let best = null;
      g.series.forEach((s) => { if (s.noHover) return; s.points.forEach((p) => { const d = Math.abs(g.X(p[0]) - mx); if (!best || d < best.d) best = { d, x: p[0] }; }); });
      if (!best || best.d > 40) { hideTip(); return; }
      const ctx = canvas.getContext("2d"); const x = g.X(best.x);
      ctx.strokeStyle = g.th.axis; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, g.pad.t); ctx.lineTo(x, g.h - g.pad.b); ctx.stroke(); ctx.setLineDash([]);
      const rows = g.series.map((s, i) => {
        if (s.noHover) return "";
        let p = null; for (const q of s.points) { if (q[0] === best.x) { p = q; break; } }
        if (!p) return "";
        return `<div><i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${s.color || g.th.series[i % 8]};margin-right:6px"></i>${s.name}: <b>${fmt(p[1])}</b></div>`;
      }).join("");
      showTip(e.clientX, e.clientY, `<div class="dim">${opts.xLabel || "x"} = ${fmt(best.x)}</div>${rows}`);
    }
  }

  /* ---------- 散布図（実測 vs 予測） ---------- */
  function scatter(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const pts = opts.points || [];
      const pad = { l: 56, r: 16, t: 14, b: 32 };
      if (!pts.length) { ctx.fillStyle = th.muted; ctx.font = th.font; ctx.textAlign = "center"; ctx.fillText("データなし", w / 2, h / 2); return; }
      const all = [].concat(pts.map((p) => p[0]), pts.map((p) => p[1]));
      const r = padRange(extent(all));
      const { X, Y } = frame(ctx, w, h, pad, r, r, th, opts);
      ctx.strokeStyle = th.axis; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(X(r[0]), Y(r[0])); ctx.lineTo(X(r[1]), Y(r[1])); ctx.stroke(); ctx.setLineDash([]);
      const color = opts.color || th.series[0];
      const rad = pts.length > 800 ? 2.5 : pts.length > 200 ? 3.5 : 4.5;
      ctx.globalAlpha = pts.length > 500 ? 0.55 : 0.8;
      pts.forEach((p) => { ctx.beginPath(); ctx.arc(X(p[0]), Y(p[1]), rad, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill(); });
      ctx.globalAlpha = 1;
      canvas._geo = { X, Y, pts, rad };
    };
    bind(canvas, draw, (e) => {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
      let best = null;
      g.pts.forEach((p) => { const d = Math.hypot(g.X(p[0]) - mx, g.Y(p[1]) - my); if (d < 12 && (!best || d < best.d)) best = { d, p }; });
      if (!best) { hideTip(); return; }
      showTip(e.clientX, e.clientY, `<div>${opts.xLabel || "実測"}: <b>${fmt(best.p[0])}</b></div><div>${opts.yLabel || "予測"}: <b>${fmt(best.p[1])}</b></div><div class="dim">誤差 ${fmt(best.p[1] - best.p[0])}</div>`);
    });
  }

  /* ---------- 棒グラフ（カテゴリ） ---------- */
  function bars(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const labels = opts.labels || [], values = opts.values || [];
      if (!labels.length) { ctx.fillStyle = th.muted; ctx.font = th.font; ctx.textAlign = "center"; ctx.fillText("データなし", w / 2, h / 2); return; }
      const horizontal = opts.horizontal ?? labels.length > 8;
      const color = opts.color || th.series[0];
      const colors = opts.colors || null;
      ctx.font = th.font;
      if (horizontal) {
        const lw = Math.min(180, Math.max(...labels.map((l) => ctx.measureText(String(l)).width)) + 10);
        const pad = { l: lw + 8, r: 50, t: 8, b: 8 };
        const maxV = Math.max(0, ...values); const xr = [0, maxV || 1];
        const X = (v) => pad.l + v / xr[1] * (w - pad.l - pad.r);
        const bh = Math.min(22, (h - pad.t - pad.b) / labels.length - 3);
        const step = (h - pad.t - pad.b) / labels.length;
        ctx.strokeStyle = th.grid; niceTicks(0, xr[1], 4).forEach((v) => { const x = Math.round(X(v)) + 0.5; ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke(); });
        labels.forEach((lab, i) => {
          const y = pad.t + step * i + (step - bh) / 2;
          ctx.fillStyle = colors ? colors[i] : color; rr(ctx, pad.l, y, Math.max(0, X(values[i]) - pad.l), bh, 4, "right"); ctx.fill();
          ctx.fillStyle = th.ink; ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText(trunc(ctx, String(lab), lw), pad.l - 8, y + bh / 2);
          ctx.fillStyle = th.muted; ctx.textAlign = "left"; ctx.fillText(opts.fmt ? opts.fmt(values[i]) : fmt(values[i]), X(values[i]) + 6, y + bh / 2);
        });
        canvas._geo = { horizontal, pad, step, labels, values, X };
      } else {
        const pad = { l: 46, r: 12, t: 22, b: 40 };
        const maxV = Math.max(0, ...values); const yr = [0, (maxV || 1) * 1.08];
        const { X, Y } = frame(ctx, w, h, pad, [0, labels.length], yr, th, { noXTicks: true });
        const slot = (w - pad.l - pad.r) / labels.length, bw = Math.min(28, slot * 0.7);
        labels.forEach((lab, i) => {
          const x = pad.l + slot * i + (slot - bw) / 2, y = Y(values[i]);
          ctx.fillStyle = colors ? colors[i] : color; rr(ctx, x, y, bw, Math.max(0, h - pad.b - y), 4, "top"); ctx.fill();
          ctx.fillStyle = th.muted; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(trunc(ctx, String(lab), slot - 4), x + bw / 2, h - pad.b + 6);
          if (opts.valueLabels !== false && labels.length <= 12) { ctx.fillStyle = th.ink; ctx.textBaseline = "bottom"; ctx.fillText(opts.fmt ? opts.fmt(values[i]) : fmt(values[i]), x + bw / 2, y - 3); }
        });
        canvas._geo = { horizontal, pad, slot, labels, values, X, Y };
      }
    };
    bind(canvas, draw, (e) => {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
      const i = g.horizontal ? Math.floor((my - g.pad.t) / g.step) : Math.floor((mx - g.pad.l) / g.slot);
      if (i < 0 || i >= g.labels.length) { hideTip(); return; }
      showTip(e.clientX, e.clientY, `<div><b>${esc(String(g.labels[i]))}</b></div><div>${opts.fmt ? opts.fmt(g.values[i]) : fmt(g.values[i])}${opts.unit || ""}</div>`);
    });
  }

  /* ---------- 積み上げ棒（実データ + 合成データ） ---------- */
  function stacked(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const labels = opts.labels || [], series = (opts.series || []).filter((s) => s.values && s.values.length);
      if (!labels.length || !series.length) { ctx.fillStyle = th.muted; ctx.font = th.font; ctx.textAlign = "center"; ctx.fillText("データなし", w / 2, h / 2); return; }
      const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
      const horizontal = opts.horizontal ?? labels.length > 8;
      ctx.font = th.font;
      const refLine = opts.refLine;                       // 例: 最多クラスの件数
      if (horizontal) {
        const lw = Math.min(180, Math.max(...labels.map((l) => ctx.measureText(String(l)).width)) + 10);
        const pad = { l: lw + 8, r: 56, t: 8, b: 8 };
        const maxV = Math.max(refLine || 0, ...totals) || 1;
        const X = (v) => pad.l + v / maxV * (w - pad.l - pad.r);
        const step = (h - pad.t - pad.b) / labels.length, bh = Math.min(22, step - 3);
        ctx.strokeStyle = th.grid; niceTicks(0, maxV, 4).forEach((v) => { const x = Math.round(X(v)) + 0.5; ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke(); });
        labels.forEach((lab, i) => {
          const y = pad.t + step * i + (step - bh) / 2; let x0 = pad.l;
          series.forEach((s, si) => { const v = s.values[i] || 0; if (!v) return; const x1 = X(x0 === pad.l ? v : (x0 - pad.l) / (w - pad.l - pad.r) * maxV + v); const wv = Math.max(0, X(v) - pad.l);
            ctx.fillStyle = s.color; rr(ctx, x0 + (si ? 2 : 0), y, Math.max(0, wv - (si ? 2 : 0)), bh, si === series.length - 1 || !series.slice(si + 1).some((t) => t.values[i]) ? 4 : 0, "right"); ctx.fill(); x0 += wv; void x1; });
          ctx.fillStyle = th.ink; ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText(trunc(ctx, String(lab), lw), pad.l - 8, y + bh / 2);
          ctx.fillStyle = th.muted; ctx.textAlign = "left"; ctx.fillText(fmtTick(totals[i]), x0 + 6, y + bh / 2);
        });
        if (refLine) { const x = Math.round(X(refLine)) + 0.5; ctx.strokeStyle = th.axis; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke(); ctx.setLineDash([]); }
        canvas._geo = { horizontal, pad, step, labels, series, totals };
      } else {
        const pad = { l: 46, r: 12, t: 22, b: 40 };
        const maxV = Math.max(refLine || 0, ...totals) || 1;
        const { X, Y } = frame(ctx, w, h, pad, [0, labels.length], [0, maxV * 1.08], th, { noXTicks: true });
        const slot = (w - pad.l - pad.r) / labels.length, bw = Math.min(34, slot * 0.7);
        labels.forEach((lab, i) => {
          const x = pad.l + slot * i + (slot - bw) / 2; let base = h - pad.b;
          series.forEach((s, si) => { const v = s.values[i] || 0; if (!v) return; const top = Y((base === h - pad.b ? 0 : (h - pad.b - base) / (h - pad.t - pad.b) * maxV * 1.08) + v);
            const hh = Math.max(0, base - top); const last = !series.slice(si + 1).some((t) => t.values[i]);
            ctx.fillStyle = s.color; rr(ctx, x, top + (si ? 2 : 0), bw, Math.max(0, hh - (si ? 2 : 0)), last ? 4 : 0, "top"); ctx.fill(); base = top; });
          ctx.fillStyle = th.muted; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(trunc(ctx, String(lab), slot - 4), x + bw / 2, h - pad.b + 6);
          if (labels.length <= 12) { ctx.fillStyle = th.ink; ctx.textBaseline = "bottom"; ctx.fillText(fmtTick(totals[i]), x + bw / 2, base - 3); }
        });
        if (refLine) { const y = Math.round(Y(refLine)) + 0.5; ctx.strokeStyle = th.axis; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); ctx.setLineDash([]); }
        canvas._geo = { horizontal, pad, slot, labels, series, totals };
      }
    };
    bind(canvas, draw, (e) => {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
      const i = g.horizontal ? Math.floor((my - g.pad.t) / g.step) : Math.floor((mx - g.pad.l) / g.slot);
      if (i < 0 || i >= g.labels.length) { hideTip(); return; }
      const rows = g.series.map((s) => `<div><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${s.color};margin-right:6px"></i>${esc(s.name)}: <b>${(s.values[i] || 0).toLocaleString("ja-JP")}</b></div>`).join("");
      showTip(e.clientX, e.clientY, `<div><b>${esc(String(g.labels[i]))}</b> 合計 ${g.totals[i].toLocaleString("ja-JP")}</div>${rows}`);
    });
  }

  /* ---------- ヒストグラム ---------- */
  function hist(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const edges = opts.edges || [], counts = opts.counts || [];
      if (counts.length === 0) { ctx.fillStyle = th.muted; ctx.font = th.font; ctx.textAlign = "center"; ctx.fillText("データなし", w / 2, h / 2); return; }
      const pad = { l: 46, r: 14, t: 10, b: 30 };
      const xr = [edges[0], edges[edges.length - 1]]; if (xr[0] === xr[1]) { xr[0] -= 1; xr[1] += 1; }
      const yr = [0, Math.max(...counts) || 1];
      const { X, Y } = frame(ctx, w, h, pad, xr, yr, th, opts);
      const color = opts.color || th.series[0];
      counts.forEach((c, i) => {
        const x0 = X(edges[i]), x1 = X(edges[i + 1] ?? edges[i] + 1);
        const y = Y(c); ctx.fillStyle = color; rr(ctx, x0 + 1, y, Math.max(1, x1 - x0 - 2), Math.max(0, h - pad.b - y), 3, "top"); ctx.fill();
      });
      if (opts.zeroLine && xr[0] < 0 && xr[1] > 0) { ctx.strokeStyle = th.axis; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(X(0), pad.t); ctx.lineTo(X(0), h - pad.b); ctx.stroke(); ctx.setLineDash([]); }
      canvas._geo = { X, edges, counts, pad };
    };
    bind(canvas, draw, (e) => {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left;
      let i = -1; for (let k = 0; k < g.counts.length; k++) { if (mx >= g.X(g.edges[k]) && mx < g.X(g.edges[k + 1] ?? g.edges[k] + 1)) { i = k; break; } }
      if (i < 0) { hideTip(); return; }
      showTip(e.clientX, e.clientY, `<div>${fmt(g.edges[i])} 〜 ${fmt(g.edges[i + 1])}</div><div><b>${g.counts[i]}</b> 件</div>`);
    });
  }

  /* ---------- ヒートマップ（混同行列） ---------- */
  function heatmap(canvas, opts) {
    const draw = () => {
      const th = theme(); const { ctx, w, h } = setup(canvas);
      const m = opts.matrix || [], labels = opts.labels || [];
      const k = m.length; if (!k) return;
      ctx.font = th.font;
      const lw = Math.min(150, Math.max(...labels.map((l) => ctx.measureText(String(l)).width)) + 12);
      const pad = { l: lw + 44, r: 10, t: 40, b: 10 };
      const size = Math.min((w - pad.l - pad.r) / k, (h - pad.t - pad.b) / k);
      const ox = pad.l, oy = pad.t;
      const rowMax = m.map((r) => Math.max(1, ...r));
      const maxAll = Math.max(1, ...m.map((r) => Math.max(...r)));
      const lo = hexToRgb(th.seqLo), hi = hexToRgb(th.seqHi);
      for (let i = 0; i < k; i++) for (let j = 0; j < k; j++) {
        const v = m[i][j]; const t = opts.normalize === "row" ? v / rowMax[i] : v / maxAll;
        const c = mix(lo, hi, Math.pow(t, 0.7));
        ctx.fillStyle = `rgb(${c.join(",")})`;
        rr(ctx, ox + j * size + 1, oy + i * size + 1, size - 2, size - 2, 4); ctx.fill();
        if (size > 22) {
          const lum = (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255;
          ctx.fillStyle = lum > 0.6 ? "#111" : "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.font = (size > 40 ? 13 : 11) + 'px "Inter", system-ui, sans-serif'; ctx.fillText(String(v), ox + j * size + size / 2, oy + i * size + size / 2);
        }
      }
      ctx.font = th.font; ctx.fillStyle = th.ink;
      for (let i = 0; i < k; i++) { ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText(trunc(ctx, String(labels[i]), lw), ox - 8, oy + i * size + size / 2); }
      for (let j = 0; j < k; j++) { ctx.save(); ctx.translate(ox + j * size + size / 2, oy - 8); ctx.textAlign = "center"; ctx.textBaseline = "bottom"; ctx.fillText(trunc(ctx, String(labels[j]), size + 40), 0, 0); ctx.restore(); }
      ctx.fillStyle = th.muted; ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillText("予測 →", ox, 2);
      ctx.save(); ctx.translate(14, oy + (k * size) / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText("実際", 0, 0); ctx.restore();
      canvas._geo = { ox, oy, size, k, m, labels };
    };
    bind(canvas, draw, (e) => {
      const g = canvas._geo; if (!g) return;
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
      const j = Math.floor((mx - g.ox) / g.size), i = Math.floor((my - g.oy) / g.size);
      if (i < 0 || j < 0 || i >= g.k || j >= g.k) { hideTip(); return; }
      const rowSum = g.m[i].reduce((a, b) => a + b, 0) || 1;
      showTip(e.clientX, e.clientY, `<div>実際: <b>${esc(String(g.labels[i]))}</b></div><div>予測: <b>${esc(String(g.labels[j]))}</b></div><div>${g.m[i][j]} 件（実際クラス内 ${(100 * g.m[i][j] / rowSum).toFixed(1)}%）</div>`);
    });
  }

  /* ---------- ミニ棒（列カード用） ---------- */
  function spark(canvas, values, color) {
    const th = theme(); const { ctx, w, h } = setup(canvas);
    const n = values.length; if (!n) return;
    const maxV = Math.max(...values) || 1; const gap = n > 12 ? 1 : 2; const bw = (w - gap * (n - 1)) / n;
    values.forEach((v, i) => { const bh = Math.max(1, (v / maxV) * (h - 2)); ctx.fillStyle = color || th.series[0]; rr(ctx, i * (bw + gap), h - bh, bw, bh, 2, "top"); ctx.fill(); });
  }

  /* ---------- utils ---------- */
  function rr(ctx, x, y, w, h, r, side) {
    r = Math.min(r, w / 2, h / 2); if (r < 0) r = 0;
    ctx.beginPath();
    if (side === "top") { ctx.moveTo(x, y + h); ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r); ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h); }
    else if (side === "right") { ctx.moveTo(x, y); ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r); ctx.lineTo(x, y + h); }
    else { ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h); }
    ctx.closePath();
  }
  function trunc(ctx, s, maxW) { if (ctx.measureText(s).width <= maxW) return s; while (s.length > 1 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1); return s + "…"; }
  function hexToRgb(hex) { hex = hex.replace("#", ""); if (hex.length === 3) hex = hex.split("").map((c) => c + c).join(""); const n = parseInt(hex || "888888", 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function mix(a, b, t) { return [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t)); }
  function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  function redrawAll() { registry.forEach((fn, cv) => { if (cv.isConnected && cv.offsetParent !== null) fn(); }); }
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(redrawAll, 120); });

  window.Charts = { line, scatter, bars, stacked, hist, heatmap, spark, redrawAll, fmt, theme, hideTip, unbind: (cv) => registry.delete(cv) };
})();
