const test = require('node:test');
const assert = require('node:assert/strict');
const {previewHtml, positionPreview, createController} = require('../static/patent-inspector.js');

class Element {
  constructor(name = 'div') { this.name = name; this.listeners = new Map(); this.children = []; this.dataset = {}; this.attributes = {}; this.style = {}; this.innerHTML = ''; this.hidden = false; this.isConnected = false; this.parent = null; }
  addEventListener(name, callback) { if (!this.listeners.has(name)) this.listeners.set(name, new Set()); this.listeners.get(name).add(callback); }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }
  fire(name, extra = {}) { const event = {type: name, target: this, ...extra}; for (const callback of this.listeners.get(name) || []) callback(event); }
  appendChild(child) { this.children.push(child); child.parent = this; child.isConnected = this.isConnected; return child; }
  contains(child) { return !!child && (this === child || this.children.some(item => item.contains(child))); }
  closest() { return this.name === 'tr' && this.dataset.patentId ? this : this.parent?.closest() || null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getBoundingClientRect() { return this.name === 'tr' ? {left: 25, top: 100, bottom: 150, width: 900, height: 50} : {width: 460, height: 520}; }
}
function setup() {
  const doc = new Element('document'), win = new Element('window'), timers = new Map(), observers = [];
  let timerId = 0;
  doc.body = new Element('body'); doc.body.isConnected = true;
  doc.getElementById = id => doc.body.children.find(item => item.id === id) || null;
  doc.createElement = name => new Element(name);
  doc.activeElement = doc.body;
  Object.assign(win, {innerWidth: 1024, innerHeight: 768,
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    MutationObserver: class { constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() { this.disconnected = true; } },
  });
  const root = doc.body.appendChild(new Element('tbody'));
  const row = root.appendChild(new Element('tr')); row.dataset.patentId = 'JP-1';
  const button = row.appendChild(new Element('button'));
  const otherRow = root.appendChild(new Element('tr')); otherRow.dataset.patentId = 'JP-2';
  const patents = [{id: 'JP-1', title: '自動運転システム', abstract: 'カメラを使用する。', label: 'keep', label_source: 'human', score: .72}, {id: 'JP-2', title: '別の特許'}];
  const api = createController(doc, win); api.bind(root, patents);
  return {doc, win, root, row, button, otherRow, patents, api, observers,
    panel: () => doc.getElementById('patent-row-tooltip'),
    flush() { const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()); },
    hover(target = row) { root.fire('mouseover', {target, clientX: 930, clientY: 700}); },
    focus(target = row) { doc.activeElement = target; root.fire('focusin', {target}); },
  };
}

test('all imported text is escaped and content remains a summary, without invented links', () => {
  const bad = '<img src=x onerror="bad()">&\'text\'';
  const html = previewHtml({id: bad, title: bad, applicant: bad, abstract: bad, ipc: [bad], fterm: bad, year: bad, label_reason: bad});
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img src=x onerror=&quot;bad()&quot;&gt;&amp;&#39;text&#39;'));
  assert.ok(html.includes('<h4>要約</h4>'));
  assert.ok(!html.includes('全文'));
  assert.ok(!html.includes('href='));
});

test('missing fields are disclosed and manual decisions do not reuse stale AI confidence', () => {
  const empty = previewHtml();
  assert.ok(empty.includes('名称未収録'));
  assert.ok(empty.includes('未判定'));
  assert.ok(empty.includes('未算出'));
  const human = previewHtml({label: 'keep', label_source: 'human', agent_confidence: .99, score: .72});
  assert.ok(human.includes('利用者'));
  assert.ok(human.includes('72%'));
  assert.ok(human.includes('対象外'));
  assert.ok(!human.includes('99%'));
});

test('agent confidence and learned relevance are distinct and invalid probabilities are rejected', () => {
  const html = previewHtml({label_source: 'agent', score: .52, agent_confidence: .94});
  assert.ok(html.includes('保留'));
  assert.match(html, /学習モデルの関連度<\/span><strong>52%/);
  assert.match(html, /AI判定の確信度<\/span><strong>94%/);
  const bad = previewHtml({label_source: 'agent', score: '0.9', agent_confidence: 5});
  assert.ok(bad.includes('未算出'));
  assert.ok(!bad.includes('500%'));
});

test('LLM relevance is identified as self-assessment and withheld scores stay uncalculated', () => {
  const rated = previewHtml({score_source:'llm', score:.83, label_source:'agent', agent_confidence:.92});
  assert.match(rated, /LLMの関連度（自己評価）<\/span><strong>83%/);
  assert.ok(rated.includes('関連度と確信度はいずれもLLMの自己評価です。関連性の保証ではありません。'));
  assert.ok(!rated.includes('学習モデル'));
  const held = previewHtml({score_source:'llm', score:null, llm_relevance:.83, label_source:'agent'});
  assert.match(held, /LLMの関連度（自己評価）<\/span><strong>未算出/);
  assert.ok(!held.includes('83%'));
  for (const score_source of ['lightweight', 'transformer', undefined]) {
    assert.match(previewHtml({score_source, score:.61}), /学習モデルの関連度<\/span><strong>61%/);
  }
});

test('placement stays within the viewport at all four corners and a narrow viewport', () => {
  for (const viewport of [{width: 1024, height: 768}, {width: 360, height: 640}]) {
    const size = {width: Math.min(460, viewport.width - 24), height: viewport.height - 24};
    for (const point of [{x:0,y:0}, {x:viewport.width,y:0}, {x:0,y:viewport.height}, {x:viewport.width,y:viewport.height}]) {
      const p = positionPreview(point, size, viewport);
      assert.ok(p.left >= 12 && p.top >= 12);
      assert.ok(p.left + size.width <= viewport.width - 12);
      assert.ok(p.top + size.height <= viewport.height - 12);
    }
  }
});

test('hover shows the requested row without changing any patent data', () => {
  const s = setup(), before = JSON.stringify(s.patents);
  s.hover();
  assert.equal(s.panel().hidden, false);
  assert.ok(s.panel().innerHTML.includes('自動運転システム'));
  assert.equal(s.panel().attributes.role, 'tooltip');
  assert.equal(JSON.stringify(s.patents), before);
  s.hover(s.otherRow);
  assert.ok(s.panel().innerHTML.includes('別の特許'));
  assert.ok(!s.panel().innerHTML.includes('自動運転システム'));
});

test('moving from a row to the panel keeps it readable, including scrolling', () => {
  const s = setup(); s.hover();
  s.root.fire('mouseout', {target: s.row, relatedTarget: s.panel()});
  s.panel().fire('mouseenter'); s.flush();
  assert.equal(s.panel().hidden, false);
  s.doc.fire('scroll', {target: s.panel()});
  assert.equal(s.panel().hidden, false);
  s.panel().fire('mouseleave'); s.flush();
  assert.equal(s.panel().hidden, true);
});

test('keyboard row focus and nested judgment buttons work without consuming their keys or clicks', () => {
  const s = setup(); s.focus();
  assert.equal(s.panel().hidden, false);
  s.root.fire('focusout', {target: s.row, relatedTarget: s.button}); s.focus(s.button); s.flush();
  assert.equal(s.panel().hidden, false);
  let clicks = 0; s.button.addEventListener('click', () => clicks++); s.button.fire('click');
  s.doc.fire('keydown', {key: 'Enter', preventDefault() { throw Error('must not consume Enter'); }});
  assert.equal(clicks, 1);
  s.root.fire('focusout', {target: s.button, relatedTarget: s.doc.body}); s.flush();
  assert.equal(s.panel().hidden, true);
});

test('Escape stays dismissed while pointer moves between cells in the same row', () => {
  const s = setup(); s.hover(); s.doc.fire('keydown', {key: 'Escape'});
  assert.equal(s.panel().hidden, true);
  s.hover(s.button);
  assert.equal(s.panel().hidden, true);
  s.root.fire('mouseout', {target: s.button, relatedTarget: s.doc.body});
  s.hover(); assert.equal(s.panel().hidden, false);
});

test('rebinding after a list update hides stale content and replaces old listeners', () => {
  const s = setup(); s.hover();
  const initial = s.root.listeners.get('mouseover').size;
  s.api.bind(s.root, [{id:'JP-1', title:'更新された名称'}]);
  assert.equal(s.panel().hidden, true);
  assert.equal(s.panel().innerHTML, '');
  assert.equal(s.observers[0].disconnected, true);
  assert.equal(s.root.listeners.get('mouseover').size, initial);
  s.hover(); assert.ok(s.panel().innerHTML.includes('更新された名称'));
  s.api.bind(null, []);
  assert.equal(s.root.listeners.get('mouseover').size, 0);
  assert.equal(s.doc.listeners.get('keydown').size, 0);
});

test('row removal, outside scrolling, resize and explicit navigation hide all close previews', () => {
  const s = setup(); s.hover();
  s.doc.fire('scroll', {target:s.root}); assert.equal(s.panel().hidden, true);
  s.hover(); s.win.fire('resize'); assert.equal(s.panel().hidden, true);
  s.hover(); s.api.hide(); assert.equal(s.panel().hidden, true);
  s.hover(); s.root.children = s.root.children.filter(row => row !== s.row); s.observers.at(-1).callback();
  assert.equal(s.panel().hidden, true);
});

test('empty-result and overflow notice rows are ignored safely', () => {
  const s = setup(), notice = s.root.appendChild(new Element('tr'));
  s.root.fire('mouseover', {target:notice});
  s.root.fire('mouseout', {target:notice, relatedTarget:s.doc.body});
  s.root.fire('focusin', {target:notice});
  assert.equal(s.panel(), null);
});
