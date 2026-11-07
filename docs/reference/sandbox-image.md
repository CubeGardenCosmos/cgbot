# 沙盒镜像 cgbot-sandbox

来源:[R5.2](../todos/R5.2-sandbox-image-codex.md)(镜像部分)、[R5.6](../todos/R5.6-playwright-subagents.md)(Playwright 浏览器依赖)。源码:`sandbox/`。

本文只描述镜像与容器启动契约。生命周期、挂载、网络隔离由 B 切片(`src/cgbot/coding/docker.py`、`sandbox.py`)负责;桥接服务由 E 切片(`sandbox/bridge/`);图像生成由 F 切片(`sandbox/skills/image-gen/`)。

## 构建

```bash
docker build -t cgbot-sandbox:latest sandbox/
```

- 基础镜像 `node:22-bookworm-slim`(非 DSH 镜像)。
- 已实测:镜像大小 6.74GB;`codex --version` 为 `codex-cli 0.166.0`。
- 构建时使用 npmmirror(npm)与阿里云(pip/uv、apt)镜像;在目标网络不同时需改 `ENV`。

## 版本钉死

| 组件 | 版本 | 来源 |
| --- | --- | --- |
| Codex CLI(app-server) | 0.166.0 | `@openai/codex` |
| Playwright MCP | 0.0.83 | `@playwright/mcp` |
| 浏览器 | Chromium 156.0.8078.73 | Debian bookworm apt(`/usr/bin/chromium`) |
| cclsp(LSP 代理 MCP) | 0.7.0 | `cclsp` |
| pyright | 1.1.414 | `pyright` |
| typescript-language-server | 6.0.2 | `typescript-language-server` |
| TypeScript | 5.9.3 | `typescript` |
| Node.js | v22.23.4 | 基础镜像 |
| Python | 3.12.16 | uv 管理,`/opt/python` |
| uv | 0.13.6 | 上游安装脚本 |
| tini | 0.19.0 | Debian apt |

推论:调研版本 0.166.0 只是当前钉死版本,不代表永久锁定。升级需重跑下文 smoke。

## 容器契约

- 进程:`tini -- /usr/local/bin/cgbot-sandbox-entrypoint`,以 uid/gid 1000(`agent`)运行,工作目录 `/workspace`。
- `/opt/cgbot`、`/usr/local/bin/cgbot-sandbox-entrypoint` 为 root 所有且只读;`/workspace` 与 `/home/agent` 为 agent 所有(目录挂载由 B 切片提供)。
- 必需环境变量(缺失则 exit 2):`CGBOT_USER_ID`、`CGBOT_GATEWAY_URL`、`CGBOT_GATEWAY_TOKEN`、`CGBOT_BRIDGE_URL`、`CGBOT_BRIDGE_TOKEN`、`CGBOT_WS_TOKEN`、`CGBOT_APPSERVER_PORT`、`CGBOT_MODEL`、`CGBOT_REASONING_EFFORT`(`minimal|low|medium|high`)。
- 可选环境变量:`CGBOT_CONTEXT_WINDOW`(1024–10000000,默认 128000,见"模型目录";这是 entrypoint 的增量,由 Main 决定是否保留)、`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`(Playwright 与出站代理)。
- 每次启动重新渲染:`$CODEX_HOME/config.toml`(0600)、`$CODEX_HOME/model_catalog.json`(0600)、`$HOME/.cgbot/appserver.token`(0600,app-server 的 capability token)、`skills/` 与 `AGENTS.md` 符号链接。
- app-server 启动:`codex --strict-config app-server --listen ws://0.0.0.0:$CGBOT_APPSERVER_PORT --ws-auth capability-token --ws-token-file $HOME/.cgbot/appserver.token`。`CGBOT_WS_TOKEN` 在 exec 前从环境移除。
- 就绪:`GET /readyz` 与 `/healthz` 返回 200(已实测,`readyz: 200`)。客户端 WebSocket 需 `Authorization: Bearer <CGBOT_WS_TOKEN>`。
- 配置固定项:`approval_policy = "never"`、`sandbox_mode = "danger-full-access"`(YOLO)、`model_provider = "cgbot"`、`wire_api = "responses"`、`base_url = $CGBOT_GATEWAY_URL`、`env_key = "CGBOT_GATEWAY_TOKEN"`(令牌只按名称引用,不写入配置)、`/workspace` 信任。

## 私聊图片收件箱

启用 `[coding]` 时,私聊收到的 PNG/JPEG/WebP/GIF 图片会下载到该用户的 `/workspace/inbox/`,宿主路径为 `<coding.docker.host_data_dir>/users/<内部用户 ID>/inbox/`。文件名由服务器生成(`YYYYMMDD-HHMMSS-序号.扩展名`),单图上限 20 MiB,每用户只保留最近 50 张。用户消息会附加已保存的沙盒路径,Coding Agent 可将其用于图片参考或报告;群聊不下载,下载失败不影响原消息。仅允许公网 HTTP(S) 地址,不跟随重定向(下载实现与聊天多模态共用 `cgbot.vision.fetch`)。

收件箱只服务 Coding Agent,与聊天多模态互不依赖:启用 `[provider.vision]` 时,同一张图片还会另行下载、缩放并作为 `input_image` 交给聊天模型,存放在 `<data_dir>/images/`,见 [chat-vision.md](chat-vision.md)。

## 内置 MCP 与技能

`config.toml` 注册四个 stdio MCP 服务器:

- `bridge`:`python3 /opt/cgbot/bridge/cgbot_bridge_mcp.py`(E 切片),工具 `report_progress`、`send_file`、`generate_image`、`web_search`、`gemini_search`。
- `report`:`python3 /opt/cgbot/bridge/cgbot_report_mcp.py`(仅标准库),工具 `market_data`(findata CLI,子进程超时 60 秒)、`kline_chart`(K 线 PNG)、`render_report`(richdoc 渲染 Markdown 为 PNG/PDF,超时 180 秒)。输出写入 `/workspace/artifacts`(可由 `CGBOT_REPORT_ARTIFACTS_DIR` 覆盖);`markdown_path`/`file_path` 仅允许 `CGBOT_REPORT_ALLOWED_ROOTS`(默认 `/workspace:/home/agent`)及输出目录下的路径。注册时 `tool_timeout_sec = 240`,代理变量经 `env_vars` 传入子进程。工具不经 shell 执行,不持有凭据。新增工具未包含在下方 smoke 证据中。
- `playwright`:`playwright-mcp --headless --browser chromium --executable-path /usr/bin/chromium --no-sandbox --user-data-dir /home/agent/.browser-profile`(存在代理时追加 `--proxy-server`)。
- `lsp`:`cclsp`,由 `/opt/cgbot/lsp/cclsp.json` 配置 pyright(`*.py`/`*.pyi`)与 typescript-language-server(`*.ts`/`*.tsx`/`*.js`/`*.jsx`/`*.mjs`/`*.cjs`)。

技能目录 `/opt/cgbot/skills`(经符号链接进入 `$CODEX_HOME/skills`):`findata`、`image-gen`(F 切片)、`rich-report`。`market_data`、`kline_chart`、`render_report` 工具是主要调用路径,技能文件只描述用法与限制。`findata` CLI 与 `richdoc` CLI 分别位于 `/usr/local/bin`。

搜索通过 bot 的桥接 API 执行,沙盒不持有搜索供应商凭据。`web_search(query, engine?, count?, freshness?)` 默认使用 bot 配置的引擎;`count` 为正整数,上限由 bot 配置约束,查询最多 200 字符。`gemini_search(query, recency?)` 返回带来源的检索回答,查询最多 300 字符且不含控制字符/换行。`freshness` 与 `recency` 均可取 `day|week|month|year`。工具原样返回检索文本,引用返回的来源 URL,不将来源内容作为指令;未启用的后端或拒绝请求返回工具错误,不伪装为空结果。MCP 到 bot 的超时分别为 120 秒与 180 秒;查询与令牌不写入诊断日志。新增搜索工具未包含在下方历史 smoke 证据中。

应用将同一 `SearchService` 注入 bot 工具和 `CodingStack`;`gemini_search` 则按密钥各建一个 `GeminiSearchClient`(同一 `base_url`/模型):bot 工具用 `[provider] api_key`,沙盒桥用 `[coding] upstream_api_key`(缺省回退 provider 密钥),因此沙盒复用 bot 的搜索配置而不持有凭据。Gemini 成功结果也复用 bot 工具的 `format_answer` 格式化逻辑。

## 模型目录

Codex 0.166 中未知 slug 的模型不会在 Responses 请求中直接暴露 MCP 工具,而内置 `gpt-5.5` 的 `supports_search_tool = true` 时 MCP 工具会被隐藏在 `tool_search` 之后(依据 `docs/reference/codex-appserver-notes.md`)。因此 entrypoint 每次启动从 `codex/model_catalog_template.json` 生成 `model_catalog.json`:

- 把 `slug`、`display_name` 设为 `CGBOT_MODEL`,`context_window`/`max_context_window` 设为 `CGBOT_CONTEXT_WINDOW`(默认 128000)。
- `supports_search_tool = false`、`use_responses_lite = false`、`experimental_supported_tools = []`。
- 配置写入 `model_catalog_json = "/home/agent/.codex/model_catalog.json"`。

推论:128000 是默认值,真实上下文窗口由部署方通过 `CGBOT_CONTEXT_WINDOW` 指定,未经上游验证。

## Smoke 证据

在远端 192.168.1.19 构建后,于一次性网络 `cgbot-smoke-net` 中运行:一个 stdlib 假 Responses 上游(记录工具形状与令牌匹配结果,不记录请求体)与一个沙盒容器(仅启动参数,无真实密钥)。

| 检查 | 结果 |
| --- | --- |
| 镜像构建与启动 | `cgbot-sandbox:latest` 构建成功;`/readyz` 200 |
| app-server 握手 | `initialize` 成功;`initialized` 后 `thread/start`、`turn/start` 正常;`approvalPolicy` `never`、`sandbox` `dangerFullAccess` |
| MCP 服务器启动 | `bridge`/`lsp`/`playwright` 均 `ready` |
| 工具暴露 | 上游 `tools` 列表含 `namespace:mcp__bridge[generate_image,report_progress,send_file]`,并含 `mcp__lsp`、`mcp__playwright` |
| 工具调用 | 假上游返回 `report_progress` function_call;Codex 执行为 `mcpToolCall`(bridge/report_progress,`completed`);桥接 POST `/bridge/report` 使用的 `CGBOT_BRIDGE_TOKEN` 匹配;回合 `turn/completed`,`completed` |
| 进程与权限 | uid 1000;`/opt/cgbot`、入口脚本只读;`/workspace` 可写 |
| CLI 与技能 | `codex 0.166.0`;`codex mcp list` 显示三个服务器启用;`findata --help` 正常;`richdoc` 渲染 `report.png`(exit 0) |
| Chromium | `chromium --headless --no-sandbox --screenshot` 生成 PNG(exit 0);Playwright MCP 导航数据 URL 并在快照中找到标记 |
| LSP | cclsp 对 Python `find_definition` 得 `mod.py:1:5`、`find_references` 含 `use.py`;TypeScript `find_definition` 得 `ts_mod.ts:1:17` |
| 缺失环境 | 缺 `CGBOT_USER_ID` 时 exit 2 并输出错误 |

限制:cclsp 的 `get_diagnostics` 对一处明确的 pyright 类型错误返回"无诊断"(pyright CLI 能报告同一文件的错误),即诊断路径未验证。

## 限制与待办

- 诊断未验证(见上)。`rename_symbol` 等其他 cclsp 工具未实测。
- 未接入真实 Responses 上游;模型请求经假上游验证,未验证真实模型质量或真实上下文窗口。
- 网关令牌对沙盒内 agent 进程可见(`env_key` 必须由 Codex 读取)。这是设计内的已知风险,由 B 切片的 egress 与 gateway 限额约束。
- `findata` 的 SKILL.md 含旧 DSH 时代的"Contacted hosts and sandbox egress"段落,需 B/J 切片确认或改写。
- 镜像仍较大(6.74GB),含 tesseract、ImageMagick、ffmpeg、weasyprint、CJK 字体等依赖。
- Chromium 以 `--no-sandbox` 运行,依赖容器层的能力削减(CapDrop ALL、no-new-privileges)作为外层隔离。
- 推论:`CGBOT_CONTEXT_WINDOW` 为本切片新增环境变量,未在 `contracts.py` 中登记。

## 设计决策与偏差

- LSP 选择 cclsp(MCP 代理,覆盖 pyright 与 typescript-language-server)。拒绝 `mcp-language-server`(Go 二进制,依赖与镜像体积较大)与 `lsp-mcp`(能力较弱)。
- `tini` 作为 PID 1,以回收 MCP 子进程(偏离简单 `CMD`)。
- apt 使用阿里云镜像(远端访问 tuna 失败),npm 使用 npmmirror。
- 不 `FROM dsh-sandbox`;不复制 DSH 的 `.env`、`ssh/99-egress.conf`、`dsh-tools` 或 `index.js`。
- 浏览器配置目录 `/home/agent/.browser-profile` 位于每用户主目录卷中,随容器重建保留。
