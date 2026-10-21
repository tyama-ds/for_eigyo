import { renderPage, extractBlocks } from './pdf-engine.js';

const MAX_PAGE_AREA = 2_000_000;

function abortError() {
  const error = new Error('PDFは既に閉じられています。もう一度読み込んでください。');
  error.name = 'AbortError';
  return error;
}

function clearCanvas(state) {
  if (!state.canvas) return;
  state.canvas.width = 1;
  state.canvas.height = 1;
  state.canvas = null;
}

function cleanupPage(page) {
  // PDF.js may defer cleanup while its page is rendering. The load's finally
  // path calls this again after the render/extraction has settled.
  try { page?.cleanup?.(); } catch { /* Document destruction also frees these resources. */ }
}

/**
 * Keep page dimensions and editable text in memory, but render a page only when
 * it is needed. Call releasePage after leaving/exporting a page. Page references
 * and the pages array stay stable, so pending translations keep their targets.
 * Numeric arguments to ensurePage/releasePage are zero-based page indices.
 */
export async function createPageStore(doc, {
  onProgress,
  onPageLoaded,
  scale = 1.5,
  engine = { renderPage, extractBlocks },
} = {}) {
  if (!Number.isInteger(doc?.numPages) || doc.numPages < 1) {
    throw new Error('PDFのページ数を確認できません。');
  }
  const pages = [];
  try {
    for (let pageNumber = 1; pageNumber <= doc.numPages; pageNumber++) {
      const page = await doc.getPage(pageNumber);
      const viewport = page.getViewport({ scale: 1 });
      const { width, height } = viewport;
      if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
        cleanupPage(page);
        throw new Error('PDFのページサイズを確認できません。');
      }
      if (width * height > MAX_PAGE_AREA) {
        cleanupPage(page);
        throw new Error('ページの寸法が大きすぎます。通常の用紙サイズのPDFを使用してください。');
      }
      pages.push({ pageNumber, page, viewport, width, height,
        blocks: [], originalBlocks: [], loaded: false, canvas: null,
        renderScale: scale, warnings: [] });
      cleanupPage(page);
      onProgress?.(pageNumber, doc.numPages);
    }
  } catch (error) {
    pages.forEach(state => cleanupPage(state.page));
    // Ownership of doc stays with the caller if construction fails.
    throw error;
  }

  const members = new Set(pages);
  const pending = new Map();
  let disposed = false;
  let disposePromise;

  function resolvePage(target) {
    const state = typeof target === 'number' ? pages[target] : target;
    if (!members.has(state)) throw new Error('PDFのページが見つかりません。');
    return state;
  }

  // Deliberately not async: simultaneous callers receive the very same promise.
  function ensurePage(target) {
    if (disposed) return Promise.reject(abortError());
    let state;
    try { state = resolvePage(target); } catch (error) { return Promise.reject(error); }
    const existing = pending.get(state);
    if (existing) {
      existing.releaseRequested = false;
      return existing.promise;
    }
    if (state.canvas) return Promise.resolve(state);
    const job = { releaseRequested: false, promise: null };
    pending.set(state, job);
    // Starting in a microtask makes the pending entry available even if an
    // injected renderer throws synchronously before its first await.
    job.promise = Promise.resolve().then(async () => {
      let rendered;
      try {
        if (disposed) throw abortError();
        rendered = await engine.renderPage(doc, state.pageNumber, scale);
        if (disposed) throw abortError();
        if (!state.loaded) {
          const blocks = await engine.extractBlocks(rendered.page, rendered.viewport, rendered.canvas);
          if (disposed) throw abortError();
          blocks.forEach((block, index) => { block.id = `p${state.pageNumber}-b${index + 1}`; });
          state.originalBlocks = structuredClone(blocks);
          state.blocks = blocks;
          state.loaded = true;
          onPageLoaded?.(state);
        }
        state.page = rendered.page;
        state.viewport = rendered.viewport;
        state.renderScale = rendered.renderScale;
        state.canvas = rendered.canvas;
        rendered = null; // Ownership of the allocated bitmap has transferred.
        return state;
      } catch (error) {
        if (disposed) throw abortError();
        throw error;
      } finally {
        if (rendered) clearCanvas(rendered);
        pending.delete(state);
        if (disposed || job.releaseRequested) clearCanvas(state);
        if (disposed || job.releaseRequested || !state.canvas) cleanupPage(state.page);
      }
    });
    return job.promise;
  }

  function releasePage(target) {
    const state = resolvePage(target);
    const job = pending.get(state);
    if (job) job.releaseRequested = true;
    clearCanvas(state);
    if (!job) cleanupPage(state.page);
  }

  function dispose() {
    if (disposePromise) return disposePromise;
    disposed = true;
    pages.forEach(releasePage);
    disposePromise = (async () => {
      let destructionError;
      try {
        if (doc.loadingTask?.destroy) await doc.loadingTask.destroy();
        else await doc.destroy?.();
      } catch (error) { destructionError = error; }
      await Promise.allSettled([...pending.values()].map(job => job.promise));
      pages.forEach(state => { clearCanvas(state); cleanupPage(state.page); });
      if (destructionError) throw destructionError;
    })();
    return disposePromise;
  }

  return { pages, ensurePage, releasePage, dispose };
}
