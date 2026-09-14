const test = require('node:test');
const assert = require('node:assert/strict');
const names = require('../static/classification-labels.js');
const {compute} = require('../static/classification-layout.js');

test('official Japanese and English are display-only and preserve semantic coordinates', () => {
  const items = [{kind:'IPC', code:'C01D15/00', title:'Lithium compounds', title_official:'Lithium compounds',
    title_en:'Lithium compounds', title_ja:'リチウム化合物', title_ja_status:'official_translation'},
    {kind:'IPC', code:'H01M10/00', title:'Secondary cells', title_ja:'二次電池', title_en:'Secondary cells'}];
  const before = JSON.stringify(items), layout = compute(items);
  assert.equal(names.label(items[0], 'ja'), 'リチウム化合物');
  assert.equal(names.label(items[0], 'en'), 'Lithium compounds');
  assert.equal(names.bilingual(items[0]).jaStatus, 'official_translation');
  assert.equal(JSON.stringify(items), before);
  assert.deepEqual(compute(items), layout);
});

test('missing translations fall back visibly without relabeling Japanese as English', () => {
  const jp = {kind:'F-term', code:'5H029AM11', title:'固体電解質'};
  assert.equal(names.bilingual(jp).en, '');
  assert.match(names.fallback(jp, 'en'), /英訳未収録/);
  assert.equal(names.label(jp, 'en'), '固体電解質');
  const en = {kind:'IPC', code:'X', title:'Unknown caption', title_official:'Unknown caption'};
  assert.equal(names.bilingual(en).ja, '');
  assert.match(names.fallback(en, 'ja'), /日本語未収録/);
  assert.equal(names.label(en, 'ja'), 'Unknown caption');
});

test('local Japanese captions and F-term reference English translations keep distinct provenance', () => {
  const ipc = {kind:'IPC', code:'H01M10/0562', title:'無機固体電解質', title_official:'Inorganic solid electrolytes'};
  assert.equal(names.bilingual(ipc).jaStatus, 'app_caption');
  assert.equal(names.bilingual(ipc).enStatus, 'official');
  const ft = {...ipc, kind:'F-term', title_ja:'固体電解質', title_en:'Solid electrolytes', title_en_status:'app_translation'};
  assert.equal(names.status(names.bilingual(ft).enStatus), 'アプリ内の参考訳');
  assert.match(names.tooltip(ft), /日本語: 固体電解質\nEnglish: Solid electrolytes/);
  assert.equal(names.group('材料・組成 · 熱・温度', 'en'), 'Materials & compositions · Heat & temperature');
});
