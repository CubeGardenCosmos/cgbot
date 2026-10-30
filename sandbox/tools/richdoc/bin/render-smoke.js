#!/usr/bin/env node
// Explicit opt-in browser smoke; not part of npm test. Retain PNGs for manual inspection.
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import { renderReport } from '../lib/render.js';
import { assertPngMetadata } from '../lib/validate.js';

const output_dir = process.argv[2] ? path.resolve(process.argv[2]) : await mkdtemp(path.join(tmpdir(), 'rich-report-smoke-'));
await mkdir(output_dir, { recursive: true });
const markup = `# 合成示例 · Rendering smoke

This is FAKE test data, not financial analysis. 中文字体与图表检查。

<Stats><Stat label="FAKE gain" value="+3%" delta={3} /><Stat label="FAKE loss" value="-2%" delta={-2} /></Stats>

<Chart type="bar" title="FAKE" data={[{"name":"甲","value":2},{"name":"乙","value":3}]} />

<KLine title="FAKE" symbol="TEST" ma={[5]} data={[{"date":"2026-01-01","open":10,"high":12,"low":9,"close":11,"volume":100},{"date":"2026-01-02","open":11,"high":12,"low":8,"close":9,"volume":120}]} />

<Mermaid>
flowchart TD
  A[检查] --> B[通过]
</Mermaid>

Inline math $r=(b-a)/a$.
`;
const result = await renderReport({ markup, output_dir, name: 'valid' });
assert.deepEqual(result.errors, []);
assert.ok(result.files.length > 0);
for (const filename of result.files) assertPngMetadata(await sharp(filename).metadata());
const overflowDir = path.join(output_dir, 'overflow-rejected');
await assert.rejects(renderReport({ markup: `# Deliberate overflow\n\n$$\n${Array(100).fill('123456789').join('+')}\n$$`, output_dir: overflowDir, name: 'must-not-exist' }), /Report validation failed|horizontal|text clipped/);
assert.deepEqual(await readdir(overflowDir).catch(error => { if (error.code === 'ENOENT') return []; throw error; }), []);
const blankDir = path.join(output_dir, 'blank-rejected');
await assert.rejects(renderReport({ markup: '<Chart type="line" option={{"series":[{"type":"line","data":[]}]}} />', output_dir: blankDir }), /Chart has no series data/);
assert.deepEqual(await readdir(blankDir).catch(error => { if (error.code === 'ENOENT') return []; throw error; }), []);
console.log(JSON.stringify({ ...result, overflow_rejected_without_artifacts: true, blank_chart_rejected_without_artifacts: true, manual_review_required: true }, null, 2));
