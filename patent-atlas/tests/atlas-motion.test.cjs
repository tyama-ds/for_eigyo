const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/atlas-motion.js', 'utf8');

function setup({saved, reduced = false, storageBlocked = false} = {}) {
  const listeners = {};
  const select = {value: '', addEventListener(type, handler) { listeners[type] = handler; }};
  const dataset = {};
  const storage = [];
  const media = {matches: reduced, addEventListener(_type, handler) { this.changed = handler; }};
  const context = {
    document: {documentElement: {dataset}, readyState: 'complete', getElementById: () => select},
    matchMedia: () => media,
    localStorage: {
      getItem() { if (storageBlocked) throw Error('Disabled'); return saved; },
      setItem(key, value) { if (storageBlocked) throw Error('Disabled'); storage.push([key, value]); },
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return {api: context.PatentAtlasMotion, dataset, select, media, storage, listeners};
}

function surface() {
  return {effects: [], animate(frames, options) {
    const effect = {frames, options, cancelled: false, cancel() { this.cancelled = true; this.oncancel?.(); }};
    this.effects.push(effect);
    return effect;
  }};
}

test('static selection cancels an active effect and persists only a browser preference', () => {
  const s = setup();
  const node = surface();
  s.api.enter(node);
  assert.equal(node.effects.length, 1);
  s.listeners.change({currentTarget: {value: 'static'}});
  assert.equal(s.dataset.atlasMotion, 'static');
  assert.equal(node.effects[0].cancelled, true);
  s.api.enter(node);
  assert.equal(node.effects.length, 1);
  assert.deepEqual(s.storage, [['patent-atlas-motion', 'static']]);
});

test('OS reduced motion overrides dynamic and cancels active effects when changed', () => {
  const s = setup({saved: 'dynamic', reduced: true});
  const node = surface();
  assert.equal(s.select.value, 'dynamic');
  assert.equal(s.dataset.atlasMotion, 'static');
  s.api.enter(node);
  assert.equal(node.effects.length, 0);
  s.media.matches = false;
  s.media.changed();
  assert.equal(s.dataset.atlasMotion, 'dynamic');
  s.api.enter(node);
  s.media.matches = true;
  s.media.changed();
  assert.equal(node.effects[0].cancelled, true);
  assert.equal(s.storage.length, 0, 'OS changes do not replace the saved user choice');
});

test('repeated navigation cancels previous effects and backward steps use the opposite direction', () => {
  const s = setup();
  const first = surface(), second = surface();
  s.api.enter(first, {direction: 1, kind: 'step'});
  s.api.enter(second, {direction: -1, kind: 'step'});
  assert.equal(first.effects[0].cancelled, true);
  assert.match(second.effects[0].frames[0].transform, /-18px/);
  assert.equal(second.effects[0].options.fill, 'none');
  s.api.cancel(second);
  assert.equal(second.effects[0].cancelled, true);
});

test('saved static mode and blocked storage both work without relying on a server', () => {
  assert.equal(setup({saved: 'static'}).dataset.atlasMotion, 'static');
  const s = setup({storageBlocked: true});
  s.api.setMode('static');
  assert.equal(s.dataset.atlasMotion, 'static');
  s.api.setMode('unknown');
  assert.equal(s.dataset.atlasMotionPreference, 'system');
});

test('missing or unavailable browser animation support leaves navigation usable', () => {
  const s = setup();
  assert.doesNotThrow(() => s.api.enter(null));
  assert.doesNotThrow(() => s.api.enter({}));
  assert.doesNotThrow(() => s.api.enter({animate() { throw Error('Unsupported'); }}));
});
