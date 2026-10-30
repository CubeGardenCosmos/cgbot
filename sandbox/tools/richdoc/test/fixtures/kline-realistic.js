// Deterministic synthetic OHLCV fixtures: realistic magnitudes, NOT market observations.
function syntheticBars(count, base, swing, decimals, volume) {
  const round = (value) => Number(value.toFixed(decimals));
  return Array.from({ length: count }, (_, index) => {
    const open = round(base + Math.sin(index * 0.31) * swing + Math.cos(index * 0.17) * swing * 0.3);
    const close = round(open + Math.sin(index * 0.73 + 1) * swing * 0.23);
    return {
      date: new Date(Date.UTC(2026, 0, 1 + index)).toISOString().slice(0, 10),
      open, close,
      high: round(Math.max(open, close) + swing * (0.12 + (index % 4) * 0.03)),
      low: round(Math.min(open, close) - swing * (0.12 + (index % 3) * 0.03)),
      volume: Math.round(volume * (0.8 + (index % 11) * 0.05)),
    };
  });
}

const indexBars = syntheticBars(90, 3980, 180, 2, 1.1e9);
indexBars[0] = { ...indexBars[0], high: 4321.98 };
indexBars[indexBars.length - 1] = { ...indexBars.at(-1), open: 3662.45, high: 3684.76, low: 3511.64, close: 3648.31, volume: 1.23e9 };

export const realisticKLineCases = [
  { name: 'index', title: '合成指数 / Synthetic index', symbol: 'SYNTH.INDEX', bars: indexBars },
  { name: 'small-price', title: '合成低价股 / Synthetic', symbol: 'SYNTH.SMALL', bars: syntheticBars(60, 0.0678, 0.009, 4, 9e8) },
  { name: 'hk-style', title: '合成港股 / Synthetic HK', symbol: '00000.HK', bars: syntheticBars(120, 365.89, 35, 2, 1.3e9) },
  { name: 'us-style', title: '合成美股 / Synthetic US', symbol: 'SYNTH.US', bars: syntheticBars(90, 238.79, 19, 2, 1e9) },
  { name: 'five-digit', title: '合成高位指数 / Synthetic', symbol: 'SYNTH.HIGH', bars: syntheticBars(90, 24870.31, 1850, 2, 1.1e9) },
];

export const klineMAVariants = [
  { name: 'default', options: {} },
  { name: 'ma5', options: { ma: [5] } },
  { name: 'no-ma', options: { ma: false } },
  { name: 'long-ma', options: { ma: [3, 5, 8, 10, 12, 20, 30, 45, 60, 90, 120, 250, 500] } },
];
