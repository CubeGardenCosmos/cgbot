---
name: rich-report
description: Render Chinese-first Markdown reports with K-line and share charts into offline PNG long images (PDF on request) with the report tools (render_report, kline_chart; richdoc underneath), then deliver them through the bridge MCP report_progress tool.
---

# rich-report

Use the MCP tools first. They are in your tool list; this file explains the markup and the rules they enforce.

- `kline_chart`: draws an OHLCV K-line PNG from `data` or `file_path`. Use it for every index and stock chart.
- `render_report`: renders Markdown (`markdown` or `markdown_path`) to PNG (default) or PDF. Put `<KLine/>` and `<Chart/>` components in the Markdown.
- The `richdoc` CLI sits underneath both tools. Call it directly only when the tools are unavailable: `richdoc input.md [output.png|output.pdf] [--format png|pdf|both] [--output-dir DIR]`.

## Workflow

1. Get data with `market_data` (see the findata skill). Never fetch market data by hand.
2. Draw each K-line with `kline_chart`, passing the bars file from `market_data` as `file_path`.
3. Write the report as UTF-8 Markdown under `/workspace/artifacts/<name>.md`, or pass the text as `markdown`, and call `render_report`.
4. Read the tool result. It lists the files, their pixel sizes and page counts, and any `richdoc errors`. Fix the Markdown or data and render again until there are no errors.
5. Deliver the PNG/PDF paths the tool returned with the bridge `report_progress` tool, in `files`, as absolute paths. Paths must be inside `/workspace` or `/home/agent`; the bridge rejects others.

Example delivery arguments:

```json
{"message":"结论与依据已整理为长图，末尾注明来源和不确定性。","files":["/workspace/artifacts/brief.png"],"status":"completed"}
```

The model behind this sandbox is text-only. Do NOT try to open or view PNGs. Review the Markdown source and the tool's reported errors instead.

Renderer limits: each output PNG is exactly 1080px wide and is split into multiple images for long content. Files outside `/workspace` and `/home/agent` can be previewed locally but cannot be delivered.

## Rendering rules

- The renderer validates after browser rendering and before writing: horizontal overflow, clipped text (including vertical card cutoffs), failed or zero-size images, empty charts, replacement characters, and font-load failures. A failure rejects output. Fix the content and re-render; never bypass the gate.
- Validation does not prove glyph coverage and does not replace viewing the image.
- Runtime is offline: JS, CSS, Chinese fonts, KaTeX, Mermaid and charts come from bundled npm assets. Do not reference CDNs, remote images, web fonts or stylesheets. Links are citations, not resources to load.
- No model-generated scripts: no `<script>`, event handlers, `javascript:` URLs, imports, `eval`, iframes, callbacks, or functions in JSON. Use trusted components and plain data only. Unknown components or malformed JSON produce visible diagnostics; fix them rather than hiding errors.

## Markup reference

Use ordinary Markdown for title, sections, lists, links, quotes and code. Use compact tables with explicit units.

Color convention: 上涨一律绿色、下跌一律红色 for every company and market. Keep +/− signs on signed changes (color alone is insufficient). Do not use the red-up `cn` convention unless the user explicitly asks for it. `kline_chart` defaults to this convention (`colors` intl).

Component props must be quoted strings, booleans, or strict JSON in `{…}`: double-quoted keys, no trailing commas, no JavaScript expressions.

```jsx
<Callout kind="warn" title="不确定性">缺少原始证据。</Callout>
<Stats><Stat label="收入" value="128万" delta={12.8} /><Stat label="成本" value="-3.2%" delta={-3.2} /></Stats>
<Table>
| 指标 | 同比 |
| --- | ---: |
| 甲 | +12.8% |
| 乙 | -3.2% |
</Table>
<Columns><Callout title="观察">数值上升。</Callout><Callout title="限制">不能推断因果。</Callout></Columns>
<Timeline items={[{"date":"2026-01","title":"核验","body":"统一口径。"}]} />
<Sources items={[{"title":"真实来源名","url":"https://example.com"}]} />
<Mermaid>
flowchart TD
  A[证据] --> B[核验]
  B --> C[结论]
</Mermaid>
```

`Stat.delta` is the signed numeric change. `Callout.kind` is one of `info`, `warn`, `risk`, `good`; choose the semantic kind rather than relying on color.

Math uses KaTeX: `$g=(R_t-R_0)/R_0$` inline or a `$$` display block.

## Share and bar charts

Commerce and market charts use K-lines plus horizontal or stacked bar share charts. Never use pie, donut or radar charts for them. The `Chart` component takes `data={[{"name":"甲","value":2}]}` for simple charts, or a data-only ECharts `option`. Area uses `type="line"` with `areaStyle`. No formatter functions.

Horizontal bars put the category on `yAxis`. Stacked shares use one `stack` name:

```jsx
<Chart type="bar" title="成交占比" option={{"xAxis":{"type":"value","max":100},"yAxis":{"type":"category","data":["甲","乙"]},"series":[{"name":"占比","type":"bar","stack":"share","data":[60,40]}]}} />
```

## Stock charts (mandatory for market reports)

Any report about stocks or market conditions MUST include a K-line chart for every index and every individual stock discussed, built from real daily OHLCV data (at least 60 daily bars, never fabricated). Tables or line charts do not replace it. If data is unobtainable for an item, state that explicitly.

Use `kline_chart` for each one. Pass `file_path` from `market_data kline`, plus `title`, `symbol`, optional `ma` (default 5, 10, 20, 60), and optional `markers` (support and resistance levels). The same K-line is available as a `<KLine/>` tag inside `render_report` Markdown:

```jsx
<KLine title="示例" symbol="DEMO" ma={[5,10,20,60]} data={[{"date":"2026-01-01","open":100,"high":104,"low":98,"close":102,"volume":12000}]} markers={[{"price":98,"type":"support","label":"支撑"},{"price":104,"type":"resistance","label":"阻力"}]} />
```

`data` rows need ordered dates; OHLCV must be finite and consistent. The renderer draws the latest 500 bars of longer input. Cite the printed source, last trading date, data timestamp and adjustment basis; never relabel a prior session as today's data. Keep one market per report task.

## Forecasts

Forecasts are allowed only when they are visibly separated from measured data:

- Pass them through `kline_chart forecast`, or as a `forecast` prop on `<KLine/>`: `{label, start, end, series}`. `start` must be after the last bar date, `series` holds 1–250 values (numbers or `{date, close}`), and `label` must contain `非实测` (the default is `预测(非实测)`).
- The forecast is drawn as a dashed indigo line over a shaded band and labelled `预测至 <end>`. Its legend reads `预测(非实测)`. Do not restyle it.
- State the forecast basis (method, inputs, horizon) and its uncertainty in the body text, next to the chart.
- Never draw a forecast as a solid line, never give it red or green, and never mix forecast colours with measured data.

## Example inputs

Under `/opt/cgbot/tools/richdoc/examples/`:

- `stock.md`: 120 deterministic **FAKE** bars embedded as KLine JSON, plus stats, sector bar, risk and sources. `stock.fake.json` is the same OHLCV array.
- `research.md`: **FAKE** signed table, radar/line, Mermaid, math, timeline, columns and extra chart types.
- `failures.md`: deliberately invalid inputs for diagnostic review. Not a successful report.

Render an example to check the toolchain: `richdoc /opt/cgbot/tools/richdoc/examples/research.md /workspace/artifacts/example.png`. Do not deliver FAKE examples as analysis.

## Manual review before delivery

Without image input, check the source and the tool output for every split image. Check:

- text and labels do not overflow; cards and sections are not cut off;
- Chinese and other glyphs are readable, with no replacement characters or tofu;
- no empty or uninitialized charts;
- increases are green and decreases red, with signs and labels;
- every user-requested data item is present with real observation or trading dates, units and sources, or is explicitly marked unavailable;
- forecasts are labelled `预测(非实测)` with basis and uncertainty stated;
- split boundaries and uncertainty wording are correct.

If anything fails, fix the content or layout and re-render. Never claim a visual check you did not perform; state that the layout was verified structurally, not by eye.
