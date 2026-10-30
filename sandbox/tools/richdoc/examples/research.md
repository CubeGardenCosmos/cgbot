# FAKE 研究简报：离线产品组合

> **FAKE / 全部数据与事件均为合成示例。** 不是经营披露、调查结果或预测。

## 结论先行

合成样本中，产品甲的收入增速最高，但成本也增加。建议先核验数据口径，再讨论资源配置；当前样本无法支持因果推断。

<Stats>
<Stat label="FAKE 样本收入" value="¥128 万" delta={12.8} />
<Stat label="FAKE 成本变化" value="−3.2%" delta={-3.2} />
<Stat label="FAKE 置信度" value="低" />
</Stats>

## 指标与口径

上涨一律绿色、下跌一律红色；正号绿色、负号红色。颜色不表示业务好坏，成本上升可能是不利信号。保留明确的正负号，不仅靠颜色传达方向。

<Table>
| 产品 | 收入（万元） | 同比 | 成本变化 |
| --- | ---: | ---: | ---: |
| 甲 | 68 | +18.4% | +2.1% |
| 乙 | 37 | +7.2% | -6.5% |
| 丙 | 23 | -2.8% | -3.0% |
</Table>

## 趋势与能力画像

<Chart type="line" title="FAKE · 月度收入（万元）" option={{"xAxis":{"type":"category","data":["一月","二月","三月","四月"]},"yAxis":{"type":"value"},"series":[{"name":"收入","type":"line","data":[25,29,33,41]}]}} />

<Chart type="radar" title="FAKE · 能力评分（0–100）" option={{"radar":{"indicator":[{"name":"覆盖","max":100},{"name":"质量","max":100},{"name":"效率","max":100},{"name":"成本","max":100},{"name":"韧性","max":100}]},"series":[{"type":"radar","data":[{"name":"合成样本","value":[80,72,65,58,76]}]}]}} />

## 方法与计算

合成收入增长率定义为 $g=(R_t-R_{t-1})/R_{t-1}$。若基期为零，不计算增长率，必须解释缺失口径。

$$
\bar{x}=\frac{1}{n}\sum_{i=1}^{n}x_i
$$

<Mermaid>
flowchart TD
  A[收集合成数据] --> B[统一口径]
  B --> C[计算指标]
  C --> D[复核不确定性]
  D --> E[形成结论]
</Mermaid>

## 计划与不确定性

<Timeline items={[{"date":"2026-06-01","title":"FAKE：冻结口径","body":"统一收入与成本单位。"},{"date":"2026-06-08","title":"FAKE：补充样本","body":"记录缺失值和异常值，不直接删除。"},{"date":"2026-06-15","title":"FAKE：复核结论","body":"对照原始记录，说明无法验证的假设。"}]} />

<Columns>
<Callout kind="info" title="可观察事实（FAKE）">
表内合成收入合计为 128 万元；甲的合成同比为 +18.4%。
</Callout>
<Callout kind="warn" title="不能推出">
没有对照组、样本误差或季节调整，不能推出某项策略导致增长。
</Callout>
</Columns>

## 其他紧凑图型（FAKE）

<Chart type="pie" title="FAKE · 收入结构" option={{"series":[{"type":"pie","radius":["25%","60%"],"data":[{"name":"甲","value":68},{"name":"乙","value":37},{"name":"丙","value":23}]}]}} />

<Chart type="scatter" title="FAKE · 投入与产出" option={{"xAxis":{"type":"value","name":"投入"},"yAxis":{"type":"value","name":"产出"},"series":[{"type":"scatter","data":[[2,4],[3,5],[5,8],[7,9]]}]}} />

<Chart type="line" title="FAKE · 累计样本" option={{"xAxis":{"type":"category","data":["一","二","三"]},"yAxis":{"type":"value"},"series":[{"type":"line","areaStyle":{},"data":[10,22,35]}]}} />

<Chart type="heatmap" title="FAKE · 区域评分" option={{"xAxis":{"type":"category","data":["东","西"]},"yAxis":{"type":"category","data":["甲","乙"]},"visualMap":{"min":0,"max":10,"orient":"horizontal","left":"center","bottom":0},"series":[{"type":"heatmap","label":{"show":true},"data":[[0,0,8],[1,0,5],[0,1,3],[1,1,9]]}]}} />

## 来源与限制

<Sources items={[{"title":"FAKE：本文内嵌合成数据；无真实外部来源","url":"https://example.invalid/fake-research"}]} />

上面的保留域名仅用于演示引用格式，不可作为证据；渲染无需访问该链接。数据未经过统计抽样，评分维度与权重任意设定，不确定性未量化。
