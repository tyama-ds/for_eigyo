const test = require('node:test');
const assert = require('node:assert/strict');

const { loadModule, createDom } = require('./module-harness.cjs');

async function setup() {
  const events = {},
    selected = [],
    paths = [];
  const testDom = createDom(),
    document = testDom.window.document;
  const host = document.querySelector('#step-content');
  host.innerHTML =
    '<svg><path></path><circle id="inside" class="point"></circle><circle id="outside" class="point"></circle></svg>';
  const nodes = [...host.querySelectorAll('circle')],
    svg = host.querySelector('svg');
  for (const [index, node] of nodes.entries()) {
    const coordinate = index ? 130 : 30;
    node.getBoundingClientRect = () => ({ x: coordinate - 5, y: coordinate - 5, width: 10, height: 10 });
  }
  svg.getScreenCTM = () => ({ inverse: () => ({}) });
  svg.setPointerCapture = () => {
    svg.capture = true;
  };
  svg.hasPointerCapture = () => svg.capture;
  svg.releasePointerCapture = () => {
    svg.capture = false;
  };
  svg.capture = false;
  const path = host.querySelector('path'),
    set = path.setAttribute.bind(path);
  path.setAttribute = (name, value) => {
    paths.push(value);
    set(name, value);
  };
  let completed = 0;
  const context = {
    testDom,
    tool: 'lasso',
    $: (selector, root = document) => root.querySelector(selector),
    $$: () => nodes,
    setTimeout: () => {},
    DOMPoint: class {
      constructor(x, y) {
        this.x = x;
        this.y = y;
      }
      matrixTransform() {
        return this;
      }
    },
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  await loadModule(context, 'lasso.js', ['enableLasso', 'insidePolygon']);
  context.enableLasso(
    svg,
    'path',
    '.point',
    (n) => selected.push(n.id),
    () => completed++
  );
  const pointer = (type, x, y) => {
    const event = new testDom.window.Event(type);
    Object.assign(event, { clientX: x, clientY: y, button: 0, pointerId: 1 });
    svg.dispatchEvent(event);
  };
  return { context, svg, selected, paths, pointer, completed: () => completed };
}
test('closed freehand lasso selects enclosed nodes only', async () => {
  const s = await setup();
  s.pointer('pointerdown', 10, 10);
  for (const [x, y] of [
    [70, 10],
    [70, 70],
    [10, 70],
    [10, 10],
  ])
    s.pointer('pointermove', x, y);
  s.pointer('pointerup', 10, 10);
  assert.deepEqual(s.selected, ['inside']);
  assert.equal(s.completed(), 1);
  assert.equal(s.paths.at(-1), '');
  assert.equal(s.svg.capture, false);
});
test('a click is not captured, so node click selection remains available', async () => {
  const s = await setup();
  s.pointer('pointerdown', 30, 30);
  assert.equal(s.svg.capture, false);
  s.pointer('pointerup', 30, 30);
  assert.deepEqual(s.selected, []);
  assert.equal(s.completed(), 0);
});
test('pointer cancellation clears an unfinished selection without changing nodes', async () => {
  const s = await setup();
  s.pointer('pointerdown', 10, 10);
  s.pointer('pointermove', 70, 10);
  s.pointer('pointercancel', 70, 10);
  assert.equal(s.svg.capture, false);
  assert.deepEqual(s.selected, []);
  assert.equal(s.svg.dataset.drawing, undefined);
  assert.equal(s.paths.at(-1), '');
});
test('concave polygon does not select a point in its notch', async () => {
  const s = await setup(),
    poly = [
      { x: 0, y: 0 },
      { x: 100, y: 0 },
      { x: 100, y: 100 },
      { x: 50, y: 40 },
      { x: 0, y: 100 },
    ];
  assert.equal(s.context.insidePolygon({ x: 20, y: 20 }, poly), true);
  assert.equal(s.context.insidePolygon({ x: 50, y: 80 }, poly), false);
});
