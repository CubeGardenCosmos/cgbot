import test from 'node:test';
import assert from 'node:assert/strict';
import { compileMarkup } from '../lib/markup.js';

const decodeAttribute = (value) => value.replace(/&(quot|#39|lt|gt|amp);/g, (_, name) => ({ quot: '"', '#39': "'", lt: '<', gt: '>', amp: '&' })[name]);
const chartOptions = (html) => [...html.matchAll(/<[^>]+\bclass="[^"]*\brr-chart\b[^"]*"[^>]*\bdata-chart="([^"]*)"[^>]*>/g)].map((match) => JSON.parse(decodeAttribute(match[1])));
const bar = { date: '2026-01-01', open: 10, high: 12, low: 9, close: 11, volume: 100 };

async function compile(source) {
  const result = await compileMarkup(source);
  assert.equal(typeof result.html, 'string');
  assert.ok(Array.isArray(result.errors));
  assert.ok(result.errors.every((error) => typeof error === 'string'));
  assert.doesNotMatch(result.html, /RR[a-f0-9]+TOKEN\d+END/, 'internal placeholders must be fully restored');
  return result;
}

test('Markdown headings, GFM tables, strikethrough, lists, links and quotations survive', async () => {
  const { html, errors } = await compile('# 中文报告\n\n**重点** and *emphasis* and ~~removed~~\n\n- 第一项\n- 第二项\n\n> 引用\n\n| 指标 | 值 |\n| --- | ---: |\n| 收益 | +12% |\n\n[来源](https://example.com/research)\n\nhttps://example.com/auto');
  assert.deepEqual(errors, []);
  for (const pattern of [/<h1\b[^>]*>中文报告<\/h1>/, /<strong>重点<\/strong>/, /<em>emphasis<\/em>/, /<(?:s|del)>removed<\/(?:s|del)>/, /<ul\b/, /<li\b/, /<blockquote\b/, /<table\b/, /<th\b/, /href="https:\/\/example.com\/research"/, /href="https:\/\/example.com\/auto"/]) assert.match(html, pattern);
});

test('inline and display math use KaTeX without consuming code or escaped dollars', async () => {
  const { html, errors } = await compile('Inline $x^2 + y^2$ and \\$5.\n\n$$\n\\frac{1}{2}\n$$\n\n`$not_math$`');
  assert.deepEqual(errors, []);
  assert.match(html, /class="katex"/);
  assert.match(html, /class="katex-display"/);
  assert.match(html, /<math\b/);
  assert.match(html, /<code>\$not_math\$<\/code>/);
  assert.match(html, /\$5/);
});

test('fenced, indented and inline code keep component and HTML examples inert', async () => {
  const { html, errors } = await compile('```jsx\n<Unknown />\n<Chart data={[1,2]} />\n<script>example()</script>\n```\n\n~~~text\n<Callout>literal</Callout>\n~~~\n\n    <Stat value="literal" />\n\n`<Mermaid />` and ``<Unknown code="`" />``');
  assert.deepEqual(errors, []);
  assert.match(html, /<pre><code class="language-jsx">/);
  assert.match(html, /&lt;Chart/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;Stat/);
  assert.match(html, /<code>&lt;Mermaid/);
  assert.equal(chartOptions(html).length, 0);
  assert.doesNotMatch(html, /class="mermaid"|class="rr-error"|<script\b/);
});

const components = [
  ['Chart', 'type="bar" data={[1,2,3]}', '', /\brr-chart\b/],
  ['KLine', `data={${JSON.stringify([bar])}}`, '', /<svg\b/],
  ['Mermaid', 'code="graph TD; A-->B"', 'graph TD; A-->B', /class="mermaid"/],
  ['Callout', 'title="提示" kind="warn"', '**正文**', /\brr-callout-warn\b/],
  ['Stat', 'label="营收" value="42" delta={3}', '', /\brr-stat\b/],
  ['Stats', '', '<Stat label="子指标" value="1" />', /\brr-stats\b/],
  ['Table', 'title="数据"', '\n\n| 指标 | 变化 |\n| --- | --- |\n| A | +2% |\n\n', /\brr-table\b/],
  ['Columns', '', '<Callout>左列</Callout>\n<Callout>右列</Callout>', /\brr-columns\b/],
  ['Sources', 'items={[{"title":"资料","url":"https://example.com","date":"2026"}]}', '补充来源', /\brr-sources\b/],
  ['Timeline', 'items={[{"date":"2026","title":"事件","body":"**详情**"}]}', '时间线尾注', /\brr-timeline\b/],
];
for (const [name, props, body, pattern] of components) {
  for (const paired of [false, true]) {
    test(`${name} supports ${paired ? 'paired' : 'self-closing'} component syntax`, async () => {
      const source = paired ? `<${name} ${props}>${body}</${name}>` : `<${name} ${props} />`;
      const { html, errors } = await compile(source);
      assert.deepEqual(errors, []);
      assert.match(html, pattern);
      assert.doesNotMatch(html, new RegExp(`<\\/?${name}\\b`));
      if (name === 'Chart') assert.equal(chartOptions(html)[0].series[0].type, 'bar');
      if (name === 'Stat') assert.match(html, /\brr-positive\b/);
      if (paired && name === 'Callout') assert.match(html, /<strong>正文<\/strong>/);
      if (paired && name === 'Table') assert.match(html, /\brr-number rr-positive\b/);
      if (name === 'Timeline') assert.match(html, /<strong>详情<\/strong>/);
    });
  }
}

test('nested JSON arrays, objects, escaped strings and multiline props retain chart data', async () => {
  const option = {
    title: { text: '含有 "引号"、} 和 ] 的标题' },
    xAxis: { type: 'category', data: ['甲', '乙'] },
    yAxis: { type: 'value' },
    series: [{ type: 'scatter', data: [[1, 2], [3, 4]], label: { show: true }, itemStyle: { color: '#123456' } }],
  };
  const { html, errors } = await compile(`<Columns>\n<Callout title='嵌套'>\n<Chart\n option={${JSON.stringify(option, null, 2)}}\n/>\n</Callout>\n</Columns>`);
  assert.deepEqual(errors, []);
  const [chart] = chartOptions(html);
  assert.equal(chart.title.text, option.title.text);
  assert.deepEqual(chart.xAxis.data, ['甲', '乙']);
  assert.deepEqual(chart.series[0].data, [[1, 2], [3, 4]]);
  assert.deepEqual(chart.series[0].label, { show: true });
  assert.equal(chart.series[0].itemStyle.color, '#123456');
  assert.equal(chart.animation, false);
  assert.equal(chart.tooltip.renderMode, 'richText');
});

for (const bad of ['<Unknown />', '<Unknown>保留正文</Unknown>', '<Callout title="missing close">', '</Callout>', '<Chart data={notJSON} />', '<Chart data={[1,2,]} />', '<Chart data={[1,2] />']) {
  test(`component errors recover before valid following content: ${bad}`, async () => {
    const { html, errors } = await compile(`${bad}\n\n## 后续报告\n\n<Stat label="恢复指标" value="42" />\n\n<Chart data={[3,4]} />\n\n尾部内容`);
    assert.ok(errors.length > 0);
    assert.match(html, /\brr-error\b/);
    assert.match(html, /<h2\b[^>]*>后续报告<\/h2>/);
    assert.match(html, /恢复指标/);
    assert.match(html, /尾部内容/);
    assert.equal(chartOptions(html).length, 1);
    assert.deepEqual(chartOptions(html)[0].series[0].data, [3, 4]);
  });
}

test('document HTML removes scripts, handlers, unsafe links and all remote images', async () => {
  const { html } = await compile('<script>globalThis.documentAttack = true</script>\n\n<div onclick="alert(1)" onmouseover="alert(2)">可见内容</div>\n\n<a href="javascript:alert(1)" onclick="alert(1)">坏链接</a>\n\n<a href="https://example.com/safe">好链接</a>\n\n<img src="https://example.com/tracker.png" onerror="alert(1)">\n\n![图片替代文字](https://example.com/markdown.png)\n\n<iframe src="https://example.com"></iframe>');
  assert.match(html, /可见内容/);
  assert.match(html, /图片替代文字/);
  assert.match(html, /href="https:\/\/example.com\/safe"/);
  assert.doesNotMatch(html, /<script\b|documentAttack|\bon\w+\s*=|javascript:|<img\b|<iframe\b|tracker\.png|markdown\.png/i);
});

test('component strings and Mermaid source are escaped, not executable HTML', async () => {
  const payload = '<img src=x onerror="alert(1)"><script>alert(1)</script>';
  const { html, errors } = await compile(`<Stat label={${JSON.stringify(payload)}} value={${JSON.stringify(payload)}} />\n<Mermaid code={${JSON.stringify(payload)}} />\n<Sources items={[{"title":"unsafe","url":"javascript:alert(1)"}]} />`);
  assert.deepEqual(errors, []);
  assert.match(html, /&lt;img/);
  assert.match(html, /class="mermaid"/);
  assert.doesNotMatch(html, /<img\b|<script\b|href="javascript:/i);
});

test('chart JSON drops remote images and executable or navigable option fields', async () => {
  const option = { title: { text: '安全图表', link: 'javascript:alert(1)' }, series: [{ type: 'bar', data: [1, 2], symbol: 'image://https://example.com/pixel.png', itemStyle: { image: 'https://example.com/texture.png' }, onclick: 'alert(1)' }] };
  const { html, errors } = await compile(`<Chart option={${JSON.stringify(option)}} />`);
  assert.deepEqual(errors, []);
  const [chart] = chartOptions(html);
  assert.equal(chart.title.text, '安全图表');
  assert.deepEqual(chart.series[0].data, [1, 2]);
  assert.equal(chart.title.link, undefined);
  assert.equal(chart.series[0].symbol, undefined);
  assert.equal(chart.series[0].onclick, undefined);
  assert.equal(chart.series[0].itemStyle.image, undefined);
});
