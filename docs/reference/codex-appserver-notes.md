# Codex app-server 参考笔记（M5 · slice C）

基于 codex-cli **0.166.0** 本机实测。标注 **[观测]** 的内容来自真实帧或命令输出；**[推论]** 为未直接验证；**[未验证]** 为未测试，不得当作事实使用。

## (a) 启动参数、鉴权与健康检查

```
CODEX_HOME=<含 config.toml 的目录> codex app-server \
  --listen ws://127.0.0.1:<port> \
  --ws-auth capability-token \
  --ws-token-file <token 文件>
```

- token 文件：一行明文 token，权限 0600。客户端握手头：`Authorization: Bearer <token>`。
- 错误 token 返回 **401**，响应不回显 token。**[观测]**
- 启动输出提示 `binds localhost only`。**[观测]** 容器内对外监听地址（`0.0.0.0`）**[未验证]**。
- 健康检查：`GET /readyz`、`GET /healthz` 在 `127.0.0.1` 上返回 **200**，且无需 Authorization 头。**[观测]**
- YOLO 配置生效：`approval_policy="never"` + `sandbox_mode="danger-full-access"`，`thread/start` 返回 `approvalPolicy:"never"`、`sandbox:{"type":"dangerFullAccess"}`。**[观测]**

## (b) config.toml 键

已在真实运行中生效的最小配置：

```toml
model = "<model-slug>"
model_provider = "fake"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.fake]
name = "fake"
base_url = "http://127.0.0.1:18901/v1"
wire_api = "responses"

[mcp_servers.probe]
command = "/abs/path/mcp_probe.sh"
```

- `model_providers.<name>` + `wire_api="responses"`：对本地 Responses 桩生效。**[观测]**。`env_key` / 鉴权头方式**[未验证]**。
- `approval_policy` / `sandbox_mode`：生效，见 (a)。**[观测]**
- `mcp_servers.<name>.command`（stdio）：启动后发出 `mcpServer/startupStatus/updated`（`starting` → `ready`）并完成 `initialize` / `tools/list`。**[观测]**
- **重要**：默认模型 `gpt-5.5` 下，上游请求的工具列表**不包含** MCP 工具，只有 `tool_search`。**[观测]**
  - 切换为 slug `gpt-5` / `gpt-4.1` / 未知 slug `probe-model` 时，工具列表包含命名空间 `mcp__probe`，MCP 工具可被调用。**[观测]**
  - 原因为 [推论]：`gpt-5.5` 的模型元数据让 MCP 工具走 tool_search 延迟加载路径。
  - `[features] tool_search = false` 与 `tool_search_always_defer_mcp_tools = false` 两个开关**均未**改变 `gpt-5.5` 下的结果。**[观测]**
  - 含义：沙箱镜像的模型 slug 必须选择能直接暴露 MCP 工具的模型，或者改用 tool_search 路径。**[推论，待 slice G/E 选定模型后复测]**
- `model_catalog_json`、`remote_compaction`、skills 查找路径：**[未验证]**，不得依赖。

## (c) RPC 与事件形状

握手：`initialize` 参数 `{clientInfo:{name,version}, capabilities:{experimentalApi:false}}`，结果含 `userAgent`、`codexHome`、`platformFamily`、`platformOs`。之后发送 `initialized` 通知。**[观测]**

- **`thread/start`** 参数 `{}`。结果：`thread`（含 `id`、`sessionId`、`forkedFromId`、`parentThreadId`、`preview`、`status`、`turns`、`name`、`path`、`createdAt`、`updatedAt`、`historyMode:"paginated"`、`modelProvider`、`model`、`cwd`、`environments`）；顶层另含 `model`、`modelProvider`、`cwd`、`runtimeWorkspaceRoots`、`approvalPolicy`、`approvalsReviewer`、`sandbox`、`reasoningEffort`、`serviceTier`、`multiAgentMode`、`instructionSources`、`disabledPluginIds`、`activePermissionProfile`。**[观测]**
- **`thread/resume`** 参数 `{threadId}`。结果与 `thread/start` 同形，另含：
  - `collaborationMode`
  - `initialTurnsPage`（未请求时为 `null`）
  - `turnsBackwardsCursor`、`itemsBackwardsCursor`（不透明字符串）
  - `thread.preview` 为第一条用户消息文本。
  - 收到 `deprecationNotice`：完整历史水合已弃用，建议 `excludeTurns:true`，再用 `thread/turns/list` / `thread/items/list` 分页。**[观测]**
  - 未测试 `excludeTurns` 参数本身。**[未验证]**
- **`thread/fork`** 参数 `{threadId}`。结果 `thread` 的 `id` 为新 id，`forkedFromId` 为源 id；顶层形状同 `thread/start`。伴随 `thread/started`、`thread/goal/cleared`、`mcpServer/startupStatus/updated`。**[观测]**
- **`thread/turns/list`** 参数 `{threadId, limit}`。结果 `{data:[turn,…]}`；turn 含 `id`、`rootTurnId`、`items`、`status`、`startedAt`、`completedAt`、`durationMs`、`error`；item 类型如 `userMessage`（`content:[{type:"text",text}]`）、`agentMessage`（`text`）。**[观测]**
- **`thread/list`** 结果 `{data:[thread,…]}`。**[观测]**
- **`thread/status/changed`**（通知，不是请求）：
  - 参数 `{threadId, status}`，`status` 为 `{type:"active", activeFlags:[]}` 或 `{type:"idle"}`。**[观测]**
  - 随 turn 开始（active）与结束（idle）发出。
  - **未观测**名为 `thread/status` 的请求方法。**[未验证]**，CodManager 应以通知为准。
- **`turn/start`** 结果 `{turn:{id,status:"inProgress",items:[],…}}`。随后通知：`thread/status/changed`、`turn/started`、`item/started`、`item/completed`、`thread/tokenUsage/updated`（`tokenUsage.total/last/modelContextWindow`）、`account/rateLimits/updated`、`turn/completed`（`turn.status:"completed"`，`items` 为摘要）。**[观测]**
- `turn/interrupt` 之后 turn 状态为 `interrupted`。**[观测]**
- `thread/name/set` 结果 `{}`。**[观测]**
- 其他通知（如 `remoteControl/status/changed`）客户端忽略，不影响流程。**[观测]**

## (d) MCP `_meta`：能否获取 threadId / turnId

**能。** **[观测]**，stdio MCP 服务器上 `tools/call` 的 `params._meta` 包含：

- 顶层：`threadId`、`sessionId`、`windowId`（形如 `<threadId>:0`）、`callId`、`itemId`、`progressToken`。
- `x-codex-turn-metadata`（对象）：`thread_id`、`session_id`、`turn_id`、`model`、`sandbox_mode`、`codex_version`、`turn_started_at_unix_ms` 等。

因此 MCP 服务器可以从 `_meta` 读取 threadId 与 turnId，无需环境变量。

- 工具暴露形状 **[观测]**：上游看到的是命名空间工具 `{type:"namespace", name:"mcp__probe", tools:[{type:"function", name:"echo", …}]}`；函数调用项带 `namespace:"mcp__probe"`、`name:"echo"`。`tools/call` 的 `name` 为 `"echo"`。
- 子进程环境：probe 记录的 env 列表为空，未观测到 CODEX_* 变量注入给 MCP 子进程。**[观测]**
- HTTP 类型 MCP 服务器上 `_meta` 的行为**[未验证]**，只在 stdio 上验证过。
- 结论：沙箱内 bridge 若需要绑定线程，应读取 `_meta["threadId"]`（或 `x-codex-turn-metadata.thread_id`）。**[推论]**：`_meta` 由 codex 在每次调用时生成，不由模型控制；但仍需将其视为"可信度中等"的标识，身份仍以 bridge token 为准。

## (e) WebSocket 心跳与 Authorization 日志风险

- 客户端 ping 关闭时，服务器对 ping 应答正常。**[观测]**
- 空闲 180 秒后连接仍存活，之后 `thread/list` 正常。**[观测]**
- **风险**：`websockets` 库在 DEBUG 级别会记录握手请求头，其中包括 `Authorization`。**[观测]**
  - 生产环境必须确保 `websockets` logger 不低于 INFO。
  - 测试中错误 token 的日志断言仅限于 `cgbot` logger。
- token 不得出现在日志、错误消息或异常 repr 中。

## 未验证清单（不得当作事实）

- `model_catalog_json`、`remote_compaction`、skills 查找路径的实际效果。
- 容器内对外监听地址（非 127.0.0.1）。
- `thread/resume` 的 `excludeTurns` 参数。
- 是否存在 `thread/status` 请求方法。
- HTTP 类型 MCP 服务器的 `_meta` 行为。
- `gpt-5.5` 之外、生产所用模型上 MCP 工具的暴露情况。
