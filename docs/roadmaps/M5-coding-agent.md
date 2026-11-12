# M5 Coding Agent Roadmap

目标:以常驻 Codex app-server 替代 legacy,提供每用户默认 Coding Agent、线程管理、排队、进度与附件回报,并保留单独的 omp_* 关联入口。默认 Agent 不限用途。依据:DESIGN.md §13.1、§13.3–§13.7;后续决议覆盖旧调研与推论。

所属:[M5](../MILESTONE.md)。状态:待决策。能力归属:R5.1 为 gateway,R5.2/R5.6 为沙盒,R5.3 为 manager,R5.4/R5.7 为框架工具,R5.5 为回报桥接;实施负责人待分配。

| 编号 | 能力 | 依赖 | 验收条件 | 状态 | TODO 链接 |
| --- | --- | --- | --- | --- | --- |
| R5.1 | gateway Responses 薄透传 | R0.1 上游验证、R0.2、R1.5 供应商约定 | Codex 经 gateway 使用原生 Responses;校验内部令牌、注入真实密钥,不翻译协议 | 待决策 | [TODO](../todos/R5.1-gateway-responses.md) |
| R5.2 | 沙盒镜像与 Codex app-server | R5.1、R1.3、R0.2 | 默认 YOLO 常驻服务,插件/SKILLS/软件包预装;legacy 与旧桥接/NDJSON 依赖移除 | 待决策 | [TODO](../todos/R5.2-sandbox-image-codex.md) |
| R5.3 | manager 线程、队列与并发 | R5.2、R1.3、R1.7 | 每 thread 忙时 FIFO 排队,stop 可清队列;每用户并发 4,全局降级为 1 与恢复符合已定阈值 | 待决策 | [TODO](../todos/R5.3-manager-threads-queue-concurrency.md) |
| R5.4 | codex_* 工具 | R1.6、R5.3、R5.5 回报接口 | 独立工具族替代 legacy task 工具族,用户能管理线程;不保留 legacy 兼容层或统一后端工具层 | 待决策 | [TODO](../todos/R5.4-codex-tools.md) |
| R5.5 | 回报桥接与大文件 | R5.2、R1.2、R1.6、R1.7;与 R5.3 联调 | report_progress 风格;单文件 512MB、2MB 切块、全链路流式,超限拒绝,临时文件可清理 | 待决策 | [TODO](../todos/R5.5-bridge-large-binary.md) |
| R5.6 | Playwright 登录态与 Codex 子代理 | R5.2、R5.3 | 登录态每用户独立、主目录持久化、重建保留;框架不限制 Codex 子代理深度/并发 | 待决策 | [TODO](../todos/R5.6-playwright-subagents.md) |
| R5.7 | omp_* 工具 | R1.3、R1.6、R1.7 | omp_add 式私聊录入与独立工具族;保留自身队列、账户额度、模糊目录解析 | 待决策 | [TODO](../todos/R5.7-omp-tools.md) |
| R5.8 | 音乐合成(沙盒 cgmusic + bot 接线) | R5.2、R5.3、R5.4、R5.5、R1.8 | 私聊乐谱文件或关键词请求经沙盒 `cgmusic` 合成并回报音频;群聊不注入、不处理;音色目录只读挂载 | 进行中 | [TODO](../todos/R5.8-music-synth.md) |

## 待决策

- §13.6c:现有 :8788 上游是否原生支持 Responses 未验证,归 R0.1;不支持时不得用 gateway 翻译补齐。路径、SSE、zstd 头部/请求体透传细节、模型目录与远程压缩能力设置为推论,归 R5.1。
- §13.3、§13.6:外层容器隔离边界、app-server 传输/鉴权、版本、镜像软件包/插件/SKILLS 清单、初始化与存活管理待确定。
- §13.7:codex_* 精确工具清单与参数、用量视图取舍待定;不照搬调研表全部方法。任务规划重做且另行讨论,本阶段不制定规划流程。
- §13.6b、§13.7:故障分类/计数来源、降级时已运行任务处理、队列存储与重启恢复、事件重复/断连处理待定。已定恢复条件是“1 小时没有 429”,不得擅自改为“没有任何故障”。
- §13.3、§13.6b:桥接形态(MCP 或其他)、二进制分块协议、文件类型识别与临时清理策略待定;附件不进入 LLM 工具返回值仍为推论。
- §13.7:身份取自事件、不接受用户 ID、监视任务隔离/超时、回报进入系统事件队列是设计倾向,需确认并与 M1 对齐。
- §13.4–§13.5:omp 凭据持久化方式待定;录入已定沿用私聊主机/端口/密码/别名,不再以“密码不可进上下文”推论阻塞。

## 与其他阶段的接口

- M0 收束上游与工程参数;M1 提供供应商、身份、Agent 工具执行及系统事件队列;M2 决定被后台事件唤醒后的私聊处理。M5 不自行定义通知到最常用入口的统计口径,该能力后置。
- §13.7 已定覆盖 §8 的旧“manager/沙盒本体不动”表述:可复用 legacy 无关的容器生命周期、计划任务、gateway、egress、用户卷布局;必须替换 legacy、legacy-plugin-bot-bridge、legacy NDJSON 解析。复用是候选,不代表已检查可直接运行或无需改造。
- codex_* 与 omp_* 分开;不把 Codex 默认 Agent 限定为新闻采集,不恢复 legacy task 别名。计划任务仅沿用 manager 方向,任务规划尚未设计。
- 测试只用已定服务范围;不拉起旧 AstraBot,不自行改变既有远端部署。所有真实测试待授权,本轮不执行。
