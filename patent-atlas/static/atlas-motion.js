/* Motion is a browser preference; it never changes the patent workspace. */
'use strict';
(function () {
  const storageKey = 'patent-atlas-motion';
  const choices = new Set(['system', 'dynamic', 'static']);
  const root = document.documentElement;
  const media = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)') : {matches: false};
  const running = new Map();
  let mode = 'system';
  try {
    const saved = localStorage.getItem(storageKey);
    if (choices.has(saved)) mode = saved;
  } catch { /* Private browsing can disable preference storage. */ }

  function enabled() { return mode !== 'static' && !media.matches; }
  function cancel(element) {
    const animation = running.get(element);
    if (!animation) return;
    running.delete(element);
    animation.cancel();
  }
  function cancelAll() { for (const element of [...running.keys()]) cancel(element); }

  function enter(element, options = {}) {
    // Rapid navigation replaces the visual effect, without delaying navigation.
    cancelAll();
    if (!element || !enabled() || typeof element.animate !== 'function') return;
    const direction = options.direction < 0 ? -1 : 1;
    const isStep = options.kind === 'step';
    const offset = isStep ? `${direction * 18}px, 0, 0` : `${direction * 8}px, 12px, 0`;
    let animation;
    try {
      animation = element.animate([
        {opacity: 0.28, transform: `translate3d(${offset})`},
        {opacity: 1, transform: 'translate3d(0, 0, 0)'},
      ], {duration: isStep ? 280 : 340, easing: 'cubic-bezier(.2,.75,.2,1)', fill: 'none'});
    } catch { return; /* A visual effect must never prevent navigation. */ }
    running.set(element, animation);
    const release = () => { if (running.get(element) === animation) running.delete(element); };
    animation.onfinish = release;
    animation.oncancel = release;
  }

  function apply() {
    root.dataset.atlasMotion = enabled() ? 'dynamic' : 'static';
    root.dataset.atlasMotionPreference = mode;
    if (!enabled()) cancelAll();
    globalThis.syncClassMotion?.();
    const select = document.getElementById('atlas-motion-mode');
    if (select) {
      select.value = mode;
      select.title = media.matches
        ? 'OSの「動きを減らす」設定に合わせて静止表示しています。'
        : mode === 'static' ? '画面の切り替えと分類の浮遊を停止します。'
        : 'タブ・ステップの短い切り替えと、分類の穏やかな浮遊を表示します。';
    }
  }
  function setMode(value) {
    mode = choices.has(value) ? value : 'system';
    try { localStorage.setItem(storageKey, mode); } catch { /* This session still works. */ }
    apply();
  }
  function init() {
    apply();
    document.getElementById('atlas-motion-mode')?.addEventListener('change', event => setMode(event.currentTarget.value));
  }

  window.PatentAtlasMotion = {enter, cancel, cancelAll, enabled, setMode};
  // Apply the preference before the first API response renders the workspace.
  apply();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
  if (typeof media.addEventListener === 'function') media.addEventListener('change', apply);
  else if (typeof media.addListener === 'function') media.addListener(apply);
})();
