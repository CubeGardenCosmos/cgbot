#!/usr/bin/env node
// Explicit browser integration regression; not run by the unit-test glob.
// Uses the production renderer, bundled fonts, and unchanged layout validator.
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import { renderReport } from '../lib/render.js';
import { buildKLineGeometry, renderKLine } from '../lib/kline.js';
import { assertPngMetadata } from '../lib/validate.js';
import { realisticKLineCases, klineMAVariants } from '../test/fixtures/kline-realistic.js';

// The report's bottom padding is 32 CSS px at DPR 2; any larger uniform white band means the page is taller than its content.
const MAX_TRAILING_BLANK_ROWS = 32 * 2 + 4;
async function trailingBlankRows(filename) {
  const { data, info } = await sharp(filename).removeAlpha().raw().toBuffer({ resolveWithObject: true });
  const { width, height, channels } = info;
  const rowBlank = y => {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * channels;
      if (data[offset] !== 255 || data[offset + 1] !== 255 || data[offset + 2] !== 255) return false;
    }
    return true;
  };
  let blank = 0;
  while (blank < height && rowBlank(height - 1 - blank)) blank += 1;
  return blank;
}

const output_dir = process.argv[2] ? path.resolve(process.argv[2]) : await mkdtemp(path.join(tmpdir(), 'kline-browser-regression-'));
await mkdir(output_dir, { recursive: true });
const cases = [];
for (const fixture of realisticKLineCases) {
  for (const variant of klineMAVariants) {
    const name = `${fixture.name}-${variant.name}`;
    const options = { title: fixture.title, symbol: fixture.symbol, ...variant.options };
    const props = { ...options, data: fixture.bars };
    const markup = `# Synthetic test data — ${name}\n\nDeterministic synthetic prices and volumes; not actual market observations.\n\n<KLine ${Object.entries(props).map(([key, value]) => `${key}={${JSON.stringify(value)}}`).join(' ')} />`;
    // renderReport settles the real fonts and runs inspectReportLayout before
    // writing artifacts. A clipping failure must reject this call, not be hidden.
    const result = await renderReport({ markup, output_dir, name });
    assert.deepEqual(result.errors, [], name);
    assert.ok(result.files.length > 0, name);
    const images = [];
    for (const filename of result.files) {
      const metadata = await sharp(filename).metadata();
      assertPngMetadata(metadata, 1080);
      assert.equal(metadata.width, 1080, filename);
      const blank = await trailingBlankRows(filename);
      assert.ok(blank <= MAX_TRAILING_BLANK_ROWS, `${filename}: ${blank} blank bottom rows; page height must follow content`);
      images.push({ filename, width: metadata.width, height: metadata.height, trailing_blank_rows: blank });
    }
    const chart = buildKLineGeometry(fixture.bars, options);
    const svg = renderKLine(fixture.bars, options);
    assert.equal((svg.match(/data-label-role="price-tick"/g) || []).length, 5);
    assert.equal((svg.match(/data-label-role="ma-label"/g) || []).length, chart.movingAverages.length);
    await writeFile(path.join(output_dir, `${name}.svg`), svg);
    await writeFile(path.join(output_dir, `${name}.json`), JSON.stringify(props, null, 2));
    cases.push({ name, images, svg: path.join(output_dir, `${name}.svg`), plot: chart.plot,
      axis: chart.axis, tickLabels: chart.ticks.map(({ label }) => label),
      maCount: chart.movingAverages.length, headerRows: [...new Set(chart.header.map(({ y }) => y))] });
  }
}
const manifest = { synthetic_data: true, renderer: 'lib/render.js renderReport', expected_physical_width: 1080,
  manual_review_required: true, output_dir, cases };
await writeFile(path.join(output_dir, 'manifest.json'), JSON.stringify(manifest, null, 2));
console.log(JSON.stringify(manifest, null, 2));
