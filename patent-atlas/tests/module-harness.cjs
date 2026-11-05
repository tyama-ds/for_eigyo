const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { JSDOM } = require('jsdom');
const { afterEach } = require('node:test');
const { registerHooks } = require('node:module');
const windows = new Set();
const registry = new Map();
const registryKey = Symbol.for('patent-atlas-test-imports');
globalThis[registryKey] = registry;
let serial = 0;

function createDom() {
  const dom = new JSDOM(fs.readFileSync('static/index.html', 'utf8'), {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.scrollTo = () => {};
  for (const name of [
    'window',
    'document',
    'localStorage',
    'sessionStorage',
    'DOMParser',
    'MutationObserver',
  ])
    globalThis[name] = name === 'window' ? w : w[name];
  globalThis.requestAnimationFrame = () => 0;
  globalThis.cancelAnimationFrame = () => {};
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  w.HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  windows.add(w);
  return dom;
}
afterEach(() => {
  for (const w of windows) w.close();
  windows.clear();
  registry.clear();
});

// Import the actual ES module. Only its declared dependency boundaries are mocked;
// no function bodies are sliced or evaluated, and the browser graph is exercised separately.
async function loadModule(context, filename, only) {
  const dom = context.testDom || (context.testDom = createDom());
  const url = pathToFileURL(path.resolve('static', filename)).href;
  const source = fs.readFileSync(path.resolve('static', filename), 'utf8');
  const mocks = {};
  for (const match of source.matchAll(/import\s+([^;]+?)\s+from\s+['"]([^'"]+)['"];?/g)) {
    const dependencyUrl = new URL(match[2], url).href;
    const real = await import(dependencyUrl);
    const mock = {};
    for (const [name, value] of Object.entries(real)) {
      if (name.endsWith('State') && value && typeof value === 'object') {
        mock[name] = new Proxy(value, {
          get: (target, key) => (Object.hasOwn(context, key) ? context[key] : target[key]),
          set: (_target, key, value) => {
            context[key] = value;
            return true;
          },
        });
      } else if (name === 'getState') mock[name] = () => context.state;
      else if (name === 'setState') mock[name] = (next) => (context.state = next);
      else if (typeof value === 'function') {
        mock[name] = (...args) => {
          const alias = name === 'render' ? match[1].match(/render\s+as\s+(\w+)/)?.[1] : null;
          if (alias && typeof context[alias] === 'function') return context[alias](...args);
          if (typeof context[name] === 'function') return context[name](...args);
          if (
            [
              'esc',
              'html',
              'safeUrl',
              'keyOf',
              'insidePolygon',
              'compute',
              'classificationKey',
              'classificationTitle',
              'judgmentLabel',
              'tooltipPosition',
              'bindLasso',
            ].includes(name)
          )
            return value(...args);
          if (name === 'patchRows') {
            const previous = real.getState();
            real.setState(context.state);
            try {
              return value(...args);
            } finally {
              real.setState(previous);
            }
          }
          if (name === 'debounce')
            return value(
              args[0],
              args[1],
              args[2] || {
                set: context.setTimeout || setTimeout,
                clear: context.clearTimeout || clearTimeout,
              }
            );
          if (name === 'replaceHtml') {
            if (args[0]?.ownerDocument) return value(...args);
            if (args[0]) args[0].innerHTML = args[1];
            return Object.assign(() => {}, { changed: true });
          }
          if (name === 'listen') {
            if (args[0]?.ownerDocument) return value(...args);
            return args[0]?.addEventListener(args[1], args[2], args[3]);
          }
          if (name === '$') return dom.window.document.querySelector(...args);
          if (name === '$$') return [...dom.window.document.querySelectorAll(...args)];
          if (name === 'run') return args[0];
          if (name === 'busy') return args[2]();
        };
      } else if (name === 'default' && value && typeof value === 'object') {
        const alias = {
          'classification-layout.js': 'PatentClassLayout',
          'classification-labels.js': 'PatentClassLabels',
        }[path.basename(match[2])];
        mock[name] = new Proxy(
          {},
          { get: (_target, key) => (context[alias]?.default || context[alias] || value)[key] }
        );
      } else mock[name] = context[name] ?? value;
    }
    mocks['../static/' + path.basename(match[2])] = mock;
  }
  mocks.import = {};
  for (const name of [
    'document',
    'window',
    'localStorage',
    'sessionStorage',
    'setTimeout',
    'clearTimeout',
    'setInterval',
    'clearInterval',
    'fetch',
    'DOMPoint',
    'performance',
    'requestAnimationFrame',
    'cancelAnimationFrame',
    'ResizeObserver',
    'Date',
    'FormData',
    'URL',
    'Blob',
    'MutationObserver',
  ])
    if (context[name] !== undefined)
      mocks.import[name] = [
        'fetch',
        'setTimeout',
        'clearTimeout',
        'setInterval',
        'clearInterval',
        'requestAnimationFrame',
        'cancelAnimationFrame',
      ].includes(name)
        ? (...args) => context[name](...args)
        : name === 'FormData'
          ? function (...args) {
              return new context.FormData(...args);
            }
          : context[name];
  const id = ++serial;
  const target = url + '?test=' + id;
  const globals = mocks.import;
  registry.set(id, { mocks, globals });
  const hooks = registerHooks({
    resolve(specifier, options, next) {
      if (options.parentURL === target && specifier.startsWith('./')) {
        const key = '../static/' + path.basename(specifier);
        const mock = mocks[key];
        if (mock) {
          const prefix = `const mock = globalThis[Symbol.for('patent-atlas-test-imports')].get(${id}).mocks[${JSON.stringify(key)}];\n`;
          const exports = Object.keys(mock)
            .map((name) =>
              name === 'default'
                ? 'export default mock.default;'
                : `export const ${name} = mock[${JSON.stringify(name)}];`
            )
            .join('\n');
          return { url: 'data:text/javascript,' + encodeURIComponent(prefix + exports), shortCircuit: true };
        }
      }
      return next(specifier, options);
    },
    load(location, options, next) {
      const loaded = next(location, options);
      if (location !== target || !Object.keys(globals).length) return loaded;
      const injected = `const {${Object.keys(globals).join(',')}} = globalThis[Symbol.for('patent-atlas-test-imports')].get(${id}).globals;\n`;
      return { ...loaded, source: injected + loaded.source.toString() };
    },
  });
  let module;
  try {
    module = await import(target);
  } finally {
    hooks.deregister();
  }
  for (const [name, value] of Object.entries(module)) {
    if (name.endsWith('State') && value && typeof value === 'object') {
      for (const key of Object.keys(value)) {
        if (Object.hasOwn(context, key)) value[key] = context[key];
        Object.defineProperty(context, key, {
          configurable: true,
          get: () => value[key],
          set: (v) => {
            value[key] = v;
          },
        });
      }
    } else if (!only || only.includes(name) || typeof value !== 'function') context[name] = value;
  }
  return module;
}
module.exports = { createDom, loadModule };
