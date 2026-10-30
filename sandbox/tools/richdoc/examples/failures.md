# FAKE 失败与诊断示例

> **FAKE / 仅用于人工检查错误呈现，不是有效报告。** 本文件故意包含无效组件与禁止输入。不要把它作为成功渲染或安全检查通过的证据。

## 结论先行

无效组件必须产生可见诊断，而不是伪造图表或静默生成可信结论。调用方应检查返回的 `errors`；存在错误时先修复，再发送成品。危险 HTML 和远程图片可能被直接移除，不保证单独产生 `errors` 条目；没有错误并不等于所有原始输入都被保留。

## 缺失 OHLCV 字段

下面没有 high、low、close、volume，不能形成有效 K 线。

<KLine title="FAKE · 不完整行情" data={[{"date":"2026-01-01","open":10}]} />

## 非法 OHLC 关系

下面最高价低于开盘与收盘，最低价高于收盘。

<KLine title="FAKE · 非法价格关系" data={[{"date":"2026-01-01","open":10,"high":9,"low":11,"close":10,"volume":100}]} />

## 未知组件

<UnknownWidget value="FAKE" />

## 非 JSON 表达式

属性只接受数据，不执行 JavaScript。以下表达式不是合法 JSON。

<Stat label="禁止执行表达式" value={process.env.SECRET} />

## 远程资源不是数据来源

正文中的链接可以是引用，但图片、脚本、样式、字体或 iframe 不得从网络加载。下面故意请求远程图片，不能将它作为已加载的证据。

![FAKE：应被拒绝的远程图片](https://example.invalid/remote.png)

## 模型生成脚本被禁止

下面的原始 HTML 不能执行。不要通过 script、事件属性、JavaScript URL 或表达式计算数据。

<script>throw new Error('FAKE script must never run')</script>

<div onclick="throw new Error('FAKE handler must never run')">禁止内联事件</div>

## 命令级失败（不在本报告中执行）

以下命令仅用于明确失败路径，需在拥有 CLI 的环境中手动运行：

```sh
# 输入不存在：应报告文件错误，不应创建成功产物。
richdoc /workspace/does-not-exist.md /workspace/artifacts/missing.png
# 无效格式：应拒绝参数。
richdoc examples/research.md --format jpeg
```

工具调用时 `markup` 与 `file_path` 是二选一输入；同时提供或均未提供应被拒绝。CLI/库允许工作区外的本地路径以支持预览，这不是工作区文件沙箱。通过 `report_progress` 交付时，路径必须位于 `/workspace` 内；桥接层校验附件路径。

## 来源与不确定性

全部恶意或错误片段均为本地合成测试输入，没有真实数据或外部证据。本文件不声明任何检查已执行；以测试套件及真实工具返回的诊断为准。
