/* ノートのつながりを描く力学グラフ（Canvas・依存なし）。
 * ドラッグで移動、ホイールで拡大縮小、ノードをドラッグで動かす、クリックで開く。
 */
(function () {
  "use strict";
  const HUES = [162, 205, 32, 280, 350, 95, 245, 55];

  class ForceGraph {
    constructor(canvas, { onOpen, labels = "near" } = {}) {
      this.c = canvas; this.g = canvas.getContext("2d");
      this.onOpen = onOpen; this.labels = labels;
      this.nodes = []; this.edges = []; this.center = null;
      this.view = { x: 0, y: 0, k: 1 };
      this.alpha = 0; this.raf = 0; this.hover = null; this.folderHue = {};
      this.reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
      this._bind();
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(canvas);
    }
    destroy() { cancelAnimationFrame(this.raf); this._ro.disconnect(); this.c.onpointerdown = this.c.onwheel = null; }

    setData(data, center) {
      const prev = Object.fromEntries(this.nodes.map((n) => [n.id, n]));
      const W = this.c.clientWidth || 300, H = this.c.clientHeight || 300;
      this.center = center || null;
      this.nodes = data.nodes.map((n, i) => {
        const p = prev[n.id];
        const a = i * 2.39996, r = 20 + 9 * Math.sqrt(i);
        return Object.assign({ x: p ? p.x : Math.cos(a) * r, y: p ? p.y : Math.sin(a) * r, vx: 0, vy: 0 }, n);
      });
      const idx = Object.fromEntries(this.nodes.map((n, i) => [n.id, i]));
      this.edges = data.edges.map(([a, b]) => [idx[a], idx[b]]).filter(([a, b]) => a !== undefined && b !== undefined);
      this.nb = new Set();
      if (center) this.edges.forEach(([a, b]) => { if (this.nodes[a].id === center) this.nb.add(b); if (this.nodes[b].id === center) this.nb.add(a); });
      const folders = [...new Set(this.nodes.map((n) => n.folder.split("/")[0]).filter(Boolean))].sort();
      this.folderHue = Object.fromEntries(folders.map((f, i) => [f, HUES[i % HUES.length]]));
      if (!Object.keys(prev).length) this.view = { x: W / 2, y: H / 2, k: this.nodes.length > 80 ? 0.6 : 1 };
      this.alpha = 1; this.autoFit = true;
      if (this.reduce) { for (let i = 0; i < 300; i++) this.step(); this.fit(); this.draw(); }
      else this.kick();
    }

    resize() {
      const dpr = window.devicePixelRatio || 1, W = this.c.clientWidth, H = this.c.clientHeight;
      if (!W || !H) return;
      if (!this.view.x && !this.view.y) this.view = { x: W / 2, y: H / 2, k: this.view.k };
      this.c.width = W * dpr; this.c.height = H * dpr;
      this.g.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (this.autoFit) this.fit();
      this.draw();
    }

    kick() { if (!this.raf) this.raf = requestAnimationFrame(() => this.tick()); }
    tick() {
      this.raf = 0;
      if (this.alpha > 0.02) { this.step(); this.alpha *= 0.985; if (this.autoFit) this.fit(); this.draw(); this.kick(); }
      else this.draw();
    }

    /** 全ノードが収まるように表示位置と倍率を合わせる（利用者が動かすまで）。 */
    fit() {
      const W = this.c.clientWidth, H = this.c.clientHeight;
      if (!W || !H || !this.nodes.length) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of this.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
      const pad = 40;
      const k = Math.max(0.2, Math.min(1.6, (W - pad * 2) / Math.max(1, x1 - x0), (H - pad * 2) / Math.max(1, y1 - y0)));
      this.view = { k, x: W / 2 - ((x0 + x1) / 2) * k, y: H / 2 - ((y0 + y1) / 2) * k };
    }

    step() {
      const N = this.nodes, n = N.length, a = this.alpha;
      const rep = n > 150 ? 500 : 900;
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const p = N[i], q = N[j]; let dx = p.x - q.x, dy = p.y - q.y; let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.5; }
        if (d2 > 90000) continue;
        const f = (rep / d2) * a; p.vx += dx * f * 0.05; p.vy += dy * f * 0.05; q.vx -= dx * f * 0.05; q.vy -= dy * f * 0.05;
      }
      for (const [i, j] of this.edges) {
        const p = N[i], q = N[j]; const dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1, f = (d - 70) * 0.025 * a;
        p.vx += dx / d * f; p.vy += dy / d * f; q.vx -= dx / d * f; q.vy -= dy / d * f;
      }
      for (const p of N) {
        if (p === this.drag) continue;
        p.vx -= p.x * 0.006 * a; p.vy -= p.y * 0.006 * a;
        p.vx *= 0.8; p.vy *= 0.8; p.x += p.vx; p.y += p.vy;
      }
    }

    color(name) { return getComputedStyle(this.c).getPropertyValue(name).trim(); }
    nodeColor(n, i) {
      if (!n.exists) return null;
      if (n.id === this.center) return this.color("--accent");
      const hue = this.folderHue[n.folder.split("/")[0]];
      const light = document.documentElement.dataset.theme === "light";
      if (hue === undefined) return light ? "hsl(160 8% 50%)" : "hsl(160 8% 62%)";
      return light ? `hsl(${hue} 45% 42%)` : `hsl(${hue} 45% 64%)`;
    }

    draw() {
      const g = this.g, W = this.c.clientWidth, H = this.c.clientHeight, v = this.view;
      if (!W) return;
      g.clearRect(0, 0, W, H);
      g.save(); g.translate(v.x, v.y); g.scale(v.k, v.k);
      const line = this.color("--line"), acc = this.color("--accent"), muted = this.color("--muted"), ink = this.color("--ink"), bg = this.color("--bg");
      const focus = this.hover ?? (this.center ? this.nodes.findIndex((n) => n.id === this.center) : -1);
      for (const [i, j] of this.edges) {
        const p = this.nodes[i], q = this.nodes[j];
        const hot = focus >= 0 && (i === focus || j === focus);
        g.strokeStyle = hot ? acc : line; g.lineWidth = (hot ? 1.6 : 1) / Math.sqrt(v.k);
        g.globalAlpha = focus >= 0 && !hot ? 0.7 : 1;
        g.beginPath(); g.moveTo(p.x, p.y); g.lineTo(q.x, q.y); g.stroke();
      }
      g.globalAlpha = 1;
      g.font = `${11 / Math.sqrt(v.k)}px ${getComputedStyle(document.body).fontFamily}`;
      g.textAlign = "center";
      const nbs = new Set();
      if (focus >= 0) for (const [a, b] of this.edges) { if (a === focus) nbs.add(b); if (b === focus) nbs.add(a); }
      this.nodes.forEach((n, i) => {
        const r = 3.5 + Math.sqrt(n.degree) * 1.7; n.r = r;
        g.beginPath(); g.arc(n.x, n.y, r, 0, Math.PI * 2);
        const col = this.nodeColor(n, i);
        if (col) { g.fillStyle = col; g.fill(); } else { g.fillStyle = bg; g.fill(); g.strokeStyle = muted; g.lineWidth = 1; g.stroke(); }
        const neighbor = nbs.has(i);
        const show = this.labels === "all" || i === focus || neighbor || v.k > 1.3 || this.nodes.length <= 25;
        if (show) {
          g.fillStyle = i === focus ? ink : muted;
          const t = n.title.length > 16 ? n.title.slice(0, 15) + "…" : n.title;
          g.fillText(t, n.x, n.y + r + 12 / Math.sqrt(v.k));
        }
      });
      g.restore();
    }

    toWorld(e) { const r = this.c.getBoundingClientRect(); return { x: (e.clientX - r.left - this.view.x) / this.view.k, y: (e.clientY - r.top - this.view.y) / this.view.k }; }
    pick(e) {
      const p = this.toWorld(e); let best = -1, bd = Infinity;
      this.nodes.forEach((n, i) => { const d = Math.hypot(n.x - p.x, n.y - p.y); if (d < bd) { bd = d; best = i; } });
      return best >= 0 && bd < (this.nodes[best].r || 5) + 6 / this.view.k ? best : -1;
    }

    _bind() {
      const c = this.c;
      c.onpointerdown = (e) => {
        c.setPointerCapture(e.pointerId);
        const hit = this.pick(e); const start = { x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y }; let moved = false;
        if (hit >= 0) this.drag = this.nodes[hit];
        this.autoFit = false;
        c.style.cursor = "grabbing";
        c.onpointermove = (ev) => {
          if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 3) moved = true;
          if (this.drag) { const p = this.toWorld(ev); this.drag.x = p.x; this.drag.y = p.y; this.drag.vx = this.drag.vy = 0; this.alpha = Math.max(this.alpha, 0.3); this.kick(); }
          else { this.view.x = start.vx + ev.clientX - start.x; this.view.y = start.vy + ev.clientY - start.y; this.draw(); }
        };
        c.onpointerup = () => {
          c.onpointermove = null; c.style.cursor = "";
          if (!moved && hit >= 0 && this.onOpen) { const n = this.nodes[hit]; this.onOpen(n.exists ? n.id : null, n.title); }
          this.drag = null;
          c.onpointermove = (ev) => { const h = this.pick(ev); if (h !== (this.hover ?? -1)) { this.hover = h >= 0 ? h : null; c.style.cursor = h >= 0 ? "pointer" : ""; this.draw(); } };
        };
      };
      c.onpointermove = (ev) => { const h = this.pick(ev); if (h !== (this.hover ?? -1)) { this.hover = h >= 0 ? h : null; c.style.cursor = h >= 0 ? "pointer" : ""; this.draw(); } };
      c.onpointerleave = () => { if (this.hover !== null) { this.hover = null; this.draw(); } };
      c.onwheel = (e) => {
        e.preventDefault();
        this.autoFit = false;
        const r = c.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
        const k = Math.min(4, Math.max(0.2, this.view.k * Math.exp(-e.deltaY * 0.0015)));
        this.view.x = mx - (mx - this.view.x) * (k / this.view.k); this.view.y = my - (my - this.view.y) * (k / this.view.k); this.view.k = k;
        this.draw();
      };
    }
  }
  window.ForceGraph = ForceGraph;
})();
