#!/usr/bin/env node
import path from 'node:path';
import { renderReport } from '../lib/render.js';

const argv = process.argv.slice(2);
if (!argv.length || argv.includes('--help')) {
  console.log('Usage: richdoc input.md [output.png|output.pdf] [--format png|pdf|both] [--output-dir DIR]\nDefault: mobile PNG long image(s) under /workspace/artifacts.');
  process.exit(argv.length ? 0 : 1);
}
try {
  const file_path = path.resolve(argv.shift());
  const destination = argv[0] && !argv[0].startsWith('--') ? path.resolve(argv.shift()) : undefined;
  const options = { file_path };
  if (destination) {
    const ext = path.extname(destination).slice(1).toLowerCase();
    if (!['png', 'pdf'].includes(ext)) throw new Error('Output filename must end in .png or .pdf');
    Object.assign(options, { format: ext, output_dir: path.dirname(destination), name: path.basename(destination, '.' + ext) });
  }
  while (argv.length) {
    const flag = argv.shift();
    if (!['--format', '--output-dir'].includes(flag) || !argv.length) throw new Error(`Unknown or incomplete option: ${flag}`);
    options[flag === '--format' ? 'format' : 'output_dir'] = argv.shift();
  }
  const result = await renderReport(options);
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error(`richdoc: ${error.message}`);
  process.exitCode = 1;
}
