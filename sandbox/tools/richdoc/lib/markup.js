import MarkdownIt from 'markdown-it';
import katex from 'katex';
import sanitizeHtml from 'sanitize-html';
import { randomBytes } from 'node:crypto';
import { renderKLine } from './kline.js';

// Public contract: compileMarkup(markup) resolves to { html, errors: string[] }.
// Generated charts contain JSON options in .rr-chart[data-chart]; Mermaid source
// is escaped inside pre.mermaid. Only the bundled renderer may activate either.
const components = new Set(['Chart', 'KLine', 'Mermaid', 'Callout', 'Stat', 'Stats', 'Table', 'Columns', 'Sources', 'Timeline']);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const safeUrl = (value) => {
  try {
    const url = new URL(String(value));
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
};

// Scan a JSX-like tag without evaluating JavaScript. Braces and quoted strings
// may span lines; a mismatched expression stops at the next tag boundary.
function tagAt(source, start) {
  const match = /^<(\/)?([A-Z][\w-]*)\b/.exec(source.slice(start));
  if (!match) return null;
  let quote = null;
  let escaped = false;
  const stack = [];
  for (let i = start + match[0].length; i < source.length; i++) {
    const char = source[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (char === '{' || char === '[') stack.push(char);
    else if (char === '}' || char === ']') {
      if (stack.pop() !== (char === '}' ? '{' : '[')) return { name: match[2], end: i + 1, malformed: true };
    } else if (char === '>' && !stack.length) {
      const raw = source.slice(start + match[0].length, i);
      return { name: match[2], closing: Boolean(match[1]), self: /\/\s*$/.test(raw), raw: raw.replace(/\/\s*$/, ''), end: i + 1 };
    } else if (char === '<') break;
  }
  const boundary = source.slice(start).search(/[>\n]/);
  return { name: match[2], end: boundary < 0 ? source.length : start + boundary + 1, malformed: true };
}

function codeEnd(source, pos) {
  if (pos === 0 || source[pos - 1] === '\n') {
    const newline = source.indexOf('\n', pos);
    const lineEnd = newline < 0 ? source.length : newline + 1;
    const line = source.slice(pos, lineEnd);
    if (/^(?: {4}|\t)/.test(line)) return lineEnd;
    const fence = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      const closer = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}[ \\t]*\\r?$`, 'gm');
      closer.lastIndex = lineEnd;
      const match = closer.exec(source);
      return match ? match.index + match[0].length : source.length;
    }
  }
  if (source[pos] === '`') {
    const run = /^`+/.exec(source.slice(pos))[0];
    let cursor = pos + run.length;
    while ((cursor = source.indexOf(run, cursor)) >= 0) {
      if (source[cursor - 1] !== '`' && source[cursor + run.length] !== '`') return cursor + run.length;
      cursor += run.length;
    }
  }
  return pos;
}

function parseProps(raw) {
  const props = Object.create(null);
  let pos = 0;
  while (pos < raw.length) {
    while (/\s/.test(raw[pos] || '') && pos < raw.length) pos++;
    if (pos === raw.length) break;
    const key = /^[A-Za-z][\w-]*/.exec(raw.slice(pos));
    if (!key) throw new Error('Expected a property name');
    pos += key[0].length;
    while (/\s/.test(raw[pos] || '') && pos < raw.length) pos++;
    if (raw[pos] !== '=') { props[key[0]] = true; continue; }
    pos++;
    while (/\s/.test(raw[pos] || '') && pos < raw.length) pos++;
    const first = raw[pos];
    if (first === '"' || first === "'") {
      let value = '';
      let closed = false;
      for (pos++; pos < raw.length; pos++) {
        if (raw[pos] === first) { pos++; closed = true; break; }
        if (raw[pos] === '\\' && pos + 1 < raw.length) pos++;
        value += raw[pos];
      }
      if (!closed) throw new Error('Unclosed quoted property');
      props[key[0]] = value;
    } else if (first === '{') {
      const begin = ++pos;
      let depth = 1;
      let quoted = false;
      let escaped = false;
      for (; pos < raw.length; pos++) {
        const char = raw[pos];
        if (quoted) {
          if (escaped) escaped = false;
          else if (char === '\\') escaped = true;
          else if (char === '"') quoted = false;
        } else if (char === '"') quoted = true;
        else if (char === '{') depth++;
        else if (char === '}' && --depth === 0) break;
      }
      if (depth) throw new Error('Unclosed JSON property');
      try { props[key[0]] = JSON.parse(raw.slice(begin, pos)); }
      catch { throw new Error(`Property ${key[0]} must contain strict JSON, not JavaScript`); }
      pos++;
    } else {
      throw new Error(`Property ${key[0]} must use a quoted string or {JSON}`);
    }
  }
  return props;
}

const chartTypes = new Set(['line', 'bar', 'pie', 'scatter', 'effectScatter', 'radar', 'heatmap', 'candlestick']);
const optionKeys = new Set(['title', 'tooltip', 'legend', 'grid', 'xAxis', 'yAxis', 'series', 'dataset', 'color', 'radar', 'polar', 'angleAxis', 'radiusAxis', 'visualMap', 'dataZoom', 'backgroundColor', 'textStyle']);
function cleanChartValue(value, key = '') {
  if (['__proto__', 'prototype', 'constructor', 'url', 'image', 'link', 'sublink', 'href', 'renderItem', 'onclick'].includes(key)) return undefined;
  if (typeof value === 'string') return /(?:image:\/\/|url\s*\()/i.test(value) ? undefined : value;
  if (value === null || typeof value === 'number' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) return value.map((item) => cleanChartValue(item)).filter((item) => item !== undefined);
  if (value && typeof value === 'object') {
    const result = Object.create(null);
    for (const [childKey, child] of Object.entries(value)) {
      const cleaned = cleanChartValue(child, childKey);
      if (cleaned !== undefined) result[childKey] = cleaned;
    }
    if (key === 'tooltip') result.renderMode = 'richText';
    return result;
  }
  return undefined;
}
function chartOption(props) {
  let option;
  if (props.option !== undefined) {
    if (!props.option || typeof props.option !== 'object' || Array.isArray(props.option)) throw new Error('Chart option must be a JSON object');
    option = Object.create(null);
    for (const [key, value] of Object.entries(props.option)) if (optionKeys.has(key)) option[key] = cleanChartValue(value);
  } else {
    const type = props.type || 'line';
    if (!chartTypes.has(type)) throw new Error(`Unsupported Chart type: ${type}`);
    const data = props.data ?? [];
    if (!Array.isArray(data)) throw new Error('Chart data must be an array');
    const labels = props.labels ?? data.map((row, i) => row && typeof row === 'object' && !Array.isArray(row) ? row.name ?? row.label ?? String(i + 1) : String(i + 1));
    const values = data.map((row) => row && typeof row === 'object' && !Array.isArray(row) ? row.value : row);
    const series = props.series ?? [{ type, data: type === 'pie' ? data : values }];
    option = { ...(type === 'pie' || type === 'radar' ? {} : { xAxis: { type: 'category', data: labels }, yAxis: { type: 'value' } }), series };
    if (props.title) option.title = { text: String(props.title) };
    option = cleanChartValue(option);
  }
  const series = Array.isArray(option.series) ? option.series : option.series ? [option.series] : [];
  if (!series.length) throw new Error('Chart requires at least one series');
  for (const item of series) {
    if (!item || !chartTypes.has(item.type)) throw new Error('Chart series must use a supported built-in type');
    item.animation = false;
  }
  option.series = series;
  option.animation = false;
  option.tooltip = { ...(option.tooltip && !Array.isArray(option.tooltip) ? option.tooltip : {}), renderMode: 'richText' };
  return option;
}

function enhanceTable(html) {
  return html.replace(/<td([^>]*)>([\s\S]*?)<\/td>/g, (all, attrs, body) => {
    const text = body.replace(/<[^>]*>/g, '').trim();
    if (!/^[+−-]?(?:[¥￥$€£]\s*)?\d[\d,]*(?:\.\d+)?(?:%|万|亿|元|倍|bp)?$/.test(text)) return all;
    const change = /^[+]/.test(text) ? ' rr-positive' : /^[-−]/.test(text) ? ' rr-negative' : '';
    return `<td${attrs} class="rr-number${change}">${body}</td>`;
  });
}

/** Compile report markup without executing model-provided scripts or expressions. */
export async function compileMarkup(markup) {
  const errors = [];
  const prefix = `RR${randomBytes(12).toString('hex')}TOKEN`;
  const stash = [];
  const store = (html) => { const token = `${prefix}${stash.length}END`; stash.push(html); return token; };
  const error = (message) => {
    errors.push(message);
    return `<aside class="rr-error" role="note">${escapeHtml(message)}</aside>`;
  };
  const md = new MarkdownIt({ html: true, linkify: true, breaks: false });
  md.renderer.rules.image = (tokens, index) => escapeHtml(tokens[index].content || 'Image omitted (offline report)');
  md.inline.ruler.after('escape', 'report_math', (state, silent) => {
    if (state.src[state.pos] !== '$' || state.src[state.pos + 1] === '$') return false;
    let end = state.pos + 1;
    while ((end = state.src.indexOf('$', end)) !== -1 && state.src[end - 1] === '\\') end++;
    if (end < 0 || end === state.pos + 1 || state.src.slice(state.pos + 1, end).includes('\n')) return false;
    if (!silent) {
      const token = state.push('report_math', '', 0);
      token.content = state.src.slice(state.pos + 1, end);
    }
    state.pos = end + 1;
    return true;
  });
  const math = (source, displayMode) => {
    try { return store(katex.renderToString(source, { displayMode, throwOnError: true, trust: false, strict: 'warn', output: 'htmlAndMathml' })); }
    catch (cause) { return store(error(`Math: ${cause.message}`)); }
  };
  md.renderer.rules.report_math = (tokens, index) => math(tokens[index].content, false);
  md.block.ruler.before('fence', 'report_display_math', (state, startLine, endLine, silent) => {
    const start = state.bMarks[startLine] + state.tShift[startLine];
    const first = state.src.slice(start, state.eMarks[startLine]);
    if (!first.startsWith('$$')) return false;
    let content = first.slice(2);
    let next = startLine + 1;
    let closed = content.trimEnd().endsWith('$$');
    if (closed) content = content.trimEnd().slice(0, -2);
    while (!closed && next < endLine) {
      const line = state.src.slice(state.bMarks[next] + state.tShift[next], state.eMarks[next]);
      next++;
      const closing = line.indexOf('$$');
      if (closing >= 0) { content += `\n${line.slice(0, closing)}`; closed = true; }
      else content += `\n${line}`;
    }
    if (!closed) return false;
    if (silent) return true;
    const token = state.push('report_display_math', '', 0);
    token.content = content;
    state.line = next;
    return true;
  });
  md.renderer.rules.report_display_math = (tokens, index) => `<p>${math(tokens[index].content, true)}</p>\n`;

  function markdown(source) {
    const rendered = md.render(source);
    return sanitizeHtml(rendered, {
      allowedTags: ['p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 's', 'del', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'span', 'div', 'details', 'summary', 'sup', 'sub', 'kbd'],
      allowedAttributes: { a: ['href', 'title'], code: ['class'], th: ['align'], td: ['align'], ol: ['start'] },
      allowedSchemes: ['https', 'http', 'mailto'],
      allowProtocolRelative: false,
      disallowedTagsMode: 'discard',
    });
  }
  function restore(html) {
    // Nested bodies may introduce more placeholders. Descending replacement
    // expands a parent before the earlier child tokens it contains.
    for (let i = stash.length - 1; i >= 0; i--) {
      const token = `${prefix}${i}END`;
      html = html.split(`<p>${token}</p>`).join(stash[i]).split(token).join(stash[i]);
    }
    return html;
  }
  async function renderComponent(name, props, body, depth) {
    const inner = () => compileBody(body, depth + 1);
    const title = props.title ? `<h3>${escapeHtml(props.title)}</h3>` : '';
    switch (name) {
      case 'Chart': return `<section class="rr-card">${title}<div class="rr-chart" data-chart="${escapeHtml(JSON.stringify(chartOption(props)))}"></div></section>`;
      case 'KLine': return `<section class="rr-card rr-kline">${await renderKLine(props)}</section>`;
      case 'Mermaid': return `<section class="rr-card"><pre class="mermaid">${escapeHtml(body || props.code || '')}</pre></section>`;
      case 'Callout': {
        const kind = ['info', 'warn', 'risk', 'good'].includes(props.kind) ? props.kind : 'info';
        return `<aside class="rr-block rr-callout rr-callout-${kind}">${title}${await inner()}</aside>`;
      }
      case 'Stat': {
        const delta = props.delta;
        const number = typeof delta === 'number' ? delta : Number(String(delta).replace(/[%+,]/g, ''));
        const color = Number.isFinite(number) && number !== 0 ? number > 0 ? ' rr-positive' : ' rr-negative' : '';
        const deltaText = delta === undefined ? '' : typeof delta === 'number' && delta > 0 ? `+${delta}` : delta;
        return `<section class="rr-card rr-stat"><div class="rr-stat-label">${escapeHtml(props.label ?? props.title)}</div><div class="rr-stat-value">${escapeHtml(props.value)}</div>${delta === undefined ? '' : `<div class="rr-stat-change${color}">${escapeHtml(deltaText)}</div>`}${props.description ? `<div class="rr-stat-description">${escapeHtml(props.description)}</div>` : ''}</section>`;
      }
      case 'Stats': return `<section class="rr-block rr-stats">${await inner()}</section>`;
      case 'Columns': return `<section class="rr-block rr-columns">${await inner()}</section>`;
      case 'Table': return `<section class="rr-card rr-table">${title}${enhanceTable(await inner())}</section>`;
      case 'Sources': {
        if (props.items !== undefined && !Array.isArray(props.items)) throw new Error('Sources items must be an array');
        const items = (props.items || []).map((item) => {
          if (typeof item === 'string') return `<li>${escapeHtml(item)}</li>`;
          const url = safeUrl(item?.url);
          return `<li>${url ? `<a href="${escapeHtml(url)}">${escapeHtml(item?.title ?? item?.label ?? url)}</a>` : escapeHtml(item?.title ?? item?.label ?? '')}${item?.date ? ` <span>${escapeHtml(item.date)}</span>` : ''}</li>`;
        }).join('');
        return `<section class="rr-block rr-sources">${title}${items ? `<ol>${items}</ol>` : ''}${await inner()}</section>`;
      }
      case 'Timeline': {
        if (props.items !== undefined && !Array.isArray(props.items)) throw new Error('Timeline items must be an array');
        const items = [];
        for (const item of props.items || []) items.push(`<li><div class="rr-timeline-date">${escapeHtml(item?.date ?? item?.time)}</div><strong>${escapeHtml(item?.title)}</strong>${await compileBody(String(item?.body ?? item?.description ?? ''), depth + 1)}</li>`);
        return `<section class="rr-block rr-timeline">${title}${items.length ? `<ol>${items.join('')}</ol>` : ''}${await inner()}</section>`;
      }
      default: throw new Error(`Unknown component <${name}>`);
    }
  }
  async function compileBody(source, depth = 0) {
    if (depth > 40) return error('Component nesting exceeds 40 levels');
    let output = '';
    let pos = 0;
    // Code fences and inline code are opaque to the component scanner.
    while (pos < source.length) {
      const opaqueEnd = codeEnd(source, pos);
      if (opaqueEnd > pos) {
        output += source.slice(pos, opaqueEnd);
        pos = opaqueEnd;
        continue;
      }
      if (source[pos] === '\\' && source[pos + 1] === '<') { output += '\\<'; pos += 2; continue; }
      const tag = source[pos] === '<' ? tagAt(source, pos) : null;
      if (!tag) { output += source[pos++]; continue; }
      if (tag.malformed || tag.closing) {
        output += `\n\n${store(error(`${tag.malformed ? 'Malformed' : 'Unexpected closing'} component <${tag.name}>`))}\n\n`;
        pos = tag.end; continue;
      }
      let body = '';
      let end = tag.end;
      let missing = false;
      if (!tag.self && tag.name === 'Mermaid') {
        // Diagram source is text, never nested report markup.
        const closing = /<\/Mermaid\s*>/g;
        closing.lastIndex = tag.end;
        const match = closing.exec(source);
        if (!match) missing = true;
        else { body = source.slice(tag.end, match.index); end = closing.lastIndex; }
      } else if (!tag.self) {
        const stack = [tag.name];
        let cursor = tag.end;
        let closeStart = -1;
        while (cursor < source.length) {
          const opaqueEnd = codeEnd(source, cursor);
          if (opaqueEnd > cursor) { cursor = opaqueEnd; continue; }
          if (source[cursor] === '\\' && source[cursor + 1] === '<') { cursor += 2; continue; }
          const next = cursor;
          const child = source[next] === '<' ? tagAt(source, next) : null;
          if (!child || child.malformed) { cursor++; continue; }
          if (child.name === 'Mermaid' && !child.closing && !child.self) {
            const closing = /<\/Mermaid\s*>/g;
            closing.lastIndex = child.end;
            if (!closing.exec(source)) break;
            cursor = closing.lastIndex;
            continue;
          }
          if (child.closing) {
            if (stack[stack.length - 1] !== child.name) break;
            stack.pop();
            if (!stack.length) { closeStart = next; end = child.end; break; }
          } else if (!child.self) stack.push(child.name);
          cursor = child.end;
        }
        if (closeStart < 0) missing = true;
        else body = source.slice(tag.end, closeStart);
      }
      let html;
      try {
        if (missing) throw new Error(`Missing closing tag </${tag.name}>`);
        if (!components.has(tag.name)) throw new Error(`Unknown component <${tag.name}>`);
        html = await renderComponent(tag.name, parseProps(tag.raw), body, depth);
      } catch (cause) {
        html = error(`${tag.name}: ${cause.message}`);
        if (body) html += await compileBody(body, depth + 1);
      }
      output += `\n\n${store(html)}\n\n`;
      pos = end;
    }
    return markdown(output);
  }
  const html = await compileBody(String(markup ?? ''));
  return { html: restore(html), errors };
}
