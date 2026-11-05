import { $, $$ } from './core.js';
import { appState } from './ui-session.js';

const lassoTargets = new WeakMap();
function bindLasso(svg, options) {
  lassoTargets.set(svg, options);
  if (svg.dataset.lassoBound) return;
  svg.dataset.lassoBound = '1';
  let points = [],
    pointer = null,
    dragged = false;
  svg.addEventListener('pointerdown', (event) => {
    const current = lassoTargets.get(svg);
    if (!current.enabled() || event.button !== 0) return;
    pointer = event.pointerId;
    points = [current.coordinate(event)];
    dragged = false;
    svg.dataset.drawing = '1';
  });
  svg.addEventListener('pointermove', (event) => {
    if (event.pointerId !== pointer) return;
    const current = lassoTargets.get(svg),
      point = current.coordinate(event);
    const distance = Math.hypot(point.x - points[0].x, point.y - points[0].y);
    if (!dragged && (current.dragAtThreshold ? distance < 5 : distance <= 5)) return;
    dragged = true;
    if (!svg.hasPointerCapture(pointer)) svg.setPointerCapture(pointer);
    points.push(point);
    current.path().setAttribute('d', 'M' + points.map((p) => `${p.x},${p.y}`).join(' L') + ' Z');
  });
  const finish = (event, cancelled = false) => {
    if (event.pointerId !== pointer) return;
    if (svg.hasPointerCapture(pointer)) svg.releasePointerCapture(pointer);
    pointer = null;
    delete svg.dataset.drawing;
    const current = lassoTargets.get(svg);
    current.path().setAttribute('d', '');
    if (!cancelled && dragged && points.length > 3) {
      svg.dataset.dragged = '1';
      current.complete(points);
      setTimeout(() => delete svg.dataset.dragged, 0);
    }
  };
  svg.addEventListener('pointerup', (event) => finish(event));
  svg.addEventListener('pointercancel', (event) => finish(event, true));
}
function enableLasso(svg, pathSelector, nodeSelector, select, done) {
  const coordinate = (event) =>
    new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
  bindLasso(svg, {
    enabled: () => appState.tool === 'lasso',
    coordinate,
    path: () => $(pathSelector, svg),
    complete: (polygon) => {
      $$(nodeSelector, svg).forEach((node) => {
        const circle = node.tagName.toLowerCase() === 'circle' ? node : $('circle', node);
        const box = circle.getBoundingClientRect();
        const point = coordinate({ clientX: box.x + box.width / 2, clientY: box.y + box.height / 2 });
        if (insidePolygon(point, polygon)) select(node);
      });
      done();
    },
  });
}
function insidePolygon(point, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i],
      b = polygon[j];
    if (a.y > point.y !== b.y > point.y && point.x < ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x)
      inside = !inside;
  }
  return inside;
}
export { bindLasso, enableLasso, insidePolygon };
