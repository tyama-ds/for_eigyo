const test = require('node:test');
const assert = require('node:assert/strict');
const {compute} = require('../static/classification-layout.js');
const key = item => `${item.kind}:${item.code}`;
const make = (code, title = '', extra = {}) => ({kind: 'IPC', code, title, ...extra});
const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

function checkGeometry(result, count) {
  assert.equal(result.positions.length, count);
  assert.ok(Number.isFinite(result.height) && result.height >= 380);
  for (const [x, y] of result.positions) {
    assert.ok(Number.isFinite(x) && Number.isFinite(y));
    assert.ok(x >= 95.999 && x <= result.width - 95.999, `horizontal boundary: ${x}`);
    assert.ok(y >= 85.999 && y <= result.height - 83.999, `vertical boundary: ${y}`);
  }
  result.positions.forEach((a, i) => result.positions.slice(i + 1).forEach(b => {
    assert.ok(Math.abs(a[0] - b[0]) >= 179.99 || Math.abs(a[1] - b[1]) >= 121.99, `overlapping footprints: ${a} and ${b}`);
  }));
  for (const group of result.groups) {
    const bounds = group.bounds;
    assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= result.width + 0.001 && bounds.y + bounds.height <= result.height + 0.001);
    assert.ok(group.label && group.memberIndices.length >= 2);
    for (const i of group.memberIndices) {
      const [x, y] = result.positions[i];
      assert.ok(x >= bounds.x && x <= bounds.x + bounds.width);
      assert.ok(y - 36 > bounds.y + 30, 'caption clears the topmost circle');
      assert.ok(y + 79 <= bounds.y + bounds.height, 'caption bounds include the full node title');
    }
  }
}

test('semantic neighborhoods put related wording closer than unrelated controls', () => {
  const items = [
    make('A01B1/00', 'Lithium battery solid electrolyte', {terms: ['lithium', 'battery', 'solid', 'electrolyte']}),
    make('B01B1/00', 'Solid electrolyte lithium batteries', {terms: ['lithium', 'battery', 'solid', 'electrolyte']}),
    make('C01B1/00', 'Acoustic musical instruments piano keyboard'),
    make('D01B1/00', 'Navigation of ocean vessels marine shipping'),
    make('E01B1/00', 'Cooking food vegetables baking ovens'),
    make('F01B1/00', 'Agricultural soil cultivation ploughing tractors'),
  ];
  const result = compute(items);
  checkGeometry(result, items.length);
  const related = distance(result.positions[0], result.positions[1]);
  const controls = result.positions.slice(2).map(p => distance(result.positions[0], p));
  assert.ok(related < controls.reduce((sum, value) => sum + value, 0) / controls.length * 0.75, JSON.stringify({related, controls}));
  assert.equal(result.method, '技術項目・名称の類似度・分類階層');
});

test('Japanese character features and official English names both contribute', () => {
  const items = [make('A', '無機固体電解質'), make('B', '固体電解質材料'), make('C', '農業用耕運装置'), make('D', '音響楽器演奏'), make('E', '光学撮像素子'), make('F', '船舶航行装置')];
  const japanese = compute(items);
  assert.ok(distance(...japanese.positions.slice(0, 2)) < distance(japanese.positions[0], japanese.positions[3]));
  const official = compute(items.map((item, i) => ({...item, title: i < 2 ? ['日本語の名称一', '別表記の名称二'][i] : item.title, title_official: i < 2 ? 'Solid lithium battery electrolytes' : ''})));
  assert.ok(distance(...official.positions.slice(0, 2)) < official.positions.slice(2).reduce((sum, p) => sum + distance(p, official.positions[0]), 0) / 4);
});

test('modest wording similarities still separate topics when every node is a sibling', () => {
  const titles = ['electrical battery solid lithium', 'agriculture soil plough tractor', 'acoustic musical piano keyboard', 'electrical turbine generation power', 'agriculture fertilizer crop harvest', 'acoustic vibration sound resonance'];
  const items = titles.map((title, i) => make(`H${i}`, title, {parent: 'H'}));
  const result = compute(items);
  const related = [[0, 3], [1, 4], [2, 5]];
  const same = related.reduce((sum, [a, b]) => sum + distance(result.positions[a], result.positions[b]), 0) / 3;
  const others = [];
  for (let i = 0; i < 6; i++) for (let j = i + 1; j < 6; j++) if (!related.some(([a, b]) => a === i && b === j)) others.push(distance(result.positions[i], result.positions[j]));
  const different = others.reduce((sum, value) => sum + value, 0) / others.length;
  assert.ok(same < different * 0.8, JSON.stringify({same, different}));
});

test('explicit parent links attract, while code prefixes and different schemes are not ancestry', () => {
  const base = [make('H01M10/00'), make('H01M10/0562'), make('A01B1/00'), make('B01B1/00'), make('C01B1/00'), make('D01B1/00')];
  const explicit = compute(base.map((item, i) => i === 1 ? {...item, parent: base[0].code} : item));
  const unknown = compute(base);
  assert.ok(distance(...explicit.positions.slice(0, 2)) < distance(...unknown.positions.slice(0, 2)));
  const wrongScheme = base.map((item, i) => i === 1 ? {...item, kind: 'CPC'} : item);
  assert.deepEqual(compute(wrongScheme), compute(wrongScheme.map((item, i) => i === 1 ? {...item, parent: base[0].code} : item)));
});

test('semantic and grid coordinates retain candidate identity under input reordering without mutation', () => {
  const items = Array.from({length: 24}, (_, i) => make(`A01B${i}/00`, `Topic ${i % 4} ${['battery electrolyte', 'soil agriculture', 'marine navigation', 'optical camera'][i % 4]}`));
  items.push({...items[0], kind: 'CPC'});
  const before = JSON.stringify(items);
  for (const mode of ['semantic', 'grid']) {
    const a = compute(items, {mode}), reversed = [...items].reverse(), b = compute(reversed, {mode});
    const positions = new Map(reversed.map((item, i) => [key(item), b.positions[i]]));
    items.forEach((item, i) => assert.deepEqual(a.positions[i], positions.get(key(item))));
    assert.notDeepEqual(a.positions[0], a.positions.at(-1), 'IPC and CPC with the same code remain distinct');
    checkGeometry(a, items.length);
  }
  assert.equal(JSON.stringify(items), before);
});

test('empty, one, blank-title and dense layouts remain finite, padded and collision-free at both widths', () => {
  for (const width of [620, 850, 1000]) for (const count of [0, 1, 2, 8, 24, 80, 120]) {
    const items = Array.from({length: count}, (_, i) => make(`H01M${i}/00`, i % 2 ? '' : 'Secondary battery manufacture', {parent: 'H01M'}));
    checkGeometry(compute(items, {width}), count);
  }
});

test('semantic positions are irregular and settings switch back reproducibly', () => {
  const items = Array.from({length: 12}, (_, i) => make(`A${i}`, `Independent ${String.fromCharCode(97 + i)} mechanism`));
  const organic = compute(items, {mode: 'semantic', width: 1000});
  const grid = compute(items, {mode: 'grid', width: 1000});
  assert.notDeepEqual(organic.positions, grid.positions);
  assert.ok(new Set(organic.positions.map(p => p[0].toFixed(1))).size > 5);
  assert.equal(new Set(grid.positions.map(p => p[0])).size, 4);
  assert.deepEqual(compute(items, {mode: 'semantic', width: 1000}), organic);
  assert.equal(grid.method, '整列');
  for (const width of [620, 710, 950, 1000, 1300]) checkGeometry(compute(items, {mode: 'grid', width}), items.length);
});

test('large displays keep every candidate and use bounded topic partitions', () => {
  const items = Array.from({length: 350}, (_, i) => make(`A${i}`, ['solid electrolyte battery', 'agricultural soil plough', 'navigation ship vessel'][i % 3]));
  const result = compute(items, {width: 620});
  checkGeometry(result, items.length);
  assert.match(result.method, /分割配置/);
});

test('bilingual technical neighborhoods join IPC and F-term across four battery facets', () => {
  const items = [
    make('H01M10/0562', '無機固体電解質'),
    make('5H029AM12', 'Solid electrolyte compositions', {kind: 'F-term'}),
    make('B05D1/00', 'Surface coating and interfacial adhesion'),
    make('5H029DJ08', '界面の表面被覆・接合', {kind: 'F-term'}),
    make('C04B35/64', 'Ceramic powder sintering manufacture'),
    make('5H029CJ02', 'セラミック粉末の焼結製造', {kind: 'F-term'}),
    make('G01R31/36', 'Testing electrical performance of batteries'),
    make('5H029AJ01', '電池性能の測定・評価', {kind: 'F-term'}),
  ];
  for (const width of [620, 850, 1000]) {
    const result = compute(items, {width});
    checkGeometry(result, items.length);
    const pairs = [[0, 1], [2, 3], [4, 5], [6, 7]];
    for (const [a, b] of pairs) {
      assert.ok(result.groups.some(group => group.memberIndices.includes(a) && group.memberIndices.includes(b)), `missing technical pair ${a}/${b}: ${JSON.stringify(result.groups)}`);
      const otherDistances = items.flatMap((_, i) => i === a || i === b ? [] : [distance(result.positions[a], result.positions[i])]);
      assert.ok(distance(result.positions[a], result.positions[b]) < otherDistances.reduce((sum, value) => sum + value, 0) / otherDistances.length * 0.8);
      assert.ok(result.links.some(link => (link.from === a && link.to === b) || (link.from === b && link.to === a)));
    }
    assert.ok(result.groups.length >= 4, 'battery-related technologies remain visibly distinct');
    assert.ok(result.groups.some(group => group.label.includes('界面')));
    assert.ok(result.groups.some(group => group.label.includes('製造')));
  }
});

test('inherited broad terms do not overwhelm specific technical titles, and group metadata follows identity', () => {
  const items = [
    make('A', '表面被覆と界面接着', {facets: ['interface']}),
    make('B', 'Surface coating and interface adhesion', {kind: 'F-term', facets: [{id: 'interface', label: '界面・表面'}]}),
    make('C', 'セラミック粉末の焼結製造', {facets: ['process']}),
    make('D', 'Ceramic powder sintering manufacture', {facets: ['process']}),
  ].map(item => ({...item, terms: ['secondary battery', 'energy storage', 'device', 'solid state technology', 'battery construction']}));
  const result = compute(items), reversed = compute([...items].reverse());
  assert.equal(result.groups.length, 2);
  for (const group of result.groups) {
    const other = reversed.groups.find(value => value.id === group.id);
    assert.equal(group.label, other.label);
    assert.deepEqual(group.bounds, other.bounds);
    assert.deepEqual(group.memberIndices.map(i => key(items[i])), other.memberIndices.map(i => key([...items].reverse()[i])));
  }
  const bounds = result.groups.map(group => group.bounds);
  assert.ok(bounds[0].x + bounds[0].width <= bounds[1].x || bounds[1].x + bounds[1].width <= bounds[0].x || bounds[0].y + bounds[0].height <= bounds[1].y || bounds[1].y + bounds[1].height <= bounds[0].y, 'different technical captions and outlines do not overlap');
});

test('twenty-one candidates retain compact distinct neighborhoods across responsive canvas widths', () => {
  const topics = [
    [5, 'Secondary battery manufacture electrolytic structure'],
    [3, 'Ceramic sintering powder materials'],
    [3, 'Optical camera lens illumination'],
    [2, 'Acoustic musical instrument piano'],
    [2, 'Agricultural soil tractor'],
    [2, 'Marine navigation ocean vessels'],
    [2, 'Surface coating interface adhesion'],
    [1, 'Cooking food vegetables baking ovens'],
    [1, 'Discrete geothermal pressure valve'],
  ];
  const items = topics.flatMap(([count, title], group) => Array.from({length: count}, (_, i) => make(`N${group}-${i}`, `${title} ${i}`, {parent: `topic-${group}`})));
  for (const [width, maxHeight] of [[620, 1500], [850, 1200], [1000, 1050]]) {
    const result = compute(items, {width});
    checkGeometry(result, 21);
    assert.equal(result.groups.length, 7);
    assert.ok(result.height <= maxHeight, `${width}px canvas needlessly stretches to ${result.height}px`);
    for (let a = 0; a < result.groups.length; a++) for (let b = a + 1; b < result.groups.length; b++) {
      const one = result.groups[a].bounds, two = result.groups[b].bounds;
      assert.ok(one.x + one.width <= two.x || two.x + two.width <= one.x || one.y + one.height <= two.y || two.y + two.height <= one.y, 'group outlines stay separated');
    }
  }
});
