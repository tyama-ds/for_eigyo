// Usage: node tests/compare-css.cjs BASELINE_DIR CURRENT_DIR REPORT_DIR
// Optional development tools pngjs/pixelmatch must be available through NODE_PATH.
const fs = require('node:fs'),
  path = require('node:path');
const { PNG } = require('pngjs');
(async () => {
  const pixelmatch = (await import(require('node:url').pathToFileURL(require.resolve('pixelmatch')).href))
    .default;
  const [before, after, out] = process.argv.slice(2);
  if (!before || !after || !out) throw new Error('Specify baseline, current and report directories');
  fs.mkdirSync(out, { recursive: true });
  const rows = [];
  for (const file of fs.readdirSync(before).filter((n) => n.endsWith('.styles.json'))) {
    const name = file.replace('.styles.json', ''),
      a = JSON.parse(fs.readFileSync(path.join(before, file))),
      b = JSON.parse(fs.readFileSync(path.join(after, file)));
    const changes = [];
    for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
      if (!a[key] || !b[key]) {
        changes.push({ element: key, property: 'presence', before: !!a[key], after: !!b[key] });
        continue;
      }
      for (const prop of Object.keys(a[key]))
        if (a[key][prop] !== b[key][prop])
          changes.push({ element: key, property: prop, before: a[key][prop], after: b[key][prop] });
    }
    const old = PNG.sync.read(fs.readFileSync(path.join(before, name + '.png'))),
      current = PNG.sync.read(fs.readFileSync(path.join(after, name + '.png')));
    if (old.width !== current.width || old.height !== current.height)
      throw new Error('Image size differs: ' + name);
    const diff = new PNG({ width: old.width, height: old.height });
    const pixels = pixelmatch(old.data, current.data, diff.data, old.width, old.height, { threshold: 0.1 });
    fs.writeFileSync(path.join(out, name + '.diff.png'), PNG.sync.write(diff));
    fs.writeFileSync(path.join(out, name + '.changes.json'), JSON.stringify(changes, null, 2));
    rows.push({
      name,
      elements: Object.keys(a).length,
      styleChanges: changes.length,
      differentPixels: pixels,
      pixelRatio: pixels / (old.width * old.height),
    });
  }
  fs.writeFileSync(path.join(out, 'summary.json'), JSON.stringify(rows, null, 2));
  console.log(JSON.stringify(rows, null, 2));
  if (!rows.length || rows.some((row) => row.styleChanges || row.differentPixels))
    throw new Error('CSS visual comparison differs; inspect summary.json and diff images');
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
