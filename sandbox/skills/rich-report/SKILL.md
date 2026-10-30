---
name: rich-report
description: Render Chinese-first Markdown reports with charts into offline PNG long images (PDF on request) using the richdoc CLI, then deliver them through the bridge MCP report_progress tool.
---

# rich-report

Render a report offline with the `richdoc` CLI. The renderer is installed at `/opt/cgbot/tools/richdoc`, and `richdoc` is on PATH.

## Workflow

1. Write the report as UTF-8 Markdown (with optional components below) under `/workspace/artifacts/<name>.md`.
2. Render:

```sh
richdoc /workspace/artifacts/<name>.md /workspace/artifacts/<name>.png
richdoc input.md [output.png|output.pdf] [--format png|pdf|both] [--output-dir DIR]
```

3. Open every returned PNG yourself (see checklist below). Fix content or layout and re-render until clean.
4. Deliver through the bridge MCP tool `report_progress`, passing the PNG/PDF paths printed by richdoc in the `files` array as absolute paths, e.g. `/workspace/artifacts/<name>.png`. Delivered paths MUST be inside `/workspace` or `/home/agent`; the bridge rejects others.

Example delivery arguments:

```json
{"message":"结论与依据已整理为长图，末尾注明来源和不确定性。","files":["/workspace/artifacts/brief.png"],"status":"completed"}
```

Renderer limits: each output PNG is exactly 1080px wide and is split into multiple images for long content. Files outside `/workspace` and `/home/agent` can be previewed locally but cannot be delivered.

## Rendering rules

- The renderer validates after browser rendering and before writing: horizontal overflow, clipped text (including vertical card cutoffs), failed or zero-size images, empty charts, replacement characters, and font-load failures. A failure rejects output. Fix the content and re-render; never bypass the gate.
- Validation does not prove glyph coverage and does not replace viewing the image.
- Runtime is offline: JS, CSS, Chinese fonts, KaTeX, Mermaid and charts come from bundled npm assets. Do not reference CDNs, remote images, web fonts or stylesheets. Links are citations, not resources to load.
- No model-generated scripts: no `<script>`, event handlers, `javascript:` URLs, imports, `eval`, iframes, callbacks, or functions in JSON. Use trusted components and plain data only. Unknown components or malformed JSON produce visible diagnostics; fix them rather than hiding errors.

## Markup reference

Use ordinary Markdown for title, sections, lists, links, quotes and code. Use compact tables with explicit units.

Color convention: 上涨一律绿色、下跌一律红色 for every company and market. Keep +/− signs on signed changes (color alone is insufficient). Do not use the red-up `cn` convention unless the user explicitly asks for it.

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

Charts: `Chart` takes `data={[{"name":"甲","value":2}]}` for simple charts, or an explicit data-only ECharts `option` for radar, scatter, heatmap and similar. Area uses `type="line"` with `areaStyle`. No formatter functions.

```jsx
<Chart type="bar" title="行业" option={{"xAxis":{"type":"category","data":["科技","消费"]},"yAxis":{},"series":[{"type":"bar","data":[8,-2]}]}} />
<Chart type="heatmap" title="热力" option={{"xAxis":{"type":"category","data":["东","西"]},"yAxis":{"type":"category","data":["甲","乙"]},"visualMap":{"min":0,"max":10},"series":[{"type":"heatmap","data":[[0,0,8],[1,0,3],[0,1,5],[1,1,9]]}]}} />
```

## Stock charts (mandatory for market reports)

Any report about stocks or market conditions MUST include a K-line chart for every index and every individual stock discussed, built from real daily OHLCV data (about 60+ bars, never fabricated). Tables or line charts do not replace it. If data is unobtainable for an item, state that explicitly.

```jsx
<KLine title="示例" symbol="DEMO" ma={[5,10,20,60]} data={[{"date":"2026-01-01","open":100,"high":104,"low":98,"close":102,"volume":12000}]} markers={[{"price":98,"type":"support","label":"支撑"},{"price":104,"type":"resistance","label":"阻力"}]} />
```

`data` rows need ordered dates; OHLCV must be finite and consistent. Default moving averages `[5,10,20,60]`.

Get OHLCV with the preinstalled **findata** skill, not ad-hoc HTTP probes. Read `/opt/cgbot/skills/findata/SKILL.md` first, then for example `findata kline 600519 --market CN --limit 120 --out /workspace/artifacts/600519.json`. Pass the array to the `KLine` `data` prop. Cite the printed source, last trading date, data timestamp and adjustment basis; never relabel a prior session as today's data. Keep one market per report task.

## Example inputs

Under `/opt/cgbot/tools/richdoc/examples/`:

- `stock.md`: 120 deterministic **FAKE** bars embedded as KLine JSON, plus stats, sector bar, risk and sources. `stock.fake.json` is the same OHLCV array.
- `research.md`: **FAKE** signed table, radar/line, Mermaid, math, timeline, columns and extra chart types.
- `failures.md`: deliberately invalid inputs for diagnostic review. Not a successful report.

Render an example to check the toolchain: `richdoc /opt/cgbot/tools/richdoc/examples/research.md /workspace/artifacts/example.png`. Do not deliver FAKE examples as analysis.

## Manual review before delivery

Open every returned PNG, not only the first split image. Check:

- text and labels do not overflow; cards and sections are not cut off;
- Chinese and other glyphs are readable, with no replacement characters or tofu;
- no empty or uninitialized charts;
- increases are green and decreases red, with signs and labels;
- every user-requested data item is present with real observation or trading dates, units and sources, or is explicitly marked unavailable;
- split boundaries and uncertainty wording are correct.

If anything fails, fix the content or layout, re-render, and view every resulting PNG again. A clean `errors: []` is not a substitute for visual review. Report only checks you actually performed.
