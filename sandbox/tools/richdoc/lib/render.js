import { readFile, writeFile, mkdir, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { chromium } from 'playwright';
import sharp from 'sharp';
import { PDFDocument } from 'pdf-lib';
import { compileMarkup } from './markup.js';
import { inspectReportLayout, measureReportHeight, assertPngMetadata } from './validate.js';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const WIDTH = 540;
const DPR = 2;
const MAX_HEIGHT = 12000 / DPR;
const MAX_BYTES = Math.floor(4.8 * 1024 * 1024);

async function asset(candidates) {
  for (const candidate of candidates) {
    try { return await readFile(require.resolve(candidate), 'utf8'); }
    catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND' && error.code !== 'ENOENT' && error.code !== 'ERR_PACKAGE_PATH_NOT_EXPORTED') throw error;
    }
  }
  throw new Error(`Required local browser asset is missing: ${candidates.join(', ')}`);
}

async function inlineCss(specifier) {
  const filename = require.resolve(specifier);
  const css = await readFile(filename, 'utf8');
  const matches = [...css.matchAll(/url\(\s*['"]?([^)'"\s]+)['"]?\s*\)/g)];
  const replacements = await Promise.all(matches.map(async ([original, url]) => {
    if (/^data:/i.test(url)) return [original, original];
    if (/^(?:[a-z]+:|\/\/)/i.test(url)) throw new Error(`Nonlocal CSS asset rejected: ${url}`);
    const bytes = await readFile(path.resolve(path.dirname(filename), url));
    const extension = path.extname(url).slice(1);
    const mime = { woff2: 'font/woff2', woff: 'font/woff', ttf: 'font/ttf', otf: 'font/otf' }[extension];
    if (!mime) throw new Error(`Unsupported local font asset: ${url}`);
    return [original, `url("data:${mime};base64,${bytes.toString('base64')}")`];
  }));
  let result = css;
  for (const [original, replacement] of replacements) result = result.replace(original, replacement);
  return result;
}

async function browserPath(explicit) {
  if (explicit) return explicit;
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  for (const candidate of ['/usr/lib/chromium/chromium', '/usr/lib64/chromium-browser/chromium-browser', '/usr/lib/chromium-browser/chromium-browser', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    try { await access(candidate, constants.X_OK); return candidate; } catch { /* Try the next local installation. */ }
  }
  return undefined;
}

function escapeStyle(css) { return css.replace(/<\/style/gi, '<\\/style'); }

/** Render compiler-sanitized report markup without allowing any network access. */
export async function renderReport({ markup, file_path, format = 'png', output_dir = '/workspace/artifacts', name = 'report', executablePath, signal } = {}) {
  const errors = [];
  let browser;
  const abort = () => { if (browser) void browser.close().catch(() => {}); };
  try {
    if (!['png', 'pdf', 'both'].includes(format)) throw new Error('format must be png, pdf, or both');
    if ((markup != null) === (file_path != null)) throw new Error('Provide exactly one of markup or file_path');
    if (typeof name !== 'string' || !/^[\p{L}\p{N}_-][\p{L}\p{N}_.-]{0,99}$/u.test(name)) throw new Error('name must be a safe basename of 1–100 letters, digits, dots, underscores, or hyphens');
    if (signal?.aborted) throw new Error('Rendering cancelled');
    if (file_path != null) markup = await readFile(file_path, 'utf8');
    if (typeof markup !== 'string' || !markup.trim()) throw new Error('Provide nonempty markup or file_path');
    const compiled = await compileMarkup(markup);
    errors.push(...(compiled.errors || []).map(String));
    const [css, katexCss, fontRegular, fontBold, echarts, mermaid] = await Promise.all([
      readFile(path.join(here, 'style.css'), 'utf8'),
      inlineCss('katex/dist/katex.min.css'),
      inlineCss('@fontsource/noto-sans-sc/400.css'),
      inlineCss('@fontsource/noto-sans-sc/700.css'),
      asset(['echarts/dist/echarts.min.js']),
      asset(['mermaid/dist/mermaid.min.js', 'mermaid/dist/mermaid.js']),
    ]);
    browser = await chromium.launch({ executablePath: await browserPath(executablePath), headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-sync'] });
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) throw new Error('Rendering cancelled');
    const context = await browser.newContext({ viewport: { width: WIDTH, height: 900 }, deviceScaleFactor: DPR, locale: 'zh-CN', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block', offline: true });
    // Route even file:// requests; only explicitly inlined data and script strings are used.
    await context.route('**/*', route => route.abort('blockedbyclient'));
    const page = await context.newPage();
    await page.setContent(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"><style>${escapeStyle(fontRegular + fontBold + katexCss + css)}</style></head><body>${compiled.html}</body></html>`, { waitUntil: 'load' });
    await page.addScriptTag({ content: echarts });
    await page.addScriptTag({ content: mermaid });
    const renderErrors = await page.evaluate(async () => {
      const failures = [];
      if (!document.querySelector('.rr-report')) {
        const root = document.createElement('main');
        root.className = 'rr-report';
        root.append(...document.body.childNodes);
        document.body.append(root);
      }
      await document.fonts.ready;
      const rejectUnsafe = value => {
        if (typeof value === 'string' && /(?:\b(?:https?|file|ftp|javascript|data):|\/\/|url\s*\(|image:\/\/)/i.test(value)) throw new Error('Chart URLs and external resources are not allowed');
        if (Array.isArray(value)) { value.forEach(rejectUnsafe); return; }
        if (value && typeof value === 'object') {
          for (const [key, child] of Object.entries(value)) {
            if (['__proto__', 'prototype', 'constructor', 'renderItem'].includes(key)) throw new Error(`Unsupported chart property: ${key}`);
            rejectUnsafe(child);
          }
        }
      };
      for (const [index, element] of [...document.querySelectorAll('.rr-chart')].entries()) {
        try {
          const option = JSON.parse(element.dataset.chart || '{}');
          if (!option || typeof option !== 'object' || Array.isArray(option)) throw new Error('Chart option must be an object');
          const seriesList = Array.isArray(option.series) ? option.series : option.series ? [option.series] : [];
          const datasets = Array.isArray(option.dataset) ? option.dataset : option.dataset ? [option.dataset] : [];
          if (!seriesList.length || !seriesList.some(series => Array.isArray(series.data) && series.data.length > 0 || datasets.some(dataset => Array.isArray(dataset.source) && dataset.source.length > 0))) {
            throw new Error('Chart has no series data; supply fetched data, not an empty chart placeholder');
          }
          rejectUnsafe(option);
          option.animation = false;
          option.textStyle = { ...option.textStyle, fontFamily: 'Noto Sans CJK SC, Noto Sans SC, sans-serif', fontSize: Math.max(18, Number(option.textStyle?.fontSize) || 18) };
          if (option.xAxis || option.yAxis || option.grid) {
            const fitGrid = grid => ({ left: 12, right: 12, top: option.title ? 80 : 20, bottom: 20, ...grid, containLabel: true });
            option.grid = Array.isArray(option.grid) ? option.grid.map(fitGrid) : fitGrid(option.grid);
          }
          const readable = (style, minimum = 18) => ({ ...style, fontSize: Math.max(minimum, Number(style?.fontSize) || minimum) });
          for (const key of ['legend', 'visualMap', 'title', 'radar']) {
            if (!option[key]) continue;
            const components = Array.isArray(option[key]) ? option[key] : [option[key]];
            for (const component of components) {
              if (key === 'radar') component.axisName = { ...readable(component.axisName), color: '#334155' };
              else component.textStyle = { ...readable(component.textStyle, key === 'title' ? 22 : 18), ...(key === 'legend' ? { color: '#334155' } : {}) };
              if (key === 'title') component.subtextStyle = readable(component.subtextStyle);
            }
          }
          for (const key of ['xAxis', 'yAxis', 'radiusAxis', 'angleAxis', 'singleAxis']) {
            if (!option[key]) continue;
            const axes = Array.isArray(option[key]) ? option[key] : [option[key]];
            for (const axis of axes) {
              axis.axisLabel = { ...readable(axis.axisLabel), color: '#334155' };
              axis.nameTextStyle = { ...readable(axis.nameTextStyle), color: '#334155' };
            }
          }
          for (const series of (Array.isArray(option.series) ? option.series : option.series ? [option.series] : [])) {
            series.animation = false;
            series.progressive = 0;
            series.silent = true;
            series.label = readable(series.label);
            if (series.emphasis?.label) series.emphasis.label = readable(series.emphasis.label);
          }
          option.tooltip = { show: false };
          window.echarts.init(element, null, { renderer: 'svg', width: element.clientWidth, height: 380 }).setOption(option);
          element.removeAttribute('data-chart');
        } catch (error) {
          const message = `Chart ${index + 1}: ${error.message}`;
          failures.push(message);
          element.className = 'rr-error';
          element.textContent = message;
        }
      }
      window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'default', deterministicIds: true, deterministicIDSeed: 'rich-report', fontFamily: 'Noto Sans CJK SC, Noto Sans SC, sans-serif', themeVariables: { fontSize: '22px' }, flowchart: { htmlLabels: false }, suppressErrorRendering: true });
      for (const [index, element] of [...document.querySelectorAll('pre.mermaid, .mermaid')].entries()) {
        try {
          if (/%%\s*\{|^\s*---/m.test(element.textContent)) throw new Error('Mermaid configuration directives are not allowed');
          const { svg } = await window.mermaid.render(`rr-mermaid-${index}`, element.textContent);
          element.innerHTML = svg;
        } catch (error) {
          const message = `Mermaid ${index + 1}: ${error.message}`;
          failures.push(message);
          element.className = 'rr-error';
          element.textContent = message;
        }
      }
      await document.fonts.ready;
      for (const value of document.querySelectorAll('.rr-stat-value')) {
        if (!/^[\s+\-−¥￥$€£\d,.%万亿兆元倍bp]+$/u.test(value.textContent) || !/\d/.test(value.textContent)) continue;
        value.style.whiteSpace = 'nowrap';
        value.style.overflowWrap = 'normal';
        if (value.scrollWidth > value.clientWidth) {
          const size = parseFloat(getComputedStyle(value).fontSize);
          value.style.fontSize = `${Math.max(22, Math.floor(size * value.clientWidth / value.scrollWidth))}px`;
        }
        if (value.scrollWidth > value.clientWidth) {
          const card = value.closest('.rr-stat');
          if (card && getComputedStyle(card.parentElement).display === 'grid') card.style.gridColumn = '1 / -1';
        }
      }
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return failures;
    });
    errors.push(...renderErrors);
    const layoutErrors = await page.evaluate(inspectReportLayout, { width: WIDTH });
    if (renderErrors.length || layoutErrors.length) {
      throw new Error(`Report validation failed before output:\n${layoutErrors.join('\n')}\nFix the content/layout and rerender at ${WIDTH * DPR}px physical width.`);
    }
    await mkdir(output_dir, { recursive: true });
    const basename = name;
    let pdf;
    let pdfPages = 0;
    if (format !== 'png') {
      pdf = await page.pdf({ width: '540px', height: '900px', printBackground: true, preferCSSPageSize: true });
      pdfPages = (await PDFDocument.load(pdf)).getPageCount();
      if (format === 'pdf') {
        const filename = path.resolve(output_dir, `${basename}.pdf`);
        await writeFile(filename, pdf);
        return { files: [filename], page_count: pdfPages, errors };
      }
      await page.emulateMedia({ media: 'screen' });
    }
    const height = await page.evaluate(measureReportHeight);
    const layout = await page.evaluate(height => {
      const root = document.querySelector('.rr-report');
      const atomic = '.rr-card, .rr-chart, table, pre, .mermaid, figure, p, li, h1, h2, h3, h4, h5, h6, blockquote, .katex-display';
      const rectangles = [...root.querySelectorAll(atomic)].map(element => element.getBoundingClientRect()).filter(rect => rect.height > 0);
      const points = new Set([0, height]);
      for (const element of root.querySelectorAll('*')) {
        if (element.closest('.rr-chart, table, pre, .mermaid, figure, .rr-card') !== element && element.closest('.rr-chart, table, pre, .mermaid, figure, .rr-card')) continue;
        if (!['block', 'list-item', 'grid', 'flex', 'table'].includes(getComputedStyle(element).display)) continue;
        const rect = element.getBoundingClientRect();
        for (const y of [Math.floor(rect.top), Math.ceil(rect.bottom)]) {
          if (y > 0 && y < height && !rectangles.some(other => other.top < y - 0.5 && other.bottom > y + 0.5)) points.add(y);
        }
      }
      return { height, boundaries: [...points].sort((a, b) => a - b) };
    }, height);
    const pending = [];
    let start = 0;
    while (start < layout.height) {
      const end = Math.max(...layout.boundaries.filter(point => point > start && point <= start + MAX_HEIGHT));
      if (!Number.isFinite(end)) throw new Error(`An indivisible report block exceeds ${MAX_HEIGHT}px CSS height. Shorten the card, chart, or table; it cannot be split safely.`);
      pending.push([start, end]);
      start = end;
    }
    const images = [];
    while (pending.length) {
      const [top, bottom] = pending.shift();
      const screenshot = await page.screenshot({ type: 'png', fullPage: true, clip: { x: 0, y: top, width: WIDTH, height: bottom - top }, animations: 'disabled', caret: 'hide', timeout: 60000 });
      const bytes = await sharp(screenshot).png({ compressionLevel: 9, adaptiveFiltering: true, palette: false }).toBuffer();
      if (bytes.length <= MAX_BYTES) { images.push(bytes); continue; }
      const middle = (top + bottom) / 2;
      const choices = layout.boundaries.filter(point => point > top && point < bottom);
      if (!choices.length) throw new Error('An indivisible report block exceeds the 4.8 MiB PNG delivery limit after lossless compression. Reduce the content of that card, chart, or table.');
      const split = choices.reduce((best, point) => Math.abs(point - middle) < Math.abs(best - middle) ? point : best);
      pending.unshift([top, split], [split, bottom]);
    }
    const files = [];
    // Decode every final encoded image first. No PNG or PDF is emitted if any image is invalid.
    for (const bytes of images) assertPngMetadata(await sharp(bytes).metadata(), WIDTH * DPR);
    for (const [index, bytes] of images.entries()) {
      const suffix = images.length > 1 ? `-${String(index + 1).padStart(2, '0')}` : '';
      const filename = path.resolve(output_dir, `${basename}${suffix}.png`);
      await writeFile(filename, bytes);
      files.push(filename);
    }
    if (pdf) {
      const filename = path.resolve(output_dir, `${basename}.pdf`);
      await writeFile(filename, pdf);
      files.push(filename);
    }
    return { files, page_count: images.length + pdfPages, errors };
  } catch (error) {
    errors.push(signal?.aborted ? 'Rendering cancelled' : `Render failed: ${error.message}`);
    throw new Error(errors.join('\n'), { cause: error });
  } finally {
    signal?.removeEventListener('abort', abort);
    if (browser) await browser.close().catch(() => {});
  }
}
