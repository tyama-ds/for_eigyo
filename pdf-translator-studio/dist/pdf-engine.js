/**
 * PDF layout engine. Coordinates are page-viewport points (scale = 1).
 * PDF.js supplies the original artwork and text geometry. Replacement text is
 * fitted into editable rectangles. Export is deliberately rasterized: it does
 * not preserve PDF text objects, tags, hyperlinks, signatures, or form fields.
 * Reference: https://mozilla.github.io/pdf.js/api/draft/module-pdfjsLib.html
 */

let pdfModulePromise;
let pdfModuleFailures = 0;
const FONT_SANS = 'Arial, "Noto Sans JP", "Yu Gothic", Meiryo, sans-serif';
const FONT_SERIF = 'Georgia, "Noto Serif JP", "Yu Mincho", serif';
const FONT_MONO = 'Consolas, "Noto Sans Mono", "Yu Gothic", monospace';
// One canvas uses at most ~48 MB of RGBA storage, independent of PDF page size.
// The app limits resident page canvases and releases them when pages are evicted.
const MAX_CANVAS_PIXELS = 12_000_000;

async function pdfLibrary() {
  if (!pdfModulePromise) {
    const url = new URL('./vendor/pdf.mjs', import.meta.url);
    // Browsers also cache a failed module import by URL. Only a retry needs a
    // fresh URL; keep the normal request and successful module cache unchanged.
    if (pdfModuleFailures) url.searchParams.set('retry', String(pdfModuleFailures));
    pdfModulePromise = import(url.href).then(pdfjs => {
      pdfjs.GlobalWorkerOptions.workerSrc = new URL('./vendor/pdf.worker.mjs', import.meta.url).href;
      return pdfjs;
    }).catch(error => {
      pdfModulePromise = undefined;
      pdfModuleFailures++;
      throw new Error('PDF読み込み用ライブラリを取得できません。ローカルサーバーを起動した状態で、PDFをもう一度選択するか、サンプルを再度開いてください。', { cause: error });
    });
  }
  return pdfModulePromise;
}

export async function loadPdf(arrayBuffer, { password } = {}) {
  const pdfjs = await pdfLibrary();
  // Keep the caller's data usable when PDF.js transfers its bytes to a worker.
  const bytes = arrayBuffer instanceof Uint8Array
    ? arrayBuffer.slice() : new Uint8Array(arrayBuffer.slice(0));
  const loadingTask = pdfjs.getDocument({
    data: bytes,
    password,
    isEvalSupported: false,
    useSystemFonts: true,
    fontExtraProperties: true,
    cMapUrl: new URL('./vendor/cmaps/', import.meta.url).href,
    cMapPacked: true,
    standardFontDataUrl: new URL('./vendor/standard_fonts/', import.meta.url).href,
    wasmUrl: new URL('./vendor/wasm/', import.meta.url).href,
  });
  try {
    return await loadingTask.promise;
  } catch (error) {
    // Rejected loads still own a worker (including a missing/incorrect password).
    // Preserve the actionable load error even if worker teardown also fails.
    try { await loadingTask.destroy(); } catch { /* The original error is retained. */ }
    throw error;
  }
}

function canvasFor(width, height, requestedScale) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error('PDF のページサイズを確認できません。');
  }
  const scale = Math.min(Math.max(Number(requestedScale) || 1, 0.1),
    Math.sqrt(MAX_CANVAS_PIXELS / (width * height)), 16384 / width, 16384 / height);
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.ceil(width * scale));
  canvas.height = Math.max(1, Math.ceil(height * scale));
  return { canvas, scale };
}

export async function renderPage(doc, pageNumber, scale = 1.5) {
  const page = await doc.getPage(pageNumber);
  const viewport = page.getViewport({ scale: 1 });
  const rendered = canvasFor(viewport.width, viewport.height, scale);
  try {
    await page.render({
      canvasContext: rendered.canvas.getContext('2d', { willReadFrequently: true }),
      viewport: page.getViewport({ scale: rendered.scale }),
      background: '#ffffff',
    }).promise;
  } catch (error) {
    rendered.canvas.width = 1; rendered.canvas.height = 1;
    throw error;
  }
  return {
    pageNumber, page, viewport,
    width: viewport.width, height: viewport.height,
    canvas: rendered.canvas, renderScale: rendered.scale,
    blocks: [], warnings: [],
  };
}

export function multiplyTransforms(a, b) {
  return [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
    a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
    a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];
}

const hex = rgb => '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

function colorBuckets(pixels) {
  const buckets = new Map();
  for (const rgb of pixels) {
    const key = rgb.map(v => Math.round(v / 8)).join(',');
    const bucket = buckets.get(key) || { count: 0, sum: [0, 0, 0] };
    bucket.count++;
    for (let c = 0; c < 3; c++) bucket.sum[c] += rgb[c];
    buckets.set(key, bucket);
  }
  return [...buckets.values()].map(b => ({ count: b.count, rgb: b.sum.map(v => v / b.count) }))
    .sort((a, b) => b.count - a.count);
}

function sampleAppearance(image, rect, pageWidth, pageHeight) {
  if (!image) return { background: '#ffffff', color: '#202b38', backgroundComplex: false };
  const sx = image.width / pageWidth, sy = image.height / pageHeight;
  const x0 = Math.max(0, Math.floor(rect.x * sx));
  const y0 = Math.max(0, Math.floor(rect.y * sy));
  const x1 = Math.min(image.width - 1, Math.ceil((rect.x + rect.width) * sx));
  const y1 = Math.min(image.height - 1, Math.ceil((rect.y + rect.height) * sy));
  const pixel = (x, y) => {
    const offset = (Math.max(0, Math.min(image.height - 1, y)) * image.width
      + Math.max(0, Math.min(image.width - 1, x))) * 4;
    return [image.data[offset], image.data[offset + 1], image.data[offset + 2]];
  };
  const rim = [], inside = [];
  const stepX = Math.max(1, Math.floor((x1 - x0) / 80));
  for (let x = x0; x <= x1; x += stepX) {
    rim.push(pixel(x, y0 - 1), pixel(x, y1 + 1));
  }
  for (let y = y0; y <= y1; y += 2) {
    rim.push(pixel(x0 - 1, y), pixel(x1 + 1, y));
  }
  const step = Math.max(1, Math.floor(Math.sqrt(Math.max(1, (x1 - x0) * (y1 - y0)) / 1600)));
  for (let y = y0; y <= y1; y += step) {
    for (let x = x0; x <= x1; x += step) inside.push(pixel(x, y));
  }
  const rimBuckets = colorBuckets(rim);
  const background = rimBuckets[0]?.rgb || [255, 255, 255];
  const backgroundShare = rim.filter(rgb => distance(rgb, background) < 25).length / Math.max(1, rim.length);
  const ink = colorBuckets(inside.filter(rgb => distance(rgb, background) > 60));
  // Prefer a sufficiently frequent extreme color over antialiased edge pixels.
  const minCount = Math.max(1, (ink[0]?.count || 0) * 0.18);
  const foreground = ink.filter(b => b.count >= minCount)
    .sort((a, b) => distance(b.rgb, background) - distance(a.rgb, background))[0]?.rgb
    || (background.reduce((a, b) => a + b, 0) > 380 ? [32, 43, 56] : [255, 255, 255]);
  return { background: hex(background), color: hex(foreground), backgroundComplex: backgroundShare < 0.78 };
}

/** Derive screen-space rectangles from the PDF.js TextContent public format. */
export function textItemsToBlocks(items, styles, viewport, { pageNumber = 1, fontNames = {} } = {}) {
  const scale = viewport.scale || 1;
  return items.filter(item => typeof item.str === 'string' && item.str.trim()).map((item, index) => {
    const style = styles[item.fontName] || {};
    const tx = multiplyTransforms(viewport.transform, item.transform);
    const fontSize = Math.max(1, Math.hypot(tx[2], tx[3]) / scale);
    const ascent = Number.isFinite(style.ascent) ? style.ascent : 0.85;
    const descent = Number.isFinite(style.descent) ? style.descent : -0.22;
    const angle = Math.atan2(tx[1], tx[0]);
    const rotation = angle * 180 / Math.PI;
    const supported = !style.vertical && item.dir !== 'ttb' && Math.abs(rotation) < 0.5
      && Math.abs(tx[2]) < Math.max(0.08, fontSize * scale * 0.03);
    // /UserUnit changes the physical size of PDF coordinates independently of
    // the caller's viewport zoom. Width is not part of the text transform.
    const width = Math.max(0.5, Math.abs(item.width) * (viewport.userUnit || 1));
    const localTop = -ascent * fontSize;
    const localBottom = Math.max(-descent * fontSize, fontSize * 0.15);
    const origin = [tx[4] / scale, tx[5] / scale];
    const corners = [[0, localTop], [width, localTop], [0, localBottom], [width, localBottom]]
      .map(([x, y]) => [origin[0] + x * Math.cos(angle) - y * Math.sin(angle),
        origin[1] + x * Math.sin(angle) + y * Math.cos(angle)]);
    const xs = corners.map(p => p[0]), ys = corners.map(p => p[1]);
    const x = Math.min(...xs), y = Math.min(...ys);
    const actualName = fontNames[item.fontName] || style.fontFamily || '';
    const family = style.fontFamily || '';
    const fontFamily = /mono|courier/i.test(family + actualName) ? FONT_MONO
      : /serif|times|georgia/i.test(family + actualName) && !/sans/i.test(family) ? FONT_SERIF : FONT_SANS;
    const block = {
      id: `p${pageNumber}-b${index + 1}`, original: item.str, translation: '',
      x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y,
      baseline: origin[1], fontSize, sourceFontSize: fontSize, fontFamily,
      fontWeight: /bold|black|heavy|demi/i.test(actualName) ? '700' : '400',
      fontStyle: /italic|oblique/i.test(actualName) ? 'italic' : 'normal',
      sourceFont: actualName, sourceFontKey: item.fontName, direction: item.dir || 'ltr',
      rotation, supported, enabled: supported, color: '#202b38', background: '#ffffff',
      backgroundComplex: false, fit: null,
      warning: supported ? '' : '回転・縦書き・斜体変形の文字です。自動置換の対象外です。',
      _hasEOL: !!item.hasEOL,
    };
    block.sourceRect = { x: block.x, y: block.y, width: block.width, height: block.height };
    return block;
  });
}

/** Merge adjacent same-style runs only; large gaps keep table cells separate. */
export function mergeTextRuns(blocks) {
  const result = [];
  const ordered = [...blocks].sort((a, b) => a.y - b.y || a.x - b.x);
  for (const current of ordered) {
    const candidate = result.findLast(previous => {
      const gap = current.x - (previous.x + previous.width);
      return previous.supported && current.supported && !previous._hasEOL
        && previous.direction === current.direction && current.direction !== 'rtl'
        && previous.sourceFontKey === current.sourceFontKey
        && Math.abs(previous.fontSize - current.fontSize) < 0.15
        && Math.abs(previous.baseline - current.baseline) < current.fontSize * 0.16
        && previous.color === current.color && previous.background === current.background
        && gap >= -0.4 && gap <= current.fontSize * 0.8;
    });
    if (!candidate) { result.push({ ...current, sourceRect: { ...current.sourceRect } }); continue; }
    const gap = current.x - (candidate.x + candidate.width);
    const spacer = gap > current.fontSize * 0.12 && !/\s$/.test(candidate.original) && !/^\s/.test(current.original) ? ' ' : '';
    candidate.original += spacer + current.original;
    const right = Math.max(candidate.x + candidate.width, current.x + current.width);
    const bottom = Math.max(candidate.y + candidate.height, current.y + current.height);
    candidate.y = Math.min(candidate.y, current.y);
    candidate.width = right - candidate.x;
    candidate.height = bottom - candidate.y;
    candidate.sourceRect = { x: candidate.x, y: candidate.y, width: candidate.width, height: candidate.height };
    candidate._hasEOL = current._hasEOL;
    candidate.backgroundComplex ||= current.backgroundComplex;
  }
  return result.sort((a, b) => a.y - b.y || a.x - b.x);
}

export async function extractBlocks(page, viewport, canvas) {
  const content = await page.getTextContent();
  const fontNames = {};
  for (const key of Object.keys(content.styles)) {
    try { fontNames[key] = page.commonObjs.get(key)?.name || ''; } catch { /* Font names are optional hints. */ }
  }
  let image;
  if (canvas) {
    try { image = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height); } catch { /* Keep safe fallback colors. */ }
  }
  const pageWidth = viewport.width / (viewport.scale || 1);
  const pageHeight = viewport.height / (viewport.scale || 1);
  const runs = textItemsToBlocks(content.items, content.styles, viewport, { pageNumber: page.pageNumber, fontNames });
  for (const block of runs) {
    Object.assign(block, sampleAppearance(image, block, pageWidth, pageHeight));
    if (block.backgroundComplex && block.supported) {
      block.warning = '背景に色や図柄の変化があります。置換箇所の見た目を確認してください。';
    }
  }
  return mergeTextRuns(runs);
}

function graphemes(text) {
  if (typeof Intl.Segmenter === 'function') {
    return [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text)].map(x => x.segment);
  }
  return Array.from(text);
}

function wordTokens(text) {
  if (typeof Intl.Segmenter === 'function') {
    return [...new Intl.Segmenter(undefined, { granularity: 'word' }).segment(text)].map(x => x.segment);
  }
  return text.match(/[A-Za-z0-9_]+|\s+|[^\s]/gu) || [];
}

export function wrapText(text, maxWidth, measure) {
  const lines = [];
  for (const paragraph of String(text).replace(/\r\n?/g, '\n').split('\n')) {
    let line = '';
    for (const token of wordTokens(paragraph)) {
      if (!line && /^\s+$/.test(token)) continue;
      if (measure(line + token) <= maxWidth + 0.001) { line += token; continue; }
      if (line) { lines.push(line.trimEnd()); line = ''; }
      if (/^\s+$/.test(token)) continue;
      if (measure(token) <= maxWidth + 0.001) { line = token; continue; }
      for (const char of graphemes(token)) {
        if (line && measure(line + char) > maxWidth + 0.001) { lines.push(line); line = ''; }
        line += char;
      }
    }
    lines.push(line.trimEnd());
  }
  return lines;
}

export function fontString(block, size) {
  return `${block.fontStyle || 'normal'} ${block.fontWeight || '400'} ${size}px ${block.fontFamily || FONT_SANS}`;
}

/** Fit using actual canvas text metrics; never silently squeeze glyph widths. */
export function fitText(context, text, block) {
  const requestedSize = Math.max(1, Number(block.fontSize) || 12);
  const minimumSize = Math.min(requestedSize, Math.max(4, requestedSize * 0.45));
  const width = Math.max(0.1, Number(block.width) || 0);
  const height = Math.max(0.1, Number(block.height) || 0);
  const evaluate = size => {
    context.font = fontString(block, size);
    const lines = wrapText(text, width, value => context.measureText(value).width);
    const probe = context.measureText(lines.join('') || 'Ag');
    const ascent = Math.max(size * 0.72, probe.actualBoundingBoxAscent || size * 0.8);
    const descent = Math.max(size * 0.15, probe.actualBoundingBoxDescent || size * 0.2);
    const lineHeight = size * 1.15;
    const neededHeight = ascent + descent + Math.max(0, lines.length - 1) * lineHeight;
    const widest = Math.max(0, ...lines.map(line => context.measureText(line).width));
    return { fontSize: size, lines, lineHeight, ascent, descent, neededHeight,
      overflow: neededHeight > height + 0.03 || widest > width + 0.03,
      shrunk: size < requestedSize - 0.1 };
  };
  const initial = evaluate(requestedSize);
  if (!initial.overflow) return initial;
  let low = minimumSize, high = requestedSize;
  let best = evaluate(low);
  if (best.overflow) return best;
  for (let i = 0; i < 13; i++) {
    const mid = (low + high) / 2;
    const candidate = evaluate(mid);
    if (candidate.overflow) high = mid;
    else { low = mid; best = candidate; }
  }
  return best;
}

function isTranslated(block) {
  return block.enabled !== false && block.supported !== false && String(block.translation || '').trim().length > 0;
}

export async function renderTranslatedCanvas(pageState, { scale = 2, showBoxes = false } = {}) {
  const { width, height, page, blocks = [] } = pageState;
  const rendered = canvasFor(width, height, scale);
  const context = rendered.canvas.getContext('2d');
  try {
  // Re-render for export sharpness; never scale a small preview up for output.
  if (page) {
    await page.render({ canvasContext: context, viewport: page.getViewport({ scale: rendered.scale }), background: '#ffffff' }).promise;
  } else if (pageState.canvas) {
    context.drawImage(pageState.canvas, 0, 0, rendered.canvas.width, rendered.canvas.height);
  } else throw new Error('ページ画像を読み込めません。');
  if (document.fonts?.ready) await document.fonts.ready;
  context.save();
  context.scale(rendered.scale, rendered.scale);

  // Cover original text at its immutable source location. Geometry edits move
  // only the replacement; they must not accidentally leave the old text visible.
  for (const block of blocks) {
    if (!isTranslated(block)) { block.fit = null; continue; }
    const rect = block.sourceRect || block;
    context.fillStyle = block.background || '#ffffff';
    // PDF ascent/descent metrics can understate raster ink bounds (especially
    // substituted standard fonts). Include a small em-relative safety margin
    // so the original cap-height strokes do not remain as ghost text.
    const padY = Math.max(1.2, (block.sourceFontSize || block.fontSize) * 0.14);
    context.fillRect(rect.x - 0.7, rect.y - padY, rect.width + 1.4, rect.height + padY * 2);
  }
  for (const block of blocks) {
    if (isTranslated(block)) {
      const fit = fitText(context, String(block.translation), block);
      block.fit = fit;
      context.save();
      context.beginPath();
      context.rect(block.x, block.y, block.width, block.height);
      context.clip();
      context.font = fontString(block, fit.fontSize);
      context.fillStyle = block.color || '#202b38';
      context.textBaseline = 'alphabetic';
      const rtl = /^[\s\d\p{P}]*[\u0590-\u08ff]/u.test(block.translation);
      context.direction = rtl ? 'rtl' : 'ltr';
      context.textAlign = rtl ? 'right' : 'left';
      const x = rtl ? block.x + block.width : block.x;
      const baseline = block.y + fit.ascent;
      fit.lines.forEach((line, index) => context.fillText(line, x, baseline + index * fit.lineHeight));
      context.restore();
    }
    if (showBoxes) {
      context.save();
      context.strokeStyle = block.supported === false ? '#a0a6b2' : block.fit?.overflow ? '#d85839' : '#23866f';
      context.lineWidth = 0.6;
      context.setLineDash(block.supported === false ? [3, 3] : []);
      context.strokeRect(block.x, block.y, block.width, block.height);
      context.restore();
    }
  }
  context.restore();
  return rendered.canvas;
  } catch (error) {
    rendered.canvas.width = 1;
    rendered.canvas.height = 1;
    throw error;
  }
}

function jsPdfClass() {
  const jsPDF = globalThis.jspdf?.jsPDF;
  if (!jsPDF) throw new Error('PDF 出力ライブラリを読み込めません。ページを再読み込みしてください。');
  return jsPDF;
}

/** preparePage/releasePage let large documents keep only one export page resident. */
export async function exportPdf(pages, { onProgress = () => {}, preparePage, releasePage } = {}) {
  if (!pages.length) throw new Error('出力するページがありません。');
  const jsPDF = jsPdfClass();
  let pdf;
  for (let i = 0; i < pages.length; i++) {
    let state = pages[i], canvas, failed = false;
    try {
      if (preparePage) state = await preparePage(state) || state;
      canvas = await renderTranslatedCanvas(state, { scale: 2.25 });
      const overflowing = (state.blocks || []).filter(b => isTranslated(b) && b.fit?.overflow);
      if (overflowing.length) {
        const error = new Error(`${state.pageNumber} ページに、枠に収まらない訳文が ${overflowing.length} 件あります。訳文または枠のサイズを調整してください。`);
        error.code = 'TEXT_OVERFLOW';
        error.blockIds = overflowing.map(b => b.id);
        throw error;
      }
      if (!pdf) {
        pdf = new jsPDF({ unit: 'pt', format: [state.width, state.height],
          orientation: state.width > state.height ? 'landscape' : 'portrait', compress: true });
        pdf.setProperties({ title: 'Translated document', creator: 'PDF Translator Studio' });
      } else pdf.addPage([state.width, state.height], state.width > state.height ? 'landscape' : 'portrait');
      pdf.addImage(canvas, 'PNG', 0, 0, state.width, state.height, undefined, 'FAST');
    } catch (error) {
      failed = true;
      throw error;
    } finally {
      if (canvas) { canvas.width = 1; canvas.height = 1; }
      // Preserve the useful render/overflow error even if resource cleanup fails.
      try { if (releasePage) await releasePage(state); } catch (error) { if (!failed) throw error; }
    }
    onProgress(i + 1, pages.length);
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  return pdf.output('blob');
}

/** A compact designed sample, with genuine selectable PDF text. */
export function createDemoPdf() {
  const jsPDF = jsPdfClass();
  const pdf = new jsPDF({ unit: 'pt', format: 'a4', compress: true });
  const W = 595.28, H = 841.89;
  pdf.setFillColor('#fbfaf7'); pdf.rect(0, 0, W, H, 'F');
  pdf.setFillColor('#1e5c51'); pdf.rect(0, 0, W, 190, 'F');
  pdf.setTextColor('#c4e8d9'); pdf.setFont('helvetica', 'bold'); pdf.setFontSize(9);
  pdf.text('FIELDNOTES / RESEARCH BRIEF', 44, 42);
  pdf.setTextColor('#ffffff'); pdf.setFontSize(29);
  pdf.text('Design across languages', 44, 94);
  pdf.setFont('helvetica', 'normal'); pdf.setFontSize(12);
  pdf.text('Keep the structure. Make the message travel.', 44, 126);
  pdf.setFontSize(9); pdf.setTextColor('#c4e8d9'); pdf.text('OCTOBER 2026', 44, 162);
  pdf.setTextColor('#1c302c'); pdf.setFont('helvetica', 'bold'); pdf.setFontSize(18);
  pdf.text('A document is more than its words.', 44, 232);
  pdf.setFont('helvetica', 'normal'); pdf.setFontSize(11); pdf.setTextColor('#43534e');
  pdf.text('Every page carries a visual rhythm: a heading, a quiet margin,', 44, 261);
  pdf.text('an image, and the precise relationship between them.', 44, 279);
  pdf.text('Translation should respect that rhythm.', 44, 297);
  pdf.setFillColor('#e8eee7'); pdf.roundedRect(44, 329, 507, 92, 8, 8, 'F');
  pdf.setTextColor('#28604c'); pdf.setFont('helvetica', 'bold'); pdf.setFontSize(9);
  pdf.text('THE APPROACH', 62, 353);
  pdf.setTextColor('#24382f'); pdf.setFont('helvetica', 'normal'); pdf.setFontSize(12);
  pdf.text('Translate the text. Preserve the composition.', 62, 379);
  pdf.setFontSize(10); pdf.text('Review each region before exporting your translated PDF.', 62, 401);
  pdf.setFont('helvetica', 'bold'); pdf.setFontSize(15); pdf.setTextColor('#1c302c');
  pdf.text('Built for a careful review', 44, 464);
  const tableX = 44, colX = 287, rowH = 38, tableY = 488;
  pdf.setFillColor('#dce7df'); pdf.rect(tableX, tableY, 507, rowH, 'F');
  pdf.setFontSize(9); pdf.text('ELEMENT', 58, tableY + 24); pdf.text('WHAT TO CHECK', colX, tableY + 24);
  const rows = [['Page geometry', 'Size, spacing, and alignment'], ['Typography', 'Font size and text fit'], ['Visual context', 'Images, rules, and background colors']];
  pdf.setFont('helvetica', 'normal'); pdf.setFontSize(10);
  rows.forEach((row, i) => {
    const y = tableY + rowH * (i + 1);
    pdf.setDrawColor('#d7ddd5'); pdf.setLineWidth(0.5); pdf.line(tableX, y + rowH, 551, y + rowH);
    pdf.text(row[0], 58, y + 24); pdf.text(row[1], colX, y + 24);
  });
  pdf.setFillColor('#e9d7ad'); pdf.circle(505, 714, 27, 'F');
  pdf.setFillColor('#96b49b'); pdf.circle(480, 735, 19, 'F');
  pdf.setFontSize(10); pdf.setTextColor('#65736b'); pdf.text('A little care makes every language feel at home.', 44, 720);
  pdf.setDrawColor('#cdd8ce'); pdf.line(44, 777, 551, 777);
  pdf.setFontSize(8); pdf.text('FIELDNOTES STUDIO', 44, 798); pdf.text('01 / 01', 515, 798);
  return pdf.output('arraybuffer');
}
