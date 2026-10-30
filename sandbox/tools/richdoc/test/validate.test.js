import test from 'node:test';
import assert from 'node:assert/strict';
import { inspectReportLayout, measureReportHeight, assertPngMetadata } from '../lib/validate.js';

function fixture({ width = 540, ancestor, child = {}, text, fontError = false } = {}) {
  const rect = (overrides = {}) => ({ left: 0, top: 0, right: 540, bottom: 100, width: 540, height: 100, ...overrides });
  const element = (options = {}) => ({
    tagName: 'DIV', className: '', namespaceURI: 'http://www.w3.org/1999/xhtml',
    clientWidth: 540, scrollWidth: 540, clientHeight: 100,
    style: { display: 'block', visibility: 'visible', overflowX: 'visible', overflowY: 'visible' },
    getBoundingClientRect() { return this.rect || rect(); },
    querySelectorAll() { return []; }, querySelector() { return null; },
    matches() { return false; }, closest() { return null; }, ...options,
  });
  const root = element({ className: 'rr-report', rect: rect({ width, right: width }) });
  const parent = ancestor ? element({ parentElement: root, ...ancestor }) : root;
  const item = element({ parentElement: parent, ...child });
  root.querySelectorAll = () => ancestor ? [parent, item] : [item];
  const nodes = text ? [{ parentElement: item, textContent: text.value || 'Text', rect: rect(text.rect) }] : [];
  let current;
  globalThis.document = {
    querySelector: () => root,
    fonts: fontError ? [{ status: 'error', family: 'Test font' }] : [],
    createTreeWalker: () => ({ nextNode() { current = nodes.shift(); return !!current; }, get currentNode() { return current; } }),
    createRange: () => ({ selectNodeContents(node) { this.node = node; }, getClientRects() { return [this.node.rect]; }, detach() {} }),
  };
  globalThis.NodeFilter = { SHOW_TEXT: 4 };
  globalThis.getComputedStyle = target => target.style;
  return () => inspectReportLayout();
}

function withFixture(options, check) {
  try { check(fixture(options)()); }
  finally { delete globalThis.document; delete globalThis.NodeFilter; delete globalThis.getComputedStyle; }
}

test('valid CSS-normalized 540px layout passes', () => withFixture({}, result => assert.deepEqual(result, [])));
test('narrower content root is rejected rather than shrinking PNG', () => withFixture({ width: 520 }, result => assert.match(result.join('\n'), /must remain 540px/)));
test('horizontal scroll overflow is actionable', () => withFixture({ child: { scrollWidth: 600 } }, result => assert.match(result.join('\n'), /horizontal content overflow/)));
test('geometry outside root is rejected', () => withFixture({ child: { rect: { left: 0, right: 600, width: 600, height: 100 } } }, result => assert.match(result.join('\n'), /horizontal geometry/)));
test('hidden horizontal text is detected using Range bounds', () => withFixture({ child: { style: { overflowX: 'hidden' }, clientWidth: 100 }, text: { rect: { right: 120, width: 120 } } }, result => assert.match(result.join('\n'), /text clipped horizontally/)));
test('fixed-height cards cutting off text fail', () => withFixture({ child: { style: { overflowY: 'hidden' }, clientHeight: 30 }, text: { rect: { bottom: 80, height: 80 } } }, result => assert.match(result.join('\n'), /text clipped vertically/)));
test('text outside root is rejected independently of element geometry', () => withFixture({ text: { rect: { right: 570 } } }, result => assert.match(result.join('\n'), /text extends beyond/)));
test('replacement characters are detected', () => withFixture({ text: { value: 'bad \uFFFD encoding' } }, result => assert.match(result.join('\n'), /U\+FFFD/)));
test('failed fonts are detected', () => withFixture({ fontError: true }, result => assert.match(result.join('\n'), /Font failed to load/)));
test('failed images are rejected', () => withFixture({ child: { tagName: 'IMG', complete: true, naturalWidth: 0, naturalHeight: 0 } }, result => assert.match(result.join('\n'), /image failed/)));
test('empty chart initialization is rejected', () => withFixture({ child: { matches: () => true } }, result => assert.match(result.join('\n'), /chart is empty/)));
test('intentional clipped SVG plot geometry does not trigger HTML overflow rules', () => withFixture({ child: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'path', scrollWidth: 900, rect: { left: -50, right: 900, width: 950, height: 100 } } }, result => assert.deepEqual(result, [])));
test('clipping diagnostics identify text and its clipping ancestor', () => withFixture({
  ancestor: { id: 'price-panel', className: 'rr-kline', style: { overflowY: 'hidden' }, clientHeight: 30 },
  child: { tagName: 'SPAN', className: 'axis-label' },
  text: { value: 'Latest close: 123.45', rect: { bottom: 80, height: 80 } },
}, result => assert.ok(result.includes('span.axis-label text "Latest close: 123.45": text clipped vertically by div#price-panel.rr-kline; remove the fixed height or shorten the card'))));
test('SVG clipping diagnostics retain the actual text selector and animated SVG classes', () => withFixture({
  ancestor: {
    namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'svg', id: 'price-chart',
    className: { baseVal: 'kline-viewport' }, style: { overflowX: 'hidden' }, clientWidth: 100,
  },
  child: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'text', id: 'last-price', className: { baseVal: 'axis-label last-price' } },
  text: { value: '123.45 USD', rect: { right: 120, width: 120 } },
}, result => assert.ok(result.includes('text#last-price.axis-label.last-price text "123.45 USD": text clipped horizontally by svg#price-chart.kline-viewport; expand or wrap the container'))));
test('unclassified SVG text diagnostics include their viewport selector', () => withFixture({
  ancestor: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'svg', className: { baseVal: 'kline-viewport' }, style: { overflowY: 'hidden' }, clientHeight: 30 },
  child: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'text', className: { baseVal: '' } },
  text: { value: 'Volume', rect: { bottom: 80, height: 80 } },
}, result => assert.ok(result.includes('svg.kline-viewport > text text "Volume": text clipped vertically by svg.kline-viewport; remove the fixed height or shorten the card'))));
test('text diagnostics normalize control characters and safely quote bounded content', () => withFixture({
  child: { style: { overflowX: 'hidden' }, clientWidth: 100 },
  text: { value: ` \n\tPrice\u0000\u202e "quoted" \\ ${'x'.repeat(180)}\n`, rect: { right: 120, width: 120 } },
}, result => {
  // The fixture also has scrollWidth > clientWidth; keep that overflow failure.
  assert.equal(result.length, 2);
  assert.ok(result.some(message => message.includes('horizontal content overflow')));
  const clipping = result.find(message => message.includes('text clipped horizontally'));
  const excerpt = JSON.parse(clipping.match(/ text (".*"): text clipped horizontally/)[1]);
  assert.equal(excerpt, `${`Price "quoted" \\ ${'x'.repeat(180)}`.slice(0, 120)}...`);
  assert.doesNotMatch(clipping, /[\n\r\t\u0000\u202e]/u);
}));
test('out-of-root text diagnostics name the offending text', () => withFixture({
  child: { tagName: 'SPAN', id: 'summary' },
  text: { value: 'Closing price', rect: { right: 570 } },
}, result => assert.ok(result.includes('span#summary text "Closing price": text extends beyond the content root; wrap or shorten it'))));
test('diagnostic selectors escape punctuation in SVG identifiers and classes', () => withFixture({
  ancestor: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'svg', id: 'chart:price', style: { overflowX: 'hidden' }, clientWidth: 100 },
  child: { namespaceURI: 'http://www.w3.org/2000/svg', tagName: 'text', className: { baseVal: 'axis:price' } },
  text: { value: 'Close', rect: { right: 120, width: 120 } },
}, result => assert.ok(result.includes('text.axis\\3a price text "Close": text clipped horizontally by svg#chart\\3a price; expand or wrap the container'))));
test('encoded PNG metadata must retain physical 1080px width', () => {
  assert.doesNotThrow(() => assertPngMetadata({ format: 'png', width: 1080, height: 800 }));
  for (const metadata of [{ format: 'png', width: 540, height: 800 }, { format: 'png', width: 1079, height: 800 }, { format: 'png', width: 1081, height: 800 }, { format: 'jpeg', width: 1080, height: 800 }, { format: 'png', width: 1080, height: 0 }]) {
    assert.throws(() => assertPngMetadata(metadata), /Invalid encoded PNG/);
  }
});
test('report height follows content and ignores the viewport minimum', () => {
  const rect = (bottom, height = 100) => ({ top: bottom - height, bottom, height, left: 0, right: 540, width: 540 });
  const content = { getBoundingClientRect: () => rect(610.2) };
  const collapsed = { getBoundingClientRect: () => rect(900, 0) };
  const root = { querySelectorAll: () => [content, collapsed], getBoundingClientRect: () => rect(1000) };
  globalThis.document = { querySelector: () => root };
  globalThis.getComputedStyle = () => ({ paddingBottom: '32px' });
  try {
    assert.equal(measureReportHeight(), 643);
    root.querySelectorAll = () => [];
    assert.equal(measureReportHeight(), 32);
    document.querySelector = () => null;
    assert.throws(() => measureReportHeight(), /Missing .rr-report content root/);
  } finally {
    delete globalThis.document;
    delete globalThis.getComputedStyle;
  }
});
