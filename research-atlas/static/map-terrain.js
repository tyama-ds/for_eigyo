(function (global) {
  'use strict';

  // Height is relative density in the projected paper map, never a forecast.
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const finite = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const unit = value => clamp(finite(value), 0, 1);
  const fmt = value => String(Math.round(value * 100) / 100);

  function view(options = {}) {
    const width = clamp(finite(options.width, 800), 80, 10000);
    const height = clamp(finite(options.height, 500), 80, 10000);
    const pad = clamp(finite(options.pad, 48), 0, Math.min(width, height) / 3);
    return {
      width, height, pad,
      mode: options.mode === 'relief' ? 'relief' : 'flat',
      // Leave room above the tilted floor, including at maximum density.
      heightScale: clamp(finite(options.heightScale, 65), 0, (height - pad * 2) * .30),
    };
  }

  function projected(x, y, z, v) {
    const px = unit(x), py = unit(y), pz = unit(z);
    const w = v.width - v.pad * 2, h = v.height - v.pad * 2;
    if (v.mode === 'flat') return [v.pad + px * w, v.pad + py * h];
    return [v.pad + w * (.88 * px + .12 * py), v.pad + h * (.30 + .63 * py) - pz * v.heightScale];
  }

  function project(x, y, z, options) {
    return projected(x, y, z, view(options));
  }

  function readGrid(terrain) {
    const grid = terrain?.grid;
    if (!grid || !Array.isArray(grid.values)) return null;
    const width = Number(grid.width), height = Number(grid.height);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 2 || height < 2 || (width - 1) * (height - 1) > 1600 || grid.values.length !== width * height) return null;
    return {width, height, values: grid.values.map(unit)};
  }

  function sample(grid, x, y) {
    const gx = unit(x) * (grid.width - 1), gy = unit(y) * (grid.height - 1);
    const x0 = Math.floor(gx), y0 = Math.floor(gy), x1 = Math.min(x0 + 1, grid.width - 1), y1 = Math.min(y0 + 1, grid.height - 1);
    const tx = gx - x0, ty = gy - y0;
    const top = grid.values[y0 * grid.width + x0] * (1 - tx) + grid.values[y0 * grid.width + x1] * tx;
    const bottom = grid.values[y1 * grid.width + x0] * (1 - tx) + grid.values[y1 * grid.width + x1] * tx;
    return top * (1 - ty) + bottom * ty;
  }

  function heightAt(terrain, x, y) {
    const grid = readGrid(terrain);
    return grid ? sample(grid, x, y) : 0;
  }

  function referenceGridLines(terrain, mode) {
    if (!['relief', 'layers'].includes(mode)) return [];
    const grid = mode === 'relief' ? readGrid(terrain) : null;
    if (mode === 'relief' && (!grid || !grid.values.some(value => value > 0))) return [];
    const lines = [];
    // Three quiet guides per axis. Shared XY positions, independent of paper count.
    for (const axis of [0, 1]) for (const step of [.25, .5, .75]) {
      lines.push(Array.from({length: 33}, (_, i) => {
        const x = axis === 0 ? step : i / 32, y = axis === 0 ? i / 32 : step;
        return [x, y, grid ? sample(grid, x, y) : 0];
      }));
    }
    return lines;
  }

  function point(value) {
    return Array.isArray(value) && value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1]);
  }

  function path(points, close = false) {
    if (points.length < 2) return '';
    return `M${points.map(p => `${fmt(p[0])},${fmt(p[1])}`).join('L')}${close ? 'Z' : ''}`;
  }

  function shade(z, dx, dy, relief) {
    const light = relief ? clamp(.90 - dx * 2.6 - dy * 1.8, .62, 1.27) : .95;
    const base = relief ? [14 + 28 * z, 33 + 64 * z, 46 + 53 * z] : [15 + 17 * z, 31 + 43 * z, 45 + 37 * z];
    return `rgb(${base.map(channel => Math.round(clamp(channel * light, 0, 255))).join(',')})`;
  }

  function floor(v) {
    const corners = [[0, 0], [1, 0], [1, 1], [0, 1]].map(p => projected(...p, 0, v));
    return `<path class="terrain-floor" d="${path(corners, true)}"/>`;
  }

  function surface(grid, v) {
    const paths = [];
    const at = (x, y) => grid.values[y * grid.width + x];
    const vertex = (x, y) => projected(x / (grid.width - 1), y / (grid.height - 1), at(x, y), v);
    // Render from the back toward the viewer so front slopes cover rear faces.
    for (let y = 0; y < grid.height - 1; y++) {
      for (let x = 0; x < grid.width - 1; x++) {
        const a = at(x, y), b = at(x + 1, y), c = at(x + 1, y + 1), d = at(x, y + 1);
        const pa = vertex(x, y), pb = vertex(x + 1, y), pc = vertex(x + 1, y + 1), pd = vertex(x, y + 1);
        const first = shade((a + b + d) / 3, b - a, d - a, v.mode === 'relief');
        const second = shade((b + c + d) / 3, c - d, c - b, v.mode === 'relief');
        paths.push(`<path d="${path([pa, pb, pd], true)}" fill="${first}" stroke="${first}"/>`);
        paths.push(`<path d="${path([pb, pc, pd], true)}" fill="${second}" stroke="${second}"/>`);
      }
    }
    return `<g class="terrain-surface terrain-surface-${v.mode}">${paths.join('')}</g>`;
  }

  function contours(terrain, v) {
    if (!Array.isArray(terrain.contours)) return '';
    const lines = [];
    // Bound work even when loading an externally supplied result JSON.
    let remaining = 60000;
    for (const contour of terrain.contours.slice(0, 32)) {
      if (!Number.isFinite(contour?.level) || !Array.isArray(contour.paths)) continue;
      const level = unit(contour.level), projectedPaths = [];
      for (const points of contour.paths.slice(0, 512)) {
        if (!Array.isArray(points) || points.length < 2 || remaining <= 0) continue;
        const clean = points.slice(0, Math.min(remaining, 4096));
        remaining -= clean.length;
        // A malformed segment must not become a spurious bridge across a gap.
        let segment = [];
        const flush = () => {if (segment.length >= 2) projectedPaths.push(path(segment)); segment = [];};
        for (const p of clean) {
          if (point(p)) segment.push(projected(p[0], p[1], level, v));
          else flush();
        }
        flush();
      }
      if (projectedPaths.length) lines.push(`<path class="terrain-contour${level >= .7 ? ' terrain-contour-peak' : ''}" data-density="${fmt(level)}" d="${projectedPaths.join('')}" stroke-opacity="${fmt(.22 + .39 * level)}"/>`);
    }
    return `<g class="terrain-contours">${lines.join('')}</g>`;
  }

  function render(terrain, options = {}) {
    const grid = readGrid(terrain);
    if (!grid || !grid.values.some(value => value > 0)) return '';
    const v = view(options);
    const guides = options.referenceGrid === true ? referenceGridLines(terrain, v.mode) : [];
    const referenceGrid = guides.length ? `<g class="landscape-reference-grid" aria-hidden="true" pointer-events="none">${guides.map(points => `<path d="${path(points.map(p => projected(...p, v)))}"/>`).join('')}</g>` : '';
    return `<g class="map-terrain" aria-hidden="true" focusable="false" pointer-events="none">${v.mode === 'relief' ? floor(v) : ''}${surface(grid, v)}${referenceGrid}${options.contours === false ? '' : contours(terrain, v)}</g>`;
  }

  global.AtlasTerrain = Object.freeze({project, render, heightAt, referenceGridLines});
})(window);
