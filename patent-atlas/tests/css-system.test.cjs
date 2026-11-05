const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const sheets = Object.fromEntries(
  fs
    .readdirSync('static')
    .filter((name) => name.endsWith('.css'))
    .map((name) => [name, fs.readFileSync(path.join('static', name), 'utf8')])
);
const all = Object.values(sheets).join('\n');

test('light theme has one token source and colors are referenced outside that source', () => {
  assert.equal((all.match(/:root\s*\{/g) || []).length, 1);
  assert.match(sheets['atlas-theme.css'], /:root\s*\{\s*color-scheme:\s*light;/);
  assert.doesNotMatch(all, /color-scheme:\s*dark/);
  for (const [name, text] of Object.entries(sheets)) {
    const withoutRoot = text.replace(/:root\s*\{[^}]*\}/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const block of withoutRoot.matchAll(/\{([^{}]*)\}/g)) {
      for (const declaration of block[1].split(';')) {
        const value = declaration.slice(declaration.indexOf(':') + 1).replace(/var\([^)]*\)/g, '');
        assert.doesNotMatch(
          value,
          /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(|\b(?:white|black|transparent)\b/i,
          name + ': ' + declaration.trim()
        );
      }
    }
  }
});

test('spacing and radius scales are declared once and used by components', () => {
  for (const name of [
    ...Array.from({ length: 6 }, (_, i) => 'space-' + (i + 1)),
    'radius-s',
    'radius-m',
    'radius-l',
  ]) {
    assert.equal((all.match(new RegExp('--' + name + '\\s*:', 'g')) || []).length, 1, name);
    assert.match(sheets['atlas-theme.css'], new RegExp('--' + name + '\\s*:'));
    assert.ok(all.includes('var(--' + name + ')'), name);
  }
});

test('hidden and motion overrides are shared instead of copied into feature sheets', () => {
  assert.equal((all.match(/\[hidden\]/g) || []).length, 1);
  assert.match(sheets['style.css'], /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  for (const [name, text] of Object.entries(sheets)) {
    if (name === 'style.css') assert.equal((text.match(/!important/g) || []).length, 1);
    else if (name !== 'atlas-motion.css') assert.doesNotMatch(text, /!important/, name);
  }
  assert.match(sheets['atlas-motion.css'], /html\[data-atlas-motion='static'\]/);
  assert.match(sheets['atlas-motion.css'], /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
});

test('shared controls and empty-state have a single base definition', () => {
  for (const className of ['button', 'pill', 'caption', 'notice', 'empty-state']) {
    const definition = new RegExp('(?:^|\\})\\s*\\.' + className + '\\s*\\{', 'g');
    const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '');
    assert.equal((stripComments(all).match(definition) || []).length, 1, className);
    assert.match(stripComments(sheets['style.css']), definition);
  }
  for (const name of fs.readdirSync('static').filter((name) => /\.(?:css|js|html)$/.test(name))) {
    assert.doesNotMatch(
      fs.readFileSync(path.join('static', name), 'utf8'),
      /\b(?:discovery-empty|convergence-empty|ql-empty|invention-empty|empty-orbit)\b/,
      name
    );
  }
});

test('index loads the shared CSS once and has no per-file cachebuster versions', () => {
  const index = fs.readFileSync('static/index.html', 'utf8');
  assert.doesNotMatch(index, /[?&]v=/);
  const styles = [...index.matchAll(/href="\/static\/([^"?]+\.css)"/g)].map((m) => m[1]);
  assert.equal(new Set(styles).size, styles.length);
  assert.deepEqual([...styles].sort(), Object.keys(sheets).sort());
  assert.equal(styles[0], 'style.css');
});
