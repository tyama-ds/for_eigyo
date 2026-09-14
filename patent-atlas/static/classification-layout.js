/* Offline classification layout. Coordinates describe an exploratory visual
 * estimate from wording and explicit catalogue relationships, not a patent model.
 * No classification ancestry is inferred from the spelling of a code. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.PatentClassLayout = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const SPACE_X = 180, SPACE_Y = 122, PAD_X = 96, PAD_TOP = 86, PAD_BOTTOM = 84;
  const MAX_BLOCK = 96;
  // These bilingual cues describe the *wording* of a node, never its official
  // classification. In particular, shared technology can join IPC and F-term.
  const CONCEPTS = [
    ['interface', '界面・表面', /界面|表面|被覆|コーティング|接合|\binterfac\w*|\bsurface\w*|\bcoat\w*|\badhesi\w*/i],
    ['electrolyte', '電解質', /電解質|\belectrolyte\w*/i],
    ['electrode', '電極・活物質', /電極|正極|負極|活物質|\belectrode\w*|\bcathod\w*|\banod\w*/i],
    ['process', '製造・加工', /製造|加工|成形|焼結|混合|圧延|積層|接着|\bmanufactur\w*|\bfabricat\w*|\bsinter\w*|\bmould\w*|\bmold\w*|\blaminat\w*/i],
    ['material', '材料・組成', /材料|組成|化合物|粉末|セラミック|\bmaterial\w*|\bcomposi\w*|\bcompound\w*|\bceramic\w*|\bpowder\w*/i],
    ['battery', '蓄電・電池', /電池|蓄電|\bbatter\w*|\baccumulator\w*|\belectrochemical\s+cell\w*/i],
    ['measurement', '測定・評価', /測定|検査|試験|評価|検出|センサ|\bmeasur\w*|\bsensor\w*|\bdetect\w*|\binspect\w*|\btest\w*/i],
    ['thermal', '熱・温度', /温度|熱処理|加熱|冷却|\bthermal\w*|\bheating\b|\bcooling\b|\btemperature\w*/i],
    ['optical', '光学・撮像', /光学|撮像|照明|レンズ|\boptic\w*|\bcamera\w*|\blens\w*|\billuminat\w*/i],
    ['acoustic', '音響・振動', /音響|楽器|振動|\bacoustic\w*|\bmusical\b|\bpiano\b|\bsound\b|\bvibrat\w*/i],
    ['agriculture', '農業・栽培', /農業|栽培|耕|土壌|\bagricultur\w*|\bsoil\b|\bplough\w*|\bcrop\w*|\btractor\w*/i],
    ['marine', '船舶・航行', /船舶|航行|\bmarine\b|\bship\w*|\bvessel\w*|\bocean\b|\bnavigat\w*/i],
    ['power', '電力・発電', /発電|送電|配電|電力|\belectric\w*|\bgenerat\w*|\bturbine\w*|\bpower\b/i],
    ['computing', '情報・通信', /通信|演算|情報処理|コンピュータ|\bcomput\w*|\bcommunicat\w*|\bnetwork\w*|\bdata\s+process\w*/i],
    ['medical', '医療・診断', /医療|診断|治療|薬剤|\bmedic\w*|\bdiagnos\w*|\btherap\w*|\bpharmaceut\w*/i],
    ['mechanical', '機構・駆動', /機構|駆動|歯車|軸受|\bmechanism\w*|\bactuat\w*|\bgear\w*|\bbearing\w*/i],
  ];
  const FACET_LABELS = {device: '構造・装置', material: '材料・組成', process: '製造・加工', interface: '界面・表面', performance: '性能・評価', application: '用途・システム'};
  const STOP = new Set(('a an and are as at be being by comprises comprising containing thereof for from in into is it of on or other otherwise parts specially such the their these this to using use with without having apparatus devices methods thereof する ため もの こと').split(' '));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const identity = item => String(item.kind || '') + ':' + String(item.code || '');
  const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  function hash(text) {
    let value = 2166136261;
    for (let i = 0; i < text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 16777619);
    return (value >>> 0) / 4294967296;
  }
  function textFeatures(item) {
    const counts = new Map();
    const add = (token, weight) => { if (!STOP.has(token)) counts.set(token, (counts.get(token) || 0) + weight); };
    const sources = new Map();
    for (const [value, weight] of [[item.title, 2], [item.title_en, 1.6], [item.title_official, 1.6], ...(Array.isArray(item.terms) ? item.terms : [item.terms]).map(value => [value, 0.45])]) {
      if (typeof value === 'string' && value.trim()) sources.set(value, Math.max(weight, sources.get(value) || 0));
    }
    for (const [value, sourceWeight] of sources) {
      // A code used as a missing-title placeholder contains no semantic evidence.
      if (value.replace(/\s/g, '') === String(item.code || '').replace(/\s/g, '')) continue;
      const text = value.normalize('NFKC').toLowerCase().slice(0, 3000);
      for (let word of text.match(/[a-z][a-z-]{1,}/g) || []) {
        if (word.length > 4 && word.endsWith('ies')) word = word.slice(0, -3) + 'y';
        else if (word.length > 4 && word.endsWith('s') && !word.endsWith('ss')) word = word.slice(0, -1);
        if (!STOP.has(word)) add('w:' + word, sourceWeight);
      }
      for (const run of text.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]+/gu) || []) {
        if (run.length === 1) add('c:' + run, 0.35 * sourceWeight);
        for (let size = 2; size <= Math.min(3, run.length); size++) {
          for (let i = 0; i <= run.length - size; i++) add('c:' + run.slice(i, i + size), (size === 2 ? 1 : 0.6) * sourceWeight);
        }
      }
    }
    return counts;
  }
  function conceptsOf(item) {
    const titles = [item.title, item.title_en, item.title_official].filter(value => typeof value === 'string' && value.trim() && value.replace(/\s/g, '') !== String(item.code || '').replace(/\s/g, ''));
    const text = (titles.length ? titles : Array.isArray(item.terms) ? item.terms : [item.terms]).filter(value => typeof value === 'string').join(' ').normalize('NFKC').slice(0, 12000);
    const result = new Map(CONCEPTS.filter(([, , pattern]) => pattern.test(text)).map(([id, label]) => [id, label]));
    for (const facet of Array.isArray(item.facets) ? item.facets : []) {
      const id = typeof facet === 'string' ? facet : facet?.id;
      const label = typeof facet === 'object' ? facet?.label : FACET_LABELS[id];
      if (id && label && !result.has(id)) result.set(id, label);
    }
    return result;
  }
  function vectors(items) {
    const raw = items.map(textFeatures), frequency = new Map();
    for (const doc of raw) for (const token of doc.keys()) frequency.set(token, (frequency.get(token) || 0) + 1);
    return raw.map(doc => {
      let norm = 0;
      const vector = new Map();
      for (const [token, count] of doc) {
        const weight = Math.sqrt(count) * (1 + Math.log((items.length + 1) / (frequency.get(token) + 1)));
        vector.set(token, weight); norm += weight * weight;
      }
      if (norm) for (const [token, weight] of vector) vector.set(token, weight / Math.sqrt(norm));
      return vector;
    });
  }
  function cosine(a, b) {
    if (a.size > b.size) [a, b] = [b, a];
    let sum = 0;
    for (const [token, weight] of a) sum += weight * (b.get(token) || 0);
    return clamp(sum, 0, 1);
  }
  function pathOf(item) {
    const path = (Array.isArray(item.ancestors) ? item.ancestors : []).map(value => typeof value === 'string' ? value : value?.code).filter(Boolean);
    if (item.parent && !path.includes(item.parent)) path.push(item.parent);
    return path;
  }
  function hierarchy(a, b) {
    if (a.kind !== b.kind || !a.kind) return 0;
    if (a.parent === b.code || b.parent === a.code) return 0.82;
    if (a.parent && a.parent === b.parent) return 0.58;
    const ap = pathOf(a), bp = pathOf(b);
    const ai = ap.indexOf(b.code), bi = bp.indexOf(a.code);
    if (ai >= 0 || bi >= 0) return 0.64 / Math.sqrt(ai >= 0 ? ap.length - ai : bp.length - bi);
    let nearest = Infinity;
    ap.forEach((code, index) => { const other = bp.indexOf(code); if (other >= 0) nearest = Math.min(nearest, ap.length - index + bp.length - other); });
    return Number.isFinite(nearest) ? 0.7 / nearest : 0;
  }
  function similarities(items, docs, concepts) {
    const frequency = new Map();
    for (const tags of concepts) for (const id of tags.keys()) frequency.set(id, (frequency.get(id) || 0) + 1);
    const matrix = items.map((item, i) => items.map((other, j) => {
      if (i === j) return 1;
      const wording = cosine(docs[i], docs[j]);
      let shared = 0, total = 0;
      for (const id of new Set([...concepts[i].keys(), ...concepts[j].keys()])) {
        const weight = 1 + Math.log((items.length + 1) / (frequency.get(id) + 1));
        total += weight;
        if (concepts[i].has(id) && concepts[j].has(id)) shared += weight;
      }
      const technical = total ? shared / total : 0;
      const evidence = wording + (1 - wording) * technical * 0.62;
      // Hierarchy provides context without flattening modest wording differences
      // between siblings that all have the same direct parent.
      return evidence + (1 - evidence) * hierarchy(item, other) * 0.75;
    }));
    let low = 1, high = 0;
    matrix.forEach((row, i) => row.forEach((value, j) => { if (i !== j) { low = Math.min(low, value); high = Math.max(high, value); } }));
    // Distance is relative to the displayed set: a common parent shared by
    // everyone must not overwhelm its weaker, discriminating wording evidence.
    const range = Math.max(0.15, high - low);
    return {raw: matrix, normalized: matrix.map((row, i) => row.map((value, j) => i === j ? 1 : clamp((value - low) / range, 0, 1)))};
  }
  function neighborhoods(matrix, raw) {
    // Average-link clustering avoids the long chains made by single-link
    // grouping. Broad shared context alone does not merge different subtopics.
    const groups = matrix.map((_, i) => [i]);
    while (groups.length > 1) {
      let best = 0.43, pair;
      for (let a = 0; a < groups.length; a++) for (let b = a + 1; b < groups.length; b++) {
        let score = 0, weakest = 1, strong = 0;
        for (const i of groups[a]) for (const j of groups[b]) {
          score += matrix[i][j]; weakest = Math.min(weakest, matrix[i][j]);
          strong = Math.max(strong, raw[i][j]);
        }
        score /= groups[a].length * groups[b].length;
        if (strong < 0.24 || weakest < 0.16) continue;
        if (score > best + 1e-10) { best = score; pair = [a, b]; }
      }
      if (!pair) break;
      groups[pair[0]].push(...groups[pair[1]]);
      groups.splice(pair[1], 1);
    }
    return groups;
  }
  function groupLabel(members, items, concepts) {
    const local = new Map(), global = new Map();
    for (const tags of concepts) for (const [id, label] of tags) global.set(id, {label, count: (global.get(id)?.count || 0) + 1});
    for (const i of members) for (const id of concepts[i].keys()) local.set(id, (local.get(id) || 0) + 1);
    const tags = [...local].filter(([, count]) => count >= Math.ceil(members.length / 2)).map(([id, count]) => ({id, label: global.get(id).label, score: count / members.length * (1 + Math.log((items.length + 1) / (global.get(id).count + 1)))})).sort((a, b) => b.score - a.score || compare(a.id, b.id));
    if (tags.length) return tags.slice(0, 2).map(tag => tag.label).join(' · ');
    const first = items[members[0]];
    if (members.every(i => items[i].kind === first.kind)) {
      const common = pathOf(first).filter(code => members.every(i => pathOf(items[i]).includes(code) || items[i].code === code));
      const code = common.at(-1);
      if (code) {
        const parent = items.find(item => item.kind === first.kind && item.code === code);
        return parent?.title && parent.title !== code ? parent.title.slice(0, 28) : `${first.kind} ${code} 周辺`;
      }
    }
    const shortest = members.map(i => items[i].title || items[i].title_official || '').filter(Boolean).sort((a, b) => a.length - b.length || compare(a, b))[0];
    return shortest ? shortest.slice(0, 28) : '名称・階層が近い分類';
  }
  function spectral(matrix, keys) {
    const n = matrix.length, means = matrix.map(row => row.reduce((sum, value) => sum + value, 0) / n);
    const mean = means.reduce((sum, value) => sum + value, 0) / n;
    const centered = matrix.map((row, i) => row.map((value, j) => value - means[i] - means[j] + mean));
    const axes = [];
    for (let axis = 0; axis < 2; axis++) {
      let vector = keys.map(key => hash(key + '|axis' + axis) - 0.5);
      for (let iteration = 0; iteration < 40; iteration++) {
        let next = centered.map(row => row.reduce((sum, value, j) => sum + value * vector[j], 0));
        for (const previous of axes) {
          const dot = next.reduce((sum, value, j) => sum + value * previous[j], 0);
          next = next.map((value, j) => value - dot * previous[j]);
        }
        const norm = Math.hypot(...next);
        if (norm < 1e-8) break;
        vector = next.map(value => value / norm);
      }
      const norm = Math.hypot(...vector) || 1;
      axes.push(vector.map(value => value / norm));
    }
    return axes;
  }
  function separate(points, span, passes, guarantee = false) {
    // Anisotropic rectangles include the title and floating-animation clearance.
    // Expand vertically when horizontal boundaries leave no room to separate.
    for (let pass = 0; pass < passes; pass++) {
      let overlap = false;
      for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
        const a = points[i], b = points[j], dx = b[0] - a[0], dy = b[1] - a[1];
        const ox = 1 - Math.abs(dx), oy = 1 - Math.abs(dy);
        if (ox <= 0 || oy <= 0) continue;
        overlap = true;
        const sx = dx < 0 ? -1 : 1, sy = dy < 0 ? -1 : 1;
        const horizontalRoom = sx > 0 ? a[0] + span - b[0] : b[0] + span - a[0];
        if (ox < oy && horizontalRoom > ox + 0.01) {
          a[0] = clamp(a[0] - sx * (ox / 2 + 0.002), 0, span);
          b[0] = clamp(b[0] + sx * (ox / 2 + 0.002), 0, span);
        } else { a[1] -= sy * (oy / 2 + 0.002); b[1] += sy * (oy / 2 + 0.002); }
      }
      if (!overlap) return;
    }
    if (!guarantee) return;
    // A deterministic sweep guarantees clearance even for pathological dense
    // inputs. It only moves a point past earlier overlapping rectangles.
    const ordered = points.map((point, index) => ({point, index})).sort((a, b) => a.point[1] - b.point[1] || a.index - b.index);
    for (let i = 0; i < ordered.length; i++) for (let j = 0; j < i; j++) {
      const a = ordered[i].point, b = ordered[j].point;
      if (Math.abs(a[0] - b[0]) < 1 && a[1] - b[1] < 1) a[1] = b[1] + 1.004;
    }
  }
  function packNeighborhoods(points, groups, matrix, keys, width) {
    const gap = 14, usable = width - 12, boxes = [], output = [];
    const ordered = groups.slice().sort((a, b) => b.length - a.length || a.reduce((sum, i) => sum + points[i][1], 0) / a.length - b.reduce((sum, i) => sum + points[i][1], 0) / b.length || a[0] - b[0]);
    let bottom = 0;
    for (const members of ordered) {
      const axes = members.length > 1 ? spectral(members.map(i => members.map(j => matrix[i][j])), members.map(i => keys[i])) : [[0], [0]];
      const sequence = members.map((member, i) => ({member, i})).sort((a, b) => axes[0][a.i] - axes[0][b.i] || axes[1][a.i] - axes[1][b.i] || a.member - b.member);
      let best;
      // Alternative orientations allow a pair to fit beside a larger group.
      // Within a neighborhood, small stable offsets keep the display organic;
      // only nodes with real similarity evidence enter these neighborhoods.
      const maxCols = Math.min(Math.ceil(Math.sqrt(members.length)), width < 750 && members.length === 2 ? 1 : members.length, Math.floor((usable - 188) / (SPACE_X * 1.07)) + 1);
      for (let cols = 1; cols <= maxCols; cols++) {
        const local = sequence.map(({member}, index) => {
          const row = Math.floor(index / cols), rowCount = Math.min(cols, members.length - row * cols);
          return [(index % cols + (cols - rowCount) / 2) * SPACE_X * 1.07 + (hash(keys[member] + '|local-x') - 0.5) * 7,
            row * SPACE_Y * 1.11 + (hash(keys[member] + '|local-y') - 0.5) * 9];
        });
        const minX = Math.min(...local.map(p => p[0])), minY = Math.min(...local.map(p => p[1]));
        const boxWidth = Math.max(...local.map(p => p[0])) - minX + 180;
        const boxHeight = Math.max(...local.map(p => p[1])) - minY + 158;
        if (boxWidth > usable) continue;
        const starts = [...new Set([0, usable - boxWidth, ...boxes.flatMap(box => [box.x, box.x + box.width + gap])])].filter(x => x >= 0 && x + boxWidth <= usable + 0.001).sort((a, b) => a - b);
        for (const x of starts) {
          let y = 0;
          for (let pass = 0; pass <= boxes.length; pass++) {
            const blockers = boxes.filter(box => x < box.x + box.width + gap && x + boxWidth + gap > box.x && y < box.y + box.height + gap && y + boxHeight + gap > box.y);
            if (!blockers.length) break;
            y = Math.max(...blockers.map(box => box.y + box.height + gap));
          }
          const score = Math.max(bottom, y + boxHeight) * usable + y * 30 + x * 0.001;
          if (!best || score < best.score) best = {x, y, width: boxWidth, height: boxHeight, local, minX, minY, score};
        }
      }
      if (!best) continue; // Width is clamped to >=620, so one column always fits.
      boxes.push(best); bottom = Math.max(bottom, best.y + best.height);
      sequence.forEach(({member}, i) => { output[member] = [best.x + best.local[i][0] - best.minX + 96, best.y + best.local[i][1] - best.minY + PAD_TOP]; });
    }
    return output;
  }
  function block(items, docs, width) {
    if (!items.length) return {positions: [], height: 380, groups: [], links: []};
    if (items.length === 1) return {positions: [[width / 2, 175]], height: 380, groups: [], links: []};
    const keys = items.map(identity), concepts = items.map(conceptsOf);
    const {raw, normalized: matrix} = similarities(items, docs, concepts);
    const groups = neighborhoods(matrix, raw), memberships = [];
    groups.forEach((members, group) => members.forEach(i => { memberships[i] = group; }));
    const axes = spectral(matrix, keys);
    const span = (width - PAD_X * 2) / SPACE_X;
    const vertical = Math.max(1.2, items.length / (span + 1) * 1.2);
    const range = axes.map(axis => Math.max(...axis) - Math.min(...axis) || 1);
    const minima = axes.map(axis => Math.min(...axis));
    const points = items.map((item, i) => [
      clamp((axes[1][i] - minima[1]) / range[1] * span + (hash(keys[i] + '|x') - 0.5) * 0.05, 0, span),
      (axes[0][i] - minima[0]) / range[0] * vertical + (hash(keys[i] + '|y') - 0.5) * 0.05,
    ]);
    const far = Math.max(1.8, Math.sqrt(items.length) * 0.5);
    for (let iteration = 0; iteration < 180; iteration++) {
      const forces = points.map(() => [0, 0]);
      for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
        const dx = points[j][0] - points[i][0], dy = points[j][1] - points[i][1];
        const distance = Math.hypot(dx, dy) || 0.01, sameGroup = memberships[i] === memberships[j];
        const similarity = sameGroup ? Math.max(0.72, matrix[i][j]) : matrix[i][j] * 0.35;
        const target = sameGroup ? 1.08 + Math.sqrt(groups[memberships[i]].length) * 0.35 * (1 - similarity) : 2.05 + far * (1 - similarity) ** 2;
        const strength = (distance - target) * (0.04 + similarity * 0.8) / distance;
        forces[i][0] += dx * strength; forces[i][1] += dy * strength;
        forces[j][0] -= dx * strength; forces[j][1] -= dy * strength;
      }
      const rate = 0.16 / Math.sqrt(items.length) * (1 - iteration / 220);
      points.forEach((point, i) => {
        point[0] = clamp(point[0] + clamp(forces[i][0] * rate, -0.16, 0.16), 0, span);
        point[1] += clamp(forces[i][1] * rate, -0.16, 0.16);
      });
      if (iteration % 3 === 0) separate(points, span, 2);
    }
    separate(points, span, 120, true);
    const top = Math.min(...points.map(point => point[1]));
    const positions = groups.some(members => members.length > 1) ? packNeighborhoods(points, groups, matrix, keys, width) : points.map(point => [PAD_X + point[0] * SPACE_X, PAD_TOP + (point[1] - top) * SPACE_Y]);
    const height = Math.max(380, Math.max(...positions.map(point => point[1])) + PAD_BOTTOM);
    const extra = height - PAD_BOTTOM - Math.max(...positions.map(point => point[1]));
    if (extra > 0) positions.forEach(point => { point[1] += extra / 2; });
    const captions = groups.filter(members => members.length > 1).map(members => {
      const xs = members.map(i => positions[i][0]), ys = members.map(i => positions[i][1]);
      return {id: keys[members[0]], label: groupLabel(members, items, concepts), memberIndices: members,
        bounds: {x: Math.min(...xs) - 90, y: Math.min(...ys) - 76, width: Math.max(...xs) - Math.min(...xs) + 180, height: Math.max(...ys) - Math.min(...ys) + 158}};
    });
    const links = [], linked = new Set();
    for (let i = 0; i < items.length; i++) {
      const neighbors = matrix[i].map((strength, j) => ({j, strength})).filter(({j, strength}) => j !== i && strength >= 0.43 && raw[i][j] >= 0.24).sort((a, b) => b.strength - a.strength || a.j - b.j).slice(0, 2);
      for (const {j, strength} of neighbors) {
        const id = [Math.min(i, j), Math.max(i, j)].join(':');
        if (linked.has(id)) continue;
        linked.add(id);
        links.push({from: i, to: j, strength, reason: hierarchy(items[i], items[j]) >= 0.4 ? '分類階層・名称の近さ' : '技術項目・名称の近さ'});
      }
    }
    return {positions, height: Math.ceil(height), groups: captions, links};
  }
  function compute(items, options = {}) {
    items = Array.isArray(items) ? items : [];
    const width = Number.isFinite(Number(options.width)) ? clamp(Number(options.width), 620, 2400) : 1000;
    const ordered = items.map((item, index) => ({item: item || {}, index})).sort((a, b) => compare(identity(a.item), identity(b.item)));
    if (options.mode === 'grid') {
      const desiredCols = width <= 700 ? 2 : items.length > 10 ? 4 : 3;
      const cols = Math.min(desiredCols, Math.floor(width / (PAD_X * 2)));
      const height = Math.max(380, Math.ceil(items.length / cols) * 142 + 45);
      const positions = new Array(items.length);
      ordered.forEach((entry, i) => { positions[entry.index] = [width / cols * (i % cols + 0.5), 86 + Math.floor(i / cols) * 142]; });
      return {positions, width, height, method: '整列', groups: [], links: []};
    }
    const docs = vectors(ordered.map(entry => entry.item));
    ordered.forEach((entry, i) => { entry.doc = docs[i]; });
    if (items.length > MAX_BLOCK) {
      // Large overviews are partitioned by their strongest wording feature (or
      // a known parent if there is no wording), retaining every candidate.
      for (const entry of ordered) {
        const strongest = [...entry.doc].sort((a, b) => b[1] - a[1] || compare(a[0], b[0]))[0];
        entry.topic = strongest?.[0] || (entry.item.parent ? entry.item.kind + ':' + entry.item.parent : identity(entry.item));
      }
      ordered.sort((a, b) => compare(a.topic, b.topic) || compare(identity(a.item), identity(b.item)));
    }
    const positions = new Array(items.length), groups = [], links = [];
    let height = 0;
    for (let start = 0; start < ordered.length; start += MAX_BLOCK) {
      const slice = ordered.slice(start, start + MAX_BLOCK);
      const result = block(slice.map(entry => entry.item), slice.map(entry => entry.doc), width);
      slice.forEach((entry, i) => { positions[entry.index] = [result.positions[i][0], result.positions[i][1] + height]; });
      groups.push(...result.groups.map(group => ({...group, memberIndices: group.memberIndices.map(i => slice[i].index), bounds: {...group.bounds, y: group.bounds.y + height}})));
      links.push(...result.links.map(link => ({...link, from: slice[link.from].index, to: slice[link.to].index})));
      height += result.height;
    }
    return {positions, width, height: height || 380, groups, links, method: '技術項目・名称の類似度・分類階層' + (items.length > MAX_BLOCK ? '（分割配置）' : '')};
  }
  return Object.freeze({compute});
});
