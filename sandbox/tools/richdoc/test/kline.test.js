import test from 'node:test';
import assert from 'node:assert/strict';
import { buildKLineGeometry, renderKLine } from '../lib/kline.js';
import { realisticKLineCases, klineMAVariants } from './fixtures/kline-realistic.js';

const bar = (overrides = {}) => ({ date: '2026-01-01', open: 10, high: 13, low: 8, close: 12, volume: 100, ...overrides });
const series = (count) => Array.from({ length: count }, (_, index) => bar({
  date: `2026-01-${String(index + 1).padStart(2, '0')}`,
  open: index + 10, high: index + 13, low: index + 8, close: index + 12, volume: index * 100,
}));

function assertFiniteGeometry(chart) {
  for (const candle of chart.candles) {
    for (const name of ['x', 'width', 'highY', 'lowY', 'openY', 'closeY', 'bodyY', 'bodyHeight', 'volumeY']) {
      assert.ok(Number.isFinite(candle[name]), `${name} must be finite`);
    }
    assert.ok(candle.highY <= candle.openY);
    assert.ok(candle.highY <= candle.closeY);
    assert.ok(candle.lowY >= candle.openY);
    assert.ok(candle.lowY >= candle.closeY);
    assert.ok(candle.highY >= chart.plot.top - 1e-8);
    assert.ok(candle.lowY <= chart.plot.bottom + 1e-8);
    assert.ok(candle.volumeY >= chart.plot.volumeTop - 1e-8);
    assert.ok(candle.volumeY <= chart.plot.volumeBottom + 1e-8);
    assert.ok(candle.bodyHeight >= 1);
  }
  for (const average of chart.movingAverages) {
    for (const point of average.points) {
      assert.ok(Number.isFinite(point.x));
      assert.ok(Number.isFinite(point.y));
      assert.ok(Number.isFinite(point.value));
    }
  }
  for (const tick of chart.ticks ?? []) assert.ok(Number.isFinite(tick.value));
}

test('single candle is centered with correctly ordered wick and body geometry', () => {
  const chart = buildKLineGeometry([bar()]);
  assert.equal(chart.width, 540);
  assert.equal(chart.candles.length, 1);
  assert.equal(chart.candles[0].x, (chart.plot.left + chart.plot.right) / 2);
  assert.deepEqual(chart.dateIndices, [0]);
  assertFiniteGeometry(chart);
  const svg = renderKLine([bar()]);
  assert.match(svg, /^<svg /);
  assert.match(svg, /width="540"/);
  assert.match(svg, /class="last-price"/);
  assert.match(svg, /成交量 \/ VOL/);
  assert.match(svg, /高 13/);
  assert.match(svg, /低 8/);
  assert.match(svg, /font-size="24"/);
  assert.doesNotMatch(svg, /<script|NaN|Infinity/);
});

test('flat and zero-price markets keep finite nonzero bodies', () => {
  for (const price of [0, 0.000001, 10, 1e300, Number.MAX_VALUE]) {
    const chart = buildKLineGeometry(Array.from({ length: 5 }, () => bar({ open: price, high: price, low: price, close: price })));
    assertFiniteGeometry(chart);
    for (const candle of chart.candles) {
      assert.equal(candle.bodyHeight, 1);
      assert.equal(candle.highY, candle.lowY);
      assert.equal(candle.doji, true);
    }
    assert.doesNotMatch(renderKLine(chart.bars), /NaN|Infinity/);
  }
});

test('doji has a visible body and a full high-low wick', () => {
  const chart = buildKLineGeometry([bar({ open: 10, close: 10 })]);
  const candle = chart.candles[0];
  assert.equal(candle.bodyHeight, 1);
  assert.equal(candle.openY, candle.closeY);
  assert.ok(candle.lowY > candle.highY);
  assert.match(renderKLine(chart.bars), /data-doji="true"/);
});

test('missing, negative and invalid volume becomes zero without losing OHLC bars', () => {
  const inputs = [undefined, null, NaN, -100, Infinity, 'bad', '', 0];
  const chart = buildKLineGeometry(inputs.map((volume) => bar({ volume })));
  assert.equal(chart.bars.length, inputs.length);
  assert.equal(chart.maxVolume, 0);
  for (const candle of chart.candles) {
    assert.equal(candle.volume, 0);
    assert.equal(candle.volumeY, chart.plot.volumeBottom);
  }
  assertFiniteGeometry(chart);
});

test('volume scaling uses maximum, not sum, and tolerates huge finite volume', () => {
  const chart = buildKLineGeometry([bar({ volume: 0 }), bar({ volume: Number.MAX_VALUE })]);
  assert.equal(chart.candles[0].volumeY, chart.plot.volumeBottom);
  assert.equal(chart.candles[1].volumeY, chart.plot.volumeTop);
  assertFiniteGeometry(chart);
});

test('invalid OHLC, negative prices and inconsistent ranges are omitted', () => {
  const chart = buildKLineGeometry([
    null, undefined, {}, [], bar({ open: NaN }), bar({ close: Infinity }), bar({ low: -1 }),
    bar({ open: null }), bar({ close: '' }), bar({ high: 9 }), bar({ low: 11 }),
    bar({ high: 3, low: 20 }), bar(),
  ]);
  assert.equal(chart.bars.length, 1);
  assertFiniteGeometry(chart);
});

test('empty, missing and wholly invalid inputs render a visible empty state', () => {
  for (const input of [undefined, null, 'bad', 42, [], [null, bar({ close: NaN })], { data: [] }]) {
    const svg = renderKLine(input);
    assert.match(svg, /No valid OHLC data/);
    assert.doesNotMatch(svg, /NaN|Infinity/);
    assert.match(svg, /<\/svg>$/);
  }
});

test('gaps and invalid intervening rows do not poison candles or moving averages', () => {
  const input = series(7);
  delete input[2];
  input[4] = bar({ high: NaN });
  const chart = buildKLineGeometry(input);
  assert.equal(chart.bars.length, 5);
  assert.equal(chart.movingAverages[0].points.length, 1);
  assertFiniteGeometry(chart);
});

test('numeric strings are accepted but blank OHLC strings are rejected', () => {
  const chart = buildKLineGeometry([
    bar({ open: '10', high: '13', low: '8', close: '12', volume: '250' }),
    bar({ open: '  ' }),
  ]);
  assert.equal(chart.bars.length, 1);
  assert.equal(chart.bars[0].volume, 250);
});

test('default MA 5/10/20/60 values use available complete windows only', () => {
  const chart = buildKLineGeometry(series(60));
  assert.deepEqual(chart.movingAverages.map(({ period }) => period), [5, 10, 20, 60]);
  assert.deepEqual(chart.movingAverages.map(({ points }) => points.length), [56, 51, 41, 1]);
  assert.ok(Math.abs(chart.movingAverages[0].points[0].value - 14) < 1e-10);
  assert.ok(Math.abs(chart.movingAverages[3].points[0].value - 41.5) < 1e-10);
  assertFiniteGeometry(chart);
  assert.deepEqual(buildKLineGeometry(series(5), { ma: false }).movingAverages, []);
  assert.deepEqual(buildKLineGeometry(series(5), { ma: [2, 2, -1, NaN, 0, 1.5] }).movingAverages.map(({ period }) => period), [2]);
});

test('500 bars retain separated centers and mobile date labels are thinned', () => {
  const chart = buildKLineGeometry(series(500));
  assert.equal(chart.candles.length, 500);
  assertFiniteGeometry(chart);
  assert.ok(chart.dateIndices.length <= 4);
  assert.equal(chart.dateIndices[0], 0);
  assert.equal(chart.dateIndices.at(-1), 499);
  for (let index = 1; index < chart.candles.length; index += 1) {
    assert.ok(chart.candles[index].x > chart.candles[index - 1].x);
    assert.ok(chart.candles[index].width < chart.candles[index].x - chart.candles[index - 1].x);
  }
});

test('inputs beyond the limit retain the latest 500 valid bars', () => {
  const chart = buildKLineGeometry(series(510));
  assert.equal(chart.bars.length, 500);
  assert.equal(chart.bars[0].open, 20);
  assert.equal(chart.bars.at(-1).open, 519);
});

test('up is green and down is red by default; cn is the only red-up opt-in; custom color objects are ignored', () => {
  const rows = [bar(), bar({ open: 12, close: 10 })];
  const defaults = renderKLine(rows, { ma: false });
  assert.match(defaults, /data-doji="false">.*?stroke="#16803c"/);
  assert.match(renderKLine([bar({ open: 12, close: 10 })], { ma: false }), /stroke="#dc2626"/);
  assert.equal(renderKLine(rows, { ma: false, colors: 'intl' }), defaults);
  const cn = renderKLine(rows, { ma: false, colors: 'cn' });
  assert.match(cn, /data-doji="false">.*?stroke="#dc2626"/);
  assert.match(renderKLine(rows, { colors: { up: '#123456' } }), /stroke="#16803c"/);
  assert.doesNotMatch(renderKLine(rows, { colors: { up: '#123456' } }), /#123456/);
});

test('support and resistance markers share price scale and reject invalid levels', () => {
  const chart = buildKLineGeometry([bar()], { markers: [
    { price: 10, type: 'support', label: 'Floor' }, { price: 12, type: 'resistance' },
    { price: NaN }, { price: -1 }, { price: 1000 },
  ] });
  assert.equal(chart.markers.length, 2);
  assert.ok(chart.markers[0].y > chart.markers[1].y);
  const svg = renderKLine([bar()], { support: [10], resistance: 12 });
  assert.equal((svg.match(/class="price-marker"/g) ?? []).length, 2);
  assert.match(svg, /支撑 10/);
  assert.match(svg, /阻力 12/);
});

test('props-object integration and labels escape XML without introducing active markup', () => {
  const svg = renderKLine({
    data: [bar({ date: '<script>' })], title: '<img onerror="x"> & title', symbol: 'A&B',
    markers: [{ price: 10, label: '<script>alert(1)</script>' }],
  });
  assert.match(svg, /&lt;img onerror=&quot;x&quot;&gt; &amp; title/);
  assert.match(svg, /A&amp;B/);
  assert.match(svg, /&lt;script&gt;/);
  assert.doesNotMatch(svg, /<script|<img|url\(https/);
  assert.match(renderKLine({ bars: [bar()] }), /class="candle"/);
});

test('pathological dimensions fall back or clamp to finite layout', () => {
  for (const options of [{ width: NaN, height: Infinity }, { width: -2, height: -100 }, { width: 100000, height: 100000 }]) {
    const chart = buildKLineGeometry([bar()], options);
    assert.ok(chart.width >= 320 && chart.width <= 2160);
    assert.ok(chart.height >= 480 && chart.height <= 2160);
    assertFiniteGeometry(chart);
  }
});

test('realistic formatted prices share a padded dynamic axis gutter and retain every tick', () => {
  const fixture = realisticKLineCases.find(({ name }) => name === 'index');
  const chart = buildKLineGeometry(fixture.bars);
  const small = buildKLineGeometry(series(90));
  assert.ok(chart.plot.right < small.plot.right, 'four-digit grouped prices need more room');
  assert.ok(chart.plot.right - chart.plot.left > 300, 'reserve a useful candle panel');
  assert.ok(chart.axis.width > 92, 'latest-price box must not retain the old fixed width');
  assert.equal(chart.plot.right + 10 + chart.axis.width, chart.axis.right);
  assert.equal(chart.axis.right, chart.width - 18);
  assert.equal(chart.ticks.length, 5);
  assert.ok(chart.ticks.every(({ label }) => /^\d,\d{3}(?:\.\d{1,2})?$/.test(label)));
  const svg = renderKLine(fixture.bars);
  assert.equal((svg.match(/data-label-role="price-tick"/g) || []).length, 5);
  assert.match(svg, /data-label-role="price-tick" text-anchor="end"/);
  assert.match(svg, /data-label-role="last-price-label" text-anchor="end" fill="#ffffff">3,648\.31/);
  assert.match(svg, new RegExp(`class="kline-last-price-box"[^>]*width="${chart.axis.width}"`));
  assert.match(svg, /成交量 \/ VOL 1\.\dB/);
  assert.doesNotMatch(svg, /overflow[:=]["']?visible|textLength|lengthAdjust|font-size="(?:1\d|2[0-3])"/);
  assertFiniteGeometry(chart);
});

test('complete formatted extrema are clamped inside the plot, including first and last bars', () => {
  const fixture = realisticKLineCases.find(({ name }) => name === 'index');
  const chart = buildKLineGeometry(fixture.bars);
  const svg = renderKLine(fixture.bars);
  for (const role of ['high-label', 'low-label']) {
    const match = svg.match(new RegExp(`<text x="([^"]+)"[^>]*data-label-role="${role}"[^>]*>([^<]+)</text>`));
    assert.ok(match, role);
    const x = Number(match[1]);
    assert.ok(x >= chart.plot.left + 72, `${role}: complete Chinese prefix and grouped price need left padding`);
    assert.ok(x <= chart.plot.right - 72, `${role}: complete Chinese prefix and grouped price need right padding`);
  }
  assert.match(svg, /高 4,321\.98/);
  assert.match(svg, /低 3,511\.64/);
});

test('requested legends wrap without losing MA options or consuming the candle panel', () => {
  const fixture = realisticKLineCases[0];
  const options = {
    title: '合成测试标题很长时必须换行且不能缩小字号 Synthetic index chart',
    symbol: 'SYNTHETIC.SYMBOL.WITH.A.LONG.IDENTIFIER',
    ma: klineMAVariants.find(({ name }) => name === 'long-ma').options.ma,
    height: 480,
  };
  const chart = buildKLineGeometry(fixture.bars, options);
  assert.deepEqual(chart.movingAverages.map(({ period }) => period), options.ma);
  assert.equal(chart.header.filter(({ role }) => role === 'ma-label').length, options.ma.length);
  assert.ok(chart.header.filter(({ role }) => role === 'title').length > 1);
  assert.ok(chart.plot.top > 132);
  assert.ok(chart.plot.bottom - chart.plot.top >= 180);
  assert.ok(chart.height > 480);
  assert.ok(chart.header.every(({ y }) => y <= chart.plot.top - 24));
  assertFiniteGeometry(chart);
  const noMA = buildKLineGeometry(fixture.bars, { ma: false });
  assert.equal(noMA.header.filter(({ role }) => role === 'ma-label').length, 0);
  assert.ok(noMA.plot.top < buildKLineGeometry(fixture.bars).plot.top);
});

test('deterministic stock and index fixtures retain finite geometry across all MA modes', () => {
  for (const fixture of realisticKLineCases) {
    assert.ok(fixture.bars.length >= 60 && fixture.bars.length <= 120);
    for (const variant of klineMAVariants) {
      const options = { title: fixture.title, symbol: fixture.symbol, ...variant.options };
      const chart = buildKLineGeometry(fixture.bars, options);
      assertFiniteGeometry(chart);
      assert.equal(chart.candles.length, fixture.bars.length);
      assert.ok(chart.plot.right - chart.plot.left > 280);
      assert.equal(chart.dateIndices[0], 0);
      assert.equal(chart.dateIndices.at(-1), fixture.bars.length - 1);
      const svg = renderKLine(fixture.bars, options);
      assert.equal((svg.match(/data-label-role="price-tick"/g) || []).length, 5);
      assert.equal((svg.match(/data-label-role="ma-label"/g) || []).length, chart.movingAverages.length);
      assert.doesNotMatch(svg, /NaN|Infinity/);
    }
  }
});
