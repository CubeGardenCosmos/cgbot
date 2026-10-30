const DEFAULT_PERIODS = [5, 10, 20, 60];
const MA_COLORS = ['#b77900', '#7c3aed', '#2563eb', '#d97706'];
const MAX_BARS = 500;

function escapeXml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
  })[character]);
}

function number(value) {
  if (typeof value !== 'number' && typeof value !== 'string') return NaN;
  if (typeof value === 'string' && !value.trim()) return NaN;
  const result = Number(value);
  return Number.isFinite(result) ? result : NaN;
}

function dimension(value, fallback, minimum, maximum) {
  const result = number(value);
  return Number.isFinite(result) ? Math.min(maximum, Math.max(minimum, result)) : fallback;
}

function unpack(data, options) {
  if (data && !Array.isArray(data) && typeof data === 'object') {
    return { data: data.data ?? data.bars, options: { ...data, ...options } };
  }
  return { data, options: options && typeof options === 'object' ? options : {} };
}

function normalizeBars(data) {
  if (!Array.isArray(data)) return [];
  const bars = [];
  for (const row of data) {
    if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
    const open = number(row.open);
    const high = number(row.high);
    const low = number(row.low);
    const close = number(row.close);
    if (![open, high, low, close].every((value) => Number.isFinite(value) && value >= 0)) continue;
    if (low > Math.min(open, close) || high < Math.max(open, close) || low > high) continue;
    const volume = number(row.volume);
    const date = row.date ?? row.time ?? row.timestamp ?? '';
    bars.push({ open, high, low, close, volume: volume >= 0 ? volume : 0,
      date: typeof date === 'string' || typeof date === 'number' ? String(date) : '' });
  }
  return bars.slice(-MAX_BARS);
}

function periodsFor(value) {
  if (value === false) return [];
  const periods = Array.isArray(value) ? value : DEFAULT_PERIODS;
  return [...new Set(periods.map(number).filter((period) => Number.isInteger(period) && period > 0 && period <= MAX_BARS))];
}

function formatPrice(value) {
  if (!Number.isFinite(value)) return '—';
  if (value !== 0 && (Math.abs(value) >= 1e7 || Math.abs(value) < 0.001)) return value.toExponential(2);
  return value.toLocaleString('en-US', { maximumFractionDigits: value < 1 ? 4 : 2 });
}

function formatVolume(value) {
  if (value >= 1e16) return `${(value / 1e12).toExponential(2)}T`;
  if (value >= 1e12) return `${(value / 1e12).toFixed(1)}T`;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return formatPrice(value);
}

function color(value, fallback) {
  return typeof value === 'string' && /^(?:#[\da-f]{3,8}|[a-z]+)$/i.test(value) ? value : fallback;
}

function markerValues(options) {
  const markers = Array.isArray(options.markers) ? [...options.markers] : [];
  for (const type of ['support', 'resistance']) {
    const values = options[type] ?? options[`${type}s`];
    if (values === undefined) continue;
    for (const value of Array.isArray(values) ? values : [values]) {
      markers.push(value && typeof value === 'object' ? { ...value, type } : { price: value, type });
    }
  }
  return markers.flatMap((marker) => {
    if (!marker || typeof marker !== 'object') return [];
    const price = number(marker.price ?? marker.value);
    return Number.isFinite(price) && price >= 0 ? [{ price, type: marker.type,
      label: typeof marker.label === 'string' ? marker.label : marker.type === 'support' ? '支撑' : '阻力' }] : [];
  });
}

// Conservative advances for the bundled Noto Sans font, in em. Keep all synchronous
// SVG layout on this same budget; scaling the SVG cannot repair internal clipping.
function textWidth(value, size = 24) {
  let units = 0;
  for (const character of String(value)) {
    if (/\p{Mark}/u.test(character)) continue;
    if (/[ilI.,:;'!| ]/.test(character)) units += 0.38;
    else if (/[MWmw@%]/.test(character)) units += 1;
    else if (/[A-Z]/.test(character)) units += 0.78;
    else if (/[\x00-\x7f]/.test(character)) units += 0.66;
    else units += 1.08;
  }
  return units * size;
}

function wrapLabel(value, available, size = 24) {
  const lines = [];
  let line = '';
  for (const character of String(value)) {
    if (line && textWidth(line + character, size) > available) {
      lines.push(line);
      line = '';
    }
    line += character;
  }
  if (line) lines.push(line);
  return lines;
}

function headerLayout(options, width, averages) {
  const available = width - 36;
  const title = typeof options.title === 'string' ? options.title : 'K线 / OHLCV';
  const symbol = typeof options.symbol === 'string' ? options.symbol : '';
  const labels = [];
  let y = 32;
  for (const value of wrapLabel(title, available, 28)) {
    labels.push({ x: 18, y, value, role: 'title', size: 28 });
    y += 34;
  }
  if (symbol) {
    // Share the title row only when both complete labels fit, including a gap.
    if (labels.length === 1 && textWidth(title, 28) + textWidth(symbol) + 20 <= available) {
      labels.push({ x: width - 18, y: 32, value: symbol, role: 'symbol', anchor: 'end' });
    } else {
      for (const value of wrapLabel(symbol, available)) {
        labels.push({ x: width - 18, y, value, role: 'symbol', anchor: 'end' });
        y += 30;
      }
    }
  }
  let x = 18;
  for (const [index, average] of averages.entries()) {
    const value = `MA${average.period} ${average.value === undefined ? '—' : formatPrice(average.value)}`;
    const budget = textWidth(value);
    if (x > 18 && x + budget > width - 18) { x = 18; y += 30; }
    labels.push({ x, y, value, role: 'ma-label', color: MA_COLORS[index % MA_COLORS.length] });
    x += budget + 20;
  }
  return { labels, top: y + (averages.length ? 24 : 0) };
}

/** Shared, finite SVG geometry. Invalid rows are omitted; the latest 500 valid bars are shown. */
export function buildKLineGeometry(data, options = {}) {
  ({ data, options } = unpack(data, options));
  const width = dimension(options.width, 540, 320, 2160);
  let height = dimension(options.height, 640, 480, 2160);
  const bars = normalizeBars(data);
  const left = 18;
  if (!bars.length) {
    const header = headerLayout(options, width, []);
    height = Math.max(height, header.top + 100);
    return { width, height, bars, candles: [], movingAverages: [], markers: [],
      header: header.labels, plot: { left, right: width - 18, top: header.top, bottom: height - 100 } };
  }

  let high = bars[0].high;
  let low = bars[0].low;
  let maxVolume = 0;
  for (const bar of bars) {
    high = Math.max(high, bar.high);
    low = Math.min(low, bar.low);
    maxVolume = Math.max(maxVolume, bar.volume);
  }
  // Normalize before padding and averaging: even prices near Number.MAX_VALUE stay finite.
  const priceUnit = high || 1;
  const minNormalized = low / priceUnit;
  const maxNormalized = high / priceUnit;
  const padding = Math.max((maxNormalized - minNormalized) * 0.08, maxNormalized * 0.015, 0.01);
  const scaleMin = Math.max(0, minNormalized - padding);
  const scaleMax = maxNormalized + padding;
  const ticks = Array.from({ length: 5 }, (_, index) => {
    const normalized = scaleMax - (scaleMax - scaleMin) * index / 4;
    const value = Math.min(Number.MAX_VALUE, normalized * priceUnit);
    return { value, label: formatPrice(value) };
  });
  const lastLabel = formatPrice(bars.at(-1).close);
  const axisWidth = Math.max(...ticks.map((tick) => textWidth(tick.label)), textWidth(lastLabel));
  const axis = { right: width - 18, width: axisWidth + 16 };
  const right = axis.right - axis.width - 10;
  // Compute averages once, before sizing the header and mapping values to pixels.
  const averages = periodsFor(options.ma).map((period) => {
    const values = [];
    for (let index = period - 1; index < bars.length; index += 1) {
      let mean = 0;
      for (let offset = 0; offset < period; offset += 1) mean += bars[index - offset].close / priceUnit / period;
      values.push({ index, value: Math.min(high, mean * priceUnit) });
    }
    return { period, values, value: values.at(-1)?.value };
  });
  const header = headerLayout(options, width, averages);
  const top = header.top;
  // Long requested legends grow the SVG, never consume the candle panel.
  height = Math.max(height, top + 360);
  const volumeBottom = height - 66;
  const volumeTop = volumeBottom - Math.max(64, (height - top - 66) * 0.22);
  const volumeLabels = wrapLabel(`成交量 / VOL ${formatVolume(bars.at(-1).volume)}`, width - 36);
  const bottom = volumeTop - 38 - (volumeLabels.length - 1) * 28;
  const plot = { left, right, top, bottom, volumeTop, volumeBottom };
  const priceY = (price) => top + (scaleMax - price / priceUnit) / (scaleMax - scaleMin) * (bottom - top);
  ticks.forEach((tick, index) => { tick.y = top + (bottom - top) * index / 4; });
  const step = (right - left) / bars.length;
  const candleWidth = Math.max(0.35, Math.min(22, step * 0.68));
  const candles = bars.map((bar, index) => {
    const openY = priceY(bar.open);
    const closeY = priceY(bar.close);
    const bodyHeight = Math.max(1, Math.abs(openY - closeY));
    return { ...bar, x: left + step * (index + 0.5), width: candleWidth,
      highY: priceY(bar.high), lowY: priceY(bar.low), openY, closeY,
      bodyY: (openY + closeY - bodyHeight) / 2, bodyHeight,
      volumeY: volumeBottom - (maxVolume ? bar.volume / maxVolume : 0) * (volumeBottom - volumeTop),
      up: bar.close >= bar.open, doji: bar.close === bar.open };
  });
  const movingAverages = averages.map(({ period, values }) => ({
    period, points: values.map(({ index, value }) => ({ x: candles[index].x, y: priceY(value), value })),
  }));
  const markers = markerValues(options).filter((marker) => marker.price / priceUnit >= scaleMin && marker.price / priceUnit <= scaleMax)
    .map((marker) => ({ ...marker, y: priceY(marker.price) }));
  const dateLabel = (index) => {
    const date = bars[index].date;
    return /^\d{4}-\d{2}-\d{2}/.test(date) ? date.slice(0, 10) : date.slice(0, 12);
  };
  const dateIndices = bars.length === 1 ? [0] : [0, bars.length - 1];
  // Keep dates aligned with the candle axis. Stagger endpoint labels when the
  // complete formatted widths cannot share a row; do not shrink the text.
  const dates = dateIndices.map((index) => ({
    index, label: dateLabel(index), x: bars.length === 1 ? (left + right) / 2 : index === 0 ? left : right,
    anchor: bars.length === 1 ? 'middle' : index === 0 ? 'start' : 'end', y: volumeBottom + 30,
  }));
  if (dates.length === 2 && textWidth(dates[0].label) + textWidth(dates[1].label) + 16 > right - left) {
    dates[1].y += wrapLabel(dates[0].label, right - left).length * 30;
  } else if (bars.length > 2) {
    const index = Math.floor((bars.length - 1) / 2);
    const label = dateLabel(index);
    const x = (left + right) / 2;
    if (x - textWidth(label) / 2 >= left + textWidth(dates[0].label) + 16 &&
        x + textWidth(label) / 2 <= right - textWidth(dates.at(-1).label) - 16) {
      dates.splice(1, 0, { index, label, x, y: volumeBottom + 30, anchor: 'middle' });
      dateIndices.splice(1, 0, index);
    }
  }
  for (const date of dates) {
    date.rows = wrapLabel(date.label, right - left);
    height = Math.max(height, date.y + (date.rows.length - 1) * 30 + 24);
  }
  return { width, height, bars, candles, movingAverages, markers, plot, ticks, dateIndices,
    dates, axis, header: header.labels, volumeLabels, high, low, maxVolume };
}

/** Render offline, script-free OHLCV SVG. Up is always green and down red; only colors: 'cn' (explicit opt-in) makes up red. */
export function renderKLine(data, options = {}) {
  ({ data, options } = unpack(data, options));
  const chart = buildKLineGeometry(data, options);
  const { width, height, bars, candles, plot } = chart;
  const cnRedUp = options.colors === 'cn';
  const up = cnRedUp ? '#dc2626' : '#16803c';
  const down = cnRedUp ? '#16803c' : '#dc2626';
  const title = typeof options.title === 'string' ? options.title : 'K线 / OHLCV';
  const symbol = typeof options.symbol === 'string' ? options.symbol : '';
  const text = (x, y, value, attributes = '') => `<text x="${x}" y="${y}" ${attributes}>${escapeXml(value)}</text>`;
  const line = (x1, y1, x2, y2, attributes = '') => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${attributes}/>`;
  const parts = [`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeXml(`${title}${symbol ? ` · ${symbol}` : ''}`)}">`,
    `<title>${escapeXml(title)}${symbol ? ` · ${escapeXml(symbol)}` : ''}</title>`,
    '<rect width="100%" height="100%" fill="#ffffff"/>',
    '<g font-family="Noto Sans SC, Noto Sans CJK SC, sans-serif" font-size="24" fill="#334155">'];
  for (const label of chart.header) {
    parts.push(text(label.x, label.y, label.value, `class="kline-${label.role}" data-label-role="${label.role}" text-anchor="${label.anchor || 'start'}" ${label.size ? `font-size="${label.size}" font-weight="700"` : ''} ${label.color ? `fill="${label.color}"` : ''}`));
  }
  if (!bars.length) {
    wrapLabel('暂无有效行情 / No valid OHLC data', width - 36).forEach((value, index) => {
      parts.push(text(width / 2, Math.max(chart.plot.top + 30, height / 2) + index * 30, value, 'data-label-role="empty" text-anchor="middle"'));
    });
    parts.push('</g></svg>');
    return parts.join('');
  }
  for (const tick of chart.ticks) {
    parts.push(line(plot.left, tick.y, plot.right, tick.y, 'stroke="#e2e8f0"'));
    parts.push(text(chart.axis.right - 8, tick.y + 6, tick.label,
      'class="kline-price-tick" data-label-role="price-tick" text-anchor="end"'));
  }
  candles.forEach((candle) => {
    const fill = candle.up ? up : down;
    parts.push(`<g class="candle" data-doji="${candle.doji}">`,
      `<title>${escapeXml(candle.date)} O ${formatPrice(candle.open)} H ${formatPrice(candle.high)} L ${formatPrice(candle.low)} C ${formatPrice(candle.close)} V ${formatVolume(candle.volume)}</title>`,
      line(candle.x, candle.highY, candle.x, candle.lowY, `stroke="${fill}" stroke-width="1"`),
      `<rect x="${candle.x - candle.width / 2}" y="${candle.bodyY}" width="${candle.width}" height="${candle.bodyHeight}" fill="${fill}"/>`,
      `<rect class="volume-bar" x="${candle.x - candle.width / 2}" y="${candle.volumeY}" width="${candle.width}" height="${plot.volumeBottom - candle.volumeY}" fill="${fill}" opacity="0.6"/>`, '</g>');
  });
  chart.movingAverages.forEach((average, index) => {
    if (!average.points.length) return;
    const stroke = MA_COLORS[index % MA_COLORS.length];
    if (average.points.length === 1) {
      const point = average.points[0];
      parts.push(`<circle cx="${point.x}" cy="${point.y}" r="2" fill="${stroke}"/>`);
    } else parts.push(`<polyline class="moving-average" fill="none" stroke="${stroke}" stroke-width="1.5" points="${average.points.map((point) => `${point.x},${point.y}`).join(' ')}"/>`);
  });
  chart.markers.forEach((marker) => {
    const stroke = marker.type === 'support' ? '#2563eb' : '#9333ea';
    parts.push(line(plot.left, marker.y, plot.right, marker.y, `class="price-marker" stroke="${stroke}" stroke-dasharray="6 4"`),
      text(plot.left + 4, Math.max(plot.top + 18, marker.y - 5), `${marker.label} ${formatPrice(marker.price)}`, `data-label-role="marker-label" fill="${stroke}"`));
  });
  const last = candles.at(-1);
  const lastColor = last.up ? up : down;
  // Keep every tick. If the close is too near one, offset its tag into the
  // nearest inter-tick gap and connect it back to the exact closing price.
  let tagY = last.closeY;
  if (chart.ticks.some((tick) => Math.abs(tick.y - tagY) < 30)) {
    const gaps = chart.ticks.slice(1).map((tick, index) => (tick.y + chart.ticks[index].y) / 2);
    tagY = gaps.reduce((best, y) => Math.abs(y - last.closeY) < Math.abs(best - last.closeY) ? y : best);
  }
  parts.push(line(plot.left, last.closeY, plot.right, last.closeY, `class="last-price" stroke="${lastColor}" stroke-dasharray="4 4"`),
    line(plot.right, last.closeY, plot.right + 10, tagY, `stroke="${lastColor}"`),
    `<rect class="kline-last-price-box" x="${plot.right + 10}" y="${tagY - 15}" width="${chart.axis.width}" height="30" fill="${lastColor}"/>`,
    text(chart.axis.right - 8, tagY + 6, formatPrice(last.close),
      'class="kline-last-price-label" data-label-role="last-price-label" text-anchor="end" fill="#ffffff"'));
  const highCandle = candles.find((candle) => candle.high === chart.high);
  const lowCandle = candles.find((candle) => candle.low === chart.low);
  for (const [role, candle, value, y] of [
    ['high-label', highCandle, `高 ${formatPrice(chart.high)}`, Math.max(plot.top + 18, highCandle.highY - 7)],
    ['low-label', lowCandle, `低 ${formatPrice(chart.low)}`, Math.min(plot.bottom - 3, lowCandle.lowY + 22)],
  ]) {
    const rows = wrapLabel(value, plot.right - plot.left - 8);
    rows.forEach((row, index) => {
      const half = textWidth(row) / 2 + 4;
      const x = Math.max(plot.left + half, Math.min(plot.right - half, candle.x));
      const rowY = role === 'low-label' ? y - (rows.length - 1 - index) * 28 : y + index * 28;
      parts.push(text(x, rowY, row, `class="kline-${role}" data-label-role="${role}" text-anchor="middle"`));
    });
  }
  chart.volumeLabels.forEach((label, index) => {
    parts.push(text(plot.left, plot.volumeTop - 10 - (chart.volumeLabels.length - 1 - index) * 28, label,
      'class="kline-volume-label" data-label-role="volume-label"'));
  });
  parts.push(line(plot.left, plot.volumeBottom, plot.right, plot.volumeBottom, 'stroke="#94a3b8"'));
  for (const date of chart.dates) {
    date.rows.forEach((label, index) => {
      parts.push(text(date.x, date.y + index * 30, label,
        `class="kline-date-label" data-label-role="date-label" text-anchor="${date.anchor}"`));
    });
  }
  parts.push('</g></svg>');
  return parts.join('');
}
