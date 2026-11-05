// Manual CSS audit (not part of `node --test tests/*.test.cjs`): node tests/css-audit.cjs [static-dir]
// Prints file count, total bytes, :root blocks, token count, hex literals outside :root and
// selectors that are defined in more than one file.
const fs = require('node:fs');
const path = require('node:path');

const dir = process.argv[2] || path.join(__dirname, '..', 'static');
const files = fs
  .readdirSync(dir)
  .filter((name) => name.endsWith('.css'))
  .sort();

function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, '');
}

function splitTopLevel(text) {
  const parts = [];
  let depth = 0,
    current = '';
  for (const ch of text) {
    if (ch === '(') depth++;
    else if (ch === ')') depth--;
    if (ch === ',' && depth === 0) {
      parts.push(current);
      current = '';
    } else current += ch;
  }
  parts.push(current);
  return parts;
}

// Walk the sheet and return {rules: [{selector, body}], rootBlocks: [body]}; nested at-rules recurse.
function parse(text) {
  const rules = [];
  const rootBlocks = [];
  const scan = (source, insideKeyframes) => {
    let i = 0;
    while (i < source.length) {
      const open = source.indexOf('{', i);
      if (open < 0) break;
      const prelude = source.slice(i, open).trim();
      let depth = 1,
        j = open + 1;
      while (j < source.length && depth) {
        if (source[j] === '{') depth++;
        else if (source[j] === '}') depth--;
        j++;
      }
      const body = source.slice(open + 1, j - 1);
      if (prelude.startsWith('@')) {
        if (/^@(?:media|supports|layer|container)/.test(prelude)) scan(body, insideKeyframes);
        else if (/^@keyframes/.test(prelude)) scan(body, true);
      } else if (!insideKeyframes) {
        if (/^:root$/.test(prelude)) rootBlocks.push(body);
        for (const selector of splitTopLevel(prelude)) {
          const normalized = selector.replace(/\s+/g, ' ').trim();
          if (normalized) rules.push({ selector: normalized, body });
        }
      }
      i = j;
    }
  };
  scan(stripComments(text), false);
  return { rules, rootBlocks };
}

let totalBytes = 0,
  rootBlocks = 0,
  tokens = 0,
  hexOutsideRoot = [];
const owners = new Map();
for (const name of files) {
  const text = fs.readFileSync(path.join(dir, name), 'utf8');
  totalBytes += Buffer.byteLength(text);
  const parsed = parse(text);
  rootBlocks += parsed.rootBlocks.length;
  for (const body of parsed.rootBlocks) tokens += (body.match(/--[\w-]+\s*:/g) || []).length;
  for (const { selector, body } of parsed.rules) {
    if (selector !== ':root') {
      for (const hex of body.match(/#[0-9a-f]{3,8}\b/gi) || []) hexOutsideRoot.push(name + ' ' + selector + ' ' + hex);
    }
    if (!owners.has(selector)) owners.set(selector, new Set());
    owners.get(selector).add(name);
  }
}
const shared = [...owners.entries()].filter(([, set]) => set.size > 1).sort((a, b) => a[0].localeCompare(b[0]));

console.log('files: ' + files.length);
console.log('total bytes: ' + totalBytes + ' (' + (totalBytes / 1024).toFixed(1) + ' KB)');
console.log(':root blocks: ' + rootBlocks);
console.log('tokens: ' + tokens);
console.log('hex literals outside :root: ' + hexOutsideRoot.length);
for (const line of hexOutsideRoot) console.log('  ' + line);
console.log('selectors defined in >1 file: ' + shared.length);
for (const [selector, set] of shared) console.log('  ' + selector + '  <- ' + [...set].join(', '));
