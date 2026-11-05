// Manual audit: counts source lines longer than the Prettier print width (110) in static/*.js.
// Usage: node tests/line-length-audit.cjs [limit]   (exit code 1 when any line exceeds the limit)
const fs = require('node:fs');
const path = require('node:path');

const limit = Number(process.argv[2] || 110);
const dir = path.resolve(__dirname, '../static');
let total = 0,
  longest = 0;
for (const file of fs.readdirSync(dir).filter((name) => name.endsWith('.js')).sort()) {
  const lines = fs.readFileSync(path.join(dir, file), 'utf8').split('\n');
  const long = lines.map((line, index) => [index + 1, line.length]).filter(([, length]) => length > limit);
  longest = Math.max(longest, ...lines.map((line) => line.length));
  total += long.length;
  if (long.length)
    console.log(
      `${file}: ${long.length} lines > ${limit} (longest ${Math.max(...long.map(([, n]) => n))}, first at line ${long[0][0]})`
    );
}
console.log(`total lines > ${limit}: ${total}; longest line: ${longest}`);
process.exitCode = total ? 1 : 0;
