/** Runs inside Chromium. Returns the CSS height of the report content plus its bottom padding; never the viewport minimum. */
export function measureReportHeight() {
  const root = document.querySelector('.rr-report');
  if (!root) throw new Error('Missing .rr-report content root');
  let bottom = 0;
  // Descendants only: the root's own box keeps the last card's bottom margin, which is blank space.
  for (const element of root.querySelectorAll('*')) {
    const rect = element.getBoundingClientRect();
    if (rect.height > 0) bottom = Math.max(bottom, rect.bottom);
  }
  const paddingBottom = parseFloat(getComputedStyle(root).paddingBottom) || 0;
  return Math.ceil(bottom + paddingBottom);
}

 /** Runs inside Chromium after fonts/charts settle. Coordinates are CSS pixels, never DPR pixels. */
export function inspectReportLayout({ width = 540, tolerance = 1 } = {}) {
  const failures = new Set();
  const root = document.querySelector('.rr-report');
  if (!root) return ['Missing .rr-report content root'];
  const selectorToken = value => String(value).replace(/(^-?\d)|[^a-zA-Z0-9_-]/gu, token =>
    Array.from(token, character => `\\${character.codePointAt(0).toString(16)} `).join(''));
  const label = element => {
    const parts = [];
    for (let current = element; current; current = current.parentElement) {
      const className = typeof current.className === 'string' ? current.className : current.className?.baseVal || '';
      const classes = className.trim().split(/\s+/).filter(Boolean);
      parts.unshift(`${current.tagName.toLowerCase()}${current.id ? `#${selectorToken(current.id)}` : ''}${classes.map(name => `.${selectorToken(name)}`).join('')}`);
      if (current.id || classes.length || current === root) break;
    }
    return parts.join(' > ');
  };
  const textExcerpt = text => {
    // Keep diagnostics single-line and bounded, including untrusted chart labels.
    const normalized = text.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]+/gu, ' ').trim();
    return JSON.stringify(normalized.length > 120 ? `${normalized.slice(0, 120)}...` : normalized);
  };
  const fail = (element, reason, text) => failures.add(`${label(element)}${text === undefined ? '' : ` text ${textExcerpt(text)}`}: ${reason}`);
  const visible = element => {
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
  };
  const rootBounds = root.getBoundingClientRect();
  if (Math.abs(rootBounds.width - width) > tolerance || rootBounds.left < -tolerance || rootBounds.right > width + tolerance) {
    fail(root, `content root must remain ${width}px CSS wide at x=0 (got ${rootBounds.width}px at x=${rootBounds.left})`);
  }
  const elements = [root, ...root.querySelectorAll('*')];
  for (const element of elements) {
    if (!visible(element) || element.closest('.katex-mathml, script, style, defs, clipPath')) continue;
    // SVG plot paths are intentionally clipped. Only their text labels are checked below.
    if (element.namespaceURI === 'http://www.w3.org/2000/svg') continue;
    const rect = element.getBoundingClientRect();
    if (rect.width > 0 && (rect.left < rootBounds.left - tolerance || rect.right > rootBounds.right + tolerance)) {
      fail(element, `horizontal geometry exceeds the ${width}px CSS content root; wrap or shorten this block`);
    }
    if (element.clientWidth > 0 && element.scrollWidth > element.clientWidth + tolerance) {
      fail(element, 'horizontal content overflow; wrap labels/text or reduce columns (do not narrow the PNG)');
    }
    if (element.tagName === 'IMG' && (!element.complete || element.naturalWidth <= 0 || element.naturalHeight <= 0 || rect.width <= 0 || rect.height <= 0)) {
      fail(element, 'image failed to load or has zero size');
    }
    if (element.matches('.rr-chart, .mermaid, .rr-kline')) {
      const svg = element.querySelector('svg');
      if (rect.width <= 0 || rect.height <= 0 || !svg || !svg.querySelector('path, rect, circle, ellipse, line, polyline, polygon, text, image, use')) {
        fail(element, 'chart is empty, zero-sized, or failed initialization; supply real chart data and rerender');
      } else {
        const bounds = svg.getBoundingClientRect();
        if (bounds.width <= 0 || bounds.height <= 0) fail(element, 'chart SVG has zero size');
      }
    }
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const element = node.parentElement;
    if (!element || !node.textContent.trim() || !visible(element) || element.closest('script, style, defs, clipPath, .katex-mathml')) continue;
    // Ignore text deliberately hidden by an ancestor (e.g. accessibility MathML).
    let hidden = false;
    for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
      if (!visible(ancestor)) { hidden = true; break; }
    }
    if (hidden) continue;
    if (node.textContent.includes('\uFFFD')) fail(element, 'literal replacement character U+FFFD detected; repair text encoding', node.textContent);
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) {
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (rect.left < rootBounds.left - tolerance || rect.right > rootBounds.right + tolerance) fail(element, 'text extends beyond the content root; wrap or shorten it', node.textContent);
      for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        const bounds = ancestor.getBoundingClientRect();
        // SVG viewports/plot clipPaths intentionally clip geometry, not visible labels.
        if (ancestor.namespaceURI === 'http://www.w3.org/2000/svg' && ancestor.tagName.toLowerCase() !== 'svg') continue;
        const left = bounds.left + (ancestor.clientLeft || 0);
        const top = bounds.top + (ancestor.clientTop || 0);
        const right = left + ancestor.clientWidth;
        const bottom = top + ancestor.clientHeight;
        if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX) && ancestor.clientWidth > 0 && (rect.left < left - tolerance || rect.right > right + tolerance)) {
          fail(element, `text clipped horizontally by ${label(ancestor)}; expand or wrap the container`, node.textContent);
        }
        if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowY) && ancestor.clientHeight > 0 && (rect.top < top - tolerance || rect.bottom > bottom + tolerance)) {
          fail(element, `text clipped vertically by ${label(ancestor)}; remove the fixed height or shorten the card`, node.textContent);
        }
      }
    }
    range.detach();
  }
  for (const font of document.fonts) {
    if (font.status === 'error') failures.add(`Font failed to load: ${font.family}; repair the bundled font before rendering`);
  }
  return [...failures];
}

/** Validate decoded metadata for every encoded buffer before any artifact is written. */
export function assertPngMetadata(metadata, expectedWidth = 1080) {
  if (metadata.format !== 'png' || metadata.width !== expectedWidth || !Number.isInteger(metadata.height) || metadata.height <= 0) {
    throw new Error(`Invalid encoded PNG: expected ${expectedWidth}px width and positive height, got ${metadata.format || 'unknown'} ${metadata.width}×${metadata.height}. Never scale reports narrower.`);
  }
}
