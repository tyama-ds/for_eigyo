/* GraphView — 依存なしの Canvas ナレッジグラフビューア（rag-orchestrator / doc-vault 共通）
 *
 * 旧実装（固定サイズ canvas に全ノードを押し込む力学レイアウト）は、ノードが数百になると
 * 画面端に張り付き、ラベルが重なって読めなくなっていた。本実装では:
 *   - ズーム（ホイール / ボタン）・パン（背景ドラッグ）・全体表示（fit）
 *   - コミュニティごとに初期配置してから力学レイアウト（収束後に自動 fit）
 *   - ラベルは「次数上位 + ズームで大きく見えるノード + 選択/ホバー/検索ヒット」だけ描く
 *   - クリックで選択 → 隣接ノードを強調・他を減光、onSelect で詳細表示
 *   - 孤立ノード（関係なし）の表示切替、色分け（コミュニティ / 種別）
 *   - HiDPI（devicePixelRatio）対応、コンテナ幅への追従
 *
 * 使い方:
 *   const gv = new GraphView(canvas, { tooltip, onSelect: (node|null) => {} });
 *   gv.setData({ nodes, edges, communities });
 *   gv.fit(); gv.zoomBy(1.2); gv.focus(id); gv.search("文字列"); gv.setColorMode("type");
 *   gv.setShowIsolated(true); gv.highlightCommunity(cid);
 *
 * nodes: [{id, name, type, degree, community, description, docs?}]
 * edges: [{source, target, strength(1-10), description}]
 */
"use strict";

(function (global) {
  const PALETTE = ["#a78bfa", "#5aa2ff", "#34d399", "#f59e0b", "#f472b6", "#22d3ee",
    "#fb7185", "#a3e635", "#facc15", "#c084fc", "#4ade80", "#60a5fa", "#fda4af",
    "#2dd4bf", "#fbbf24", "#818cf8"];
  const GREY = "#6b7794";

  function hashColor(key) {
    let h = 0;
    const s = String(key ?? "");
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return PALETTE[Math.abs(h) % PALETTE.length];
  }

  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  class GraphView {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.tooltip = opts.tooltip || null;
      this.onSelect = opts.onSelect || (() => {});
      this.height = opts.height || 620;
      this.colorMode = "community";
      this.showIsolated = false;
      this.all = { nodes: [], edges: [], communities: [] };
      this.nodes = []; this.edges = [];
      this.byId = new Map();
      this.adj = new Map();
      this.view = { k: 1, x: 0, y: 0 };
      this.selected = null; this.hover = null;
      this.matches = new Set();
      this.highlightCom = null;
      this.alpha = 0; this.raf = null;
      this.drag = null;            // {type:"node"|"pan", node, sx, sy, moved}
      this.dpr = 1; this.W = 800; this.H = this.height;
      this._colorOf = new Map();
      this._bind();
      this.resize();
      if (global.ResizeObserver) {
        this._ro = new ResizeObserver(() => { this.resize(); this.draw(); });
        this._ro.observe(canvas.parentElement || canvas);
      } else {
        global.addEventListener("resize", () => { this.resize(); this.draw(); });
      }
    }

    // ------------------------------------------------------------ data
    setData(data) {
      this.all = {
        nodes: (data && data.nodes) || [],
        edges: (data && data.edges) || [],
        communities: (data && data.communities) || [],
      };
      this.selected = null; this.hover = null; this.matches = new Set();
      this.highlightCom = null;
      this._rebuild(true);
    }

    setShowIsolated(flag) {
      this.showIsolated = !!flag;
      this._rebuild(false);
    }

    setColorMode(mode) {
      this.colorMode = mode === "type" ? "type" : "community";
      this._assignColors();
      this.draw();
    }

    legend() {
      /* 現在の色分けの凡例 [{label, color, count}]（多い順・上位12件） */
      const counts = new Map();
      for (const n of this.nodes) {
        const key = this.colorMode === "type" ? (n.type || "その他") : (n.community || "-");
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      const titleOf = new Map(this.all.communities.map((c) => [c.id, c.title]));
      return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([k, c]) => ({
        key: k,
        label: this.colorMode === "type" ? k : `${k}${titleOf.get(k) ? " " + titleOf.get(k) : ""}`,
        color: this._colorFor(k), count: c,
      }));
    }

    stats() {
      const isolated = this.all.nodes.filter((n) => !this._degree(n.id)).length;
      return { shown: this.nodes.length, total: this.all.nodes.length,
        edges: this.edges.length, isolated };
    }

    _degree(id) { return (this.adj.get(id) || new Set()).size; }

    _rebuild(resetLayout) {
      // 隣接（全体）
      this.adj = new Map();
      const ids = new Set(this.all.nodes.map((n) => n.id));
      const validEdges = this.all.edges.filter((e) =>
        e.source !== e.target && ids.has(e.source) && ids.has(e.target));
      for (const e of validEdges) {
        if (!this.adj.has(e.source)) this.adj.set(e.source, new Set());
        if (!this.adj.has(e.target)) this.adj.set(e.target, new Set());
        this.adj.get(e.source).add(e.target);
        this.adj.get(e.target).add(e.source);
      }
      const prev = this.byId;
      const keep = this.all.nodes.filter((n) => this.showIsolated || this._degree(n.id) > 0);
      // 関係が1件も無いグラフでは孤立ノードも出さないと何も見えないので全件表示
      const nodesIn = keep.length ? keep : this.all.nodes;
      this.nodes = nodesIn.map((n) => {
        const old = prev.get(n.id);
        return {
          ...n, x: old ? old.x : 0, y: old ? old.y : 0, vx: 0, vy: 0, fixed: false,
          deg: this._degree(n.id),
          r: 4 + Math.min(14, Math.sqrt(this._degree(n.id) + (n.degree || 0)) * 2.2),
        };
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.edges = validEdges
        .filter((e) => this.byId.has(e.source) && this.byId.has(e.target))
        .map((e) => ({ ...e, a: this.byId.get(e.source), b: this.byId.get(e.target) }));
      // ラベルを常時出す上位ノード（次数順）
      const ranked = [...this.nodes].sort((a, b) => b.deg - a.deg || b.r - a.r);
      this.topLabel = new Set(ranked.slice(0, Math.max(12, Math.round(Math.sqrt(this.nodes.length) * 2)))
        .map((n) => n.id));
      this._assignColors();
      if (resetLayout || this.nodes.some((n) => !prev.has(n.id))) this._seedLayout(!resetLayout);
      this._start();
    }

    _colorFor(key) {
      if (!this._colorOf.has(key)) this._colorOf.set(key, PALETTE[this._colorOf.size % PALETTE.length]);
      return this._colorOf.get(key);
    }

    _assignColors() {
      this._colorOf = new Map();
      // 大きいグループから順に色を割り当てる（上位が見分けやすい色になる）
      const counts = new Map();
      for (const n of this.nodes) {
        const key = this.colorMode === "type" ? (n.type || "その他") : (n.community || "-");
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      [...counts.entries()].sort((a, b) => b[1] - a[1]).forEach(([k], i) => {
        this._colorOf.set(k, i < PALETTE.length ? PALETTE[i] : hashColor(k));
      });
      for (const n of this.nodes) {
        const key = this.colorMode === "type" ? (n.type || "その他") : (n.community || "-");
        n.color = this._colorOf.get(key) || GREY;
      }
    }

    _seedLayout(onlyNew) {
      /* コミュニティ単位でひまわり配置 → メンバーをその周囲に配置 */
      const groups = new Map();
      for (const n of this.nodes) {
        const key = n.community || `_${n.id}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(n);
      }
      const order = [...groups.values()].sort((a, b) => b.length - a.length);
      const golden = Math.PI * (3 - Math.sqrt(5));
      let placed = 0;
      order.forEach((members, gi) => {
        const R = 60 * Math.sqrt(gi + 0.5) * 2.2;
        const cx = Math.cos(gi * golden) * R, cy = Math.sin(gi * golden) * R;
        const spread = 14 * Math.sqrt(members.length);
        members.forEach((n, i) => {
          if (onlyNew && (n.x || n.y)) return;
          const rr = spread * Math.sqrt((i + 0.5) / members.length);
          n.x = cx + Math.cos(i * golden) * rr;
          n.y = cy + Math.sin(i * golden) * rr;
          placed++;
        });
      });
      return placed;
    }

    // ------------------------------------------------------------ simulation
    _start() {
      this.alpha = 1;
      this._userMoved = false;           // ユーザーがズーム/パンするまではレイアウトに合わせて自動 fit
      cancelAnimationFrame(this.raf);
      this._loop();
    }

    _loop() {
      cancelAnimationFrame(this.raf);
      const step = () => {
        const perFrame = this.nodes.length > 400 ? 2 : 4;
        for (let i = 0; i < perFrame && this.alpha > 0.02; i++) this._tick();
        if (!this._userMoved) this._fitView();
        this.draw();
        if (this.alpha > 0.02 || this.drag) this.raf = requestAnimationFrame(step);
        else this.raf = null;
      };
      step();
    }

    _reheat(a = 0.3) {
      this.alpha = Math.max(this.alpha, a);
      if (!this.raf) this._loop();
    }

    _tick() {
      const nodes = this.nodes, n = nodes.length;
      if (!n) { this.alpha = 0; return; }
      const alpha = this.alpha;
      // 反発（クーロン型、近距離のみ強く。O(n^2) だが 1000 ノード程度まで実用）
      const rep = 1400;
      for (let i = 0; i < n; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < n; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 > 250000) continue;               // 500px 以上は無視（遠方の微小力を省く）
          if (d2 < 0.01) { dx = (i - j) * 0.01; dy = 0.01; d2 = dx * dx + dy * dy; }
          const f = (rep * alpha) / d2;
          const d = Math.sqrt(d2);
          const fx = (dx / d) * f, fy = (dy / d) * f;
          a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
          const minD = a.r + b.r + 4;              // 衝突（重なり防止）
          if (d < minD) {
            const push = (minD - d) * 0.25;
            a.vx += (dx / d) * push; a.vy += (dy / d) * push;
            b.vx -= (dx / d) * push; b.vy -= (dy / d) * push;
          }
        }
      }
      // ばね（エッジ）
      for (const e of this.edges) {
        const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
        const d = Math.max(0.01, Math.sqrt(dx * dx + dy * dy));
        const target = 50 + (e.a.r + e.b.r);
        const k = 0.05 * (0.6 + (e.strength || 5) / 25);
        const f = (d - target) * k * alpha;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        e.a.vx += fx; e.a.vy += fy; e.b.vx -= fx; e.b.vy -= fy;
      }
      // 原点への弱い引力（孤立成分が無限に離れないように）
      for (const p of nodes) {
        p.vx -= p.x * 0.004 * alpha;
        p.vy -= p.y * 0.004 * alpha;
        if (p.fixed) { p.vx = 0; p.vy = 0; continue; }
        p.vx *= 0.6; p.vy *= 0.6;
        const sp = Math.hypot(p.vx, p.vy);
        if (sp > 30) { p.vx *= 30 / sp; p.vy *= 30 / sp; }
        p.x += p.vx; p.y += p.vy;
      }
      this.alpha *= 0.985;
    }

    // ------------------------------------------------------------ view
    resize() {
      const parent = this.canvas.parentElement;
      const w = Math.max(320, Math.floor((parent ? parent.clientWidth : 800) || 800));
      const h = this.height;
      this.dpr = global.devicePixelRatio || 1;
      this.W = w; this.H = h;
      this.canvas.style.width = "100%";
      this.canvas.style.height = `${h}px`;
      this.canvas.width = Math.floor(w * this.dpr);
      this.canvas.height = Math.floor(h * this.dpr);
    }

    fit() {
      this._userMoved = false;
      this._fitView();
      this.draw();
    }

    _fitView() {
      if (!this.nodes.length) { this.view = { k: 1, x: this.W / 2, y: this.H / 2 }; return; }
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of this.nodes) {
        x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
        x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
      }
      const pad = 40;
      const k = Math.min(2, Math.max(0.05, Math.min(
        (this.W - pad * 2) / Math.max(1, x1 - x0), (this.H - pad * 2) / Math.max(1, y1 - y0))));
      this.view = { k, x: this.W / 2 - k * (x0 + x1) / 2, y: this.H / 2 - k * (y0 + y1) / 2 };
    }

    zoomBy(f, cx = this.W / 2, cy = this.H / 2) {
      this._userMoved = true;
      const v = this.view;
      const k = Math.min(8, Math.max(0.03, v.k * f));
      const wx = (cx - v.x) / v.k, wy = (cy - v.y) / v.k;
      this.view = { k, x: cx - wx * k, y: cy - wy * k };
      this.draw();
    }

    focus(id) {
      const n = this.byId.get(id);
      if (!n) return false;
      this._userMoved = true;
      const k = Math.max(this.view.k, 1.4);
      this.view = { k, x: this.W / 2 - n.x * k, y: this.H / 2 - n.y * k };
      this.select(n);
      return true;
    }

    select(node) {
      this.selected = node || null;
      this.onSelect(node ? this._publicNode(node) : null);
      this.draw();
    }

    neighbors(id) {
      /* 選択ノードの関係一覧 [{node, edge}] */
      const out = [];
      for (const e of this.all.edges) {
        if (e.source === id || e.target === id) {
          const other = e.source === id ? e.target : e.source;
          const n = this.all.nodes.find((x) => x.id === other);
          if (n) out.push({ node: n, edge: e });
        }
      }
      return out.sort((a, b) => (b.edge.strength || 0) - (a.edge.strength || 0));
    }

    _publicNode(n) {
      const { x, y, vx, vy, fixed, r, color, ...rest } = n;
      return rest;
    }

    search(text) {
      const q = String(text || "").trim().toLowerCase();
      this.matches = new Set();
      if (q) {
        for (const n of this.all.nodes) {
          if (String(n.name).toLowerCase().includes(q)) this.matches.add(n.id);
        }
        // 非表示（孤立）ノードがヒットしたら表示に切り替える
        if ([...this.matches].some((id) => !this.byId.has(id)) && !this.showIsolated) {
          this.setShowIsolated(true);
        }
      }
      this.draw();
      // 完全一致 → 前方一致 → 関係の多い順
      const rank = (n) => {
        const name = String(n.name).toLowerCase();
        return name === q ? 0 : name.startsWith(q) ? 1 : 2;
      };
      return this.all.nodes.filter((n) => this.matches.has(n.id))
        .sort((a, b) => rank(a) - rank(b) || this._degree(b.id) - this._degree(a.id));
    }

    highlightCommunity(cid) {
      this.highlightCom = this.highlightCom === cid ? null : cid;
      if (this.highlightCom) {
        const members = this.nodes.filter((n) => n.community === cid);
        if (members.length) {
          this._userMoved = true;
          let sx = 0, sy = 0;
          for (const m of members) { sx += m.x; sy += m.y; }
          const k = Math.max(this.view.k, 1.0);
          this.view = { k, x: this.W / 2 - (sx / members.length) * k,
            y: this.H / 2 - (sy / members.length) * k };
        }
      }
      this.draw();
    }

    // ------------------------------------------------------------ draw
    draw() {
      const ctx = this.ctx, v = this.view, dpr = this.dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.W, this.H);
      if (!this.nodes.length) {
        ctx.fillStyle = "#8b98b8"; ctx.font = "14px sans-serif";
        ctx.fillText(this.emptyMessage || "表示できるグラフがありません", 24, 36);
        return;
      }
      const sel = this.selected;
      const focusSet = sel ? new Set([sel.id, ...(this.adj.get(sel.id) || [])]) : null;
      const comSet = this.highlightCom;
      const dimmed = (n) => (focusSet && !focusSet.has(n.id))
        || (comSet && n.community !== comSet)
        || (this.matches.size && !this.matches.has(n.id) && !(focusSet && focusSet.has(n.id)));

      ctx.setTransform(dpr * v.k, 0, 0, dpr * v.k, dpr * v.x, dpr * v.y);
      // edges
      for (const e of this.edges) {
        const hot = sel && (e.a === sel || e.b === sel);
        const dim = dimmed(e.a) || dimmed(e.b);
        ctx.strokeStyle = hot ? "rgba(255,255,255,0.85)"
          : dim ? "rgba(139,152,184,0.06)" : "rgba(139,152,184,0.32)";
        ctx.lineWidth = (hot ? 1.6 : 0.6 + (e.strength || 5) / 10) / Math.max(0.6, v.k);
        ctx.beginPath(); ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); ctx.stroke();
      }
      // nodes
      for (const n of this.nodes) {
        const dim = dimmed(n);
        ctx.globalAlpha = dim ? 0.15 : 1;
        ctx.fillStyle = n.color;
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill();
        if (n === sel || n === this.hover || this.matches.has(n.id)) {
          ctx.strokeStyle = this.matches.has(n.id) && n !== sel ? "#facc15" : "#ffffff";
          ctx.lineWidth = 2.2 / v.k; ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      // labels（スクリーン座標で描いて文字サイズを一定に保つ）
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = "12px sans-serif";
      ctx.textBaseline = "middle";
      const drawn = [];
      const want = (n) => n === sel || n === this.hover || this.matches.has(n.id)
        || (focusSet && focusSet.has(n.id))
        || (comSet && n.community === comSet && n.r * v.k > 5)
        || (!focusSet && !comSet && !this.matches.size
            && (this.topLabel.has(n.id) || n.r * v.k > 9 || v.k > 2.2));
      const order = [...this.nodes].sort((a, b) =>
        (b === sel) - (a === sel) || (b === this.hover) - (a === this.hover) || b.deg - a.deg);
      for (const n of order) {
        if (!want(n) || dimmed(n) && n !== sel) continue;
        const sx = n.x * v.k + v.x, sy = n.y * v.k + v.y;
        if (sx < -50 || sy < -20 || sx > this.W + 50 || sy > this.H + 20) continue;
        const text = String(n.name).slice(0, 18);
        const w = ctx.measureText(text).width;
        const lx = sx + n.r * v.k + 4, ly = sy;
        const box = [lx - 2, ly - 8, w + 4, 16];
        const important = n === sel || n === this.hover || this.matches.has(n.id);
        if (!important && drawn.some((b) => b[0] < box[0] + box[2] && box[0] < b[0] + b[2]
          && b[1] < box[1] + box[3] && box[1] < b[1] + b[3])) continue;   // 重なるラベルは省略
        drawn.push(box);
        ctx.fillStyle = "rgba(11,16,28,0.72)";
        ctx.fillRect(box[0], box[1], box[2], box[3]);
        ctx.fillStyle = important ? "#ffffff" : "#e6ecf7";
        ctx.fillText(text, lx, ly);
      }
    }

    // ------------------------------------------------------------ input
    _pos(ev) {
      const rect = this.canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }

    _nodeAt(p) {
      const v = this.view;
      const wx = (p.x - v.x) / v.k, wy = (p.y - v.y) / v.k;
      let best = null, bestD = Infinity;
      for (const n of this.nodes) {
        const d = Math.hypot(wx - n.x, wy - n.y);
        const hit = n.r + 4 / v.k;
        if (d <= hit && d < bestD) { best = n; bestD = d; }
      }
      return best;
    }

    _bind() {
      const c = this.canvas;
      c.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const p = this._pos(ev);
        this.zoomBy(ev.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y);
      }, { passive: false });
      c.addEventListener("mousedown", (ev) => {
        const p = this._pos(ev);
        const n = this._nodeAt(p);
        this.drag = n ? { type: "node", node: n, sx: p.x, sy: p.y, moved: false }
          : { type: "pan", sx: p.x, sy: p.y, vx: this.view.x, vy: this.view.y, moved: false };
        if (n) n.fixed = true;
        this._userMoved = true;
        c.style.cursor = n ? "grabbing" : "move";
      });
      global.addEventListener("mousemove", (ev) => {
        const p = this._pos(ev);
        if (this.drag) {
          const d = this.drag;
          if (Math.abs(p.x - d.sx) + Math.abs(p.y - d.sy) > 3) d.moved = true;
          if (d.type === "pan") {
            this.view.x = d.vx + (p.x - d.sx); this.view.y = d.vy + (p.y - d.sy);
            this.draw();
          } else if (d.moved) {
            d.node.x = (p.x - this.view.x) / this.view.k;
            d.node.y = (p.y - this.view.y) / this.view.k;
            this._reheat(0.12);
          }
          return;
        }
        if (ev.target !== c) {
          if (this.hover) { this.hover = null; this._tip(null); this.draw(); }
          return;
        }
        const n = this._nodeAt(p);
        if (n !== this.hover) { this.hover = n; this.draw(); }
        c.style.cursor = n ? "pointer" : "grab";
        this._tip(n, ev);
      });
      global.addEventListener("mouseup", () => {
        const d = this.drag;
        if (!d) return;
        this.drag = null;
        c.style.cursor = "grab";
        if (d.type === "node") {
          d.node.fixed = false;
          if (!d.moved) this.select(this.selected === d.node ? null : d.node);
        } else if (!d.moved) {
          this.select(null);                 // 背景クリックで選択解除
        }
      });
      c.addEventListener("mouseleave", () => { this.hover = null; this._tip(null); this.draw(); });
      c.addEventListener("dblclick", (ev) => {
        const p = this._pos(ev);
        if (!this._nodeAt(p)) this.zoomBy(1.6, p.x, p.y);
      });
    }

    _tip(n, ev) {
      const tip = this.tooltip;
      if (!tip) return;
      if (!n || !ev) { tip.classList.add("hidden"); return; }
      tip.classList.remove("hidden");
      const x = Math.min(ev.clientX + 14, global.innerWidth - 360);
      tip.style.left = `${x}px`;
      tip.style.top = `${ev.clientY + 14}px`;
      tip.innerHTML = `<b>${esc(n.name)}</b>（${esc(n.type || "")}）`
        + `<span class="muted"> 関係 ${n.deg}${n.community ? " / " + esc(n.community) : ""}</span>`
        + (n.description ? `<br>${esc(String(n.description).slice(0, 240))}` : "")
        + `<br><span class="muted">クリックで詳細</span>`;
    }
  }

  global.GraphView = GraphView;
})(typeof window !== "undefined" ? window : globalThis);
