# cgbot 部署参考(M5 容器)

上层:[运行手册](runbook.md)、[Roadmap 索引](../ROADMAP.md)。本文是 AI 部署代理的执行手册:代理按 §0 规则工作,按 §1-§3 顺序执行步骤。

**代理规则:**
1. 任何触及宿主机的操作需要用户明确授权。
2. 不得打印密钥值。
3. 向用户询问缺失的输入值(见 §2 输入表)。

## 0. 代理规则

- **授权边界**:仅当用户明确授权后才在宿主机执行任何命令。所有命令默认先 `--check` 只读检查。
- **密钥处理**:永不打印 `secrets.toml` 的值。需要密钥时向用户索取名字,由用户填入文件。
- **输入缺失**:遇到 §2 表中缺少的值,停止并询问用户,不要猜测。

## 1. 前置条件检查清单

| 检查项 | 验证命令 | 预期 |
|--------|----------|------|
| 开发机可 SSH 到宿主机 | `ssh -o BatchMode=yes "$CGBOT_HOST" true` | exit 0(须免密登录) |
| 开发机有 rsync | `command -v rsync` | 打印路径 |
| 宿主机 Docker daemon | `ssh "$CGBOT_HOST" docker info` | 正常输出 |
| 宿主机 Compose 插件 | `ssh "$CGBOT_HOST" docker compose version` | 打印版本 |
| 宿主机 mihomo(出站代理) | `ssh "$CGBOT_HOST" 'systemctl is-active mihomo'` | `active`(服务名视部署而定) |
| NapCat 容器在运行 | `ssh "$CGBOT_HOST" docker ps --format '{{.Names}}'` | 列表中含 NapCat 容器 |

## 2. 输入表(代理须向用户收集,不得猜测)

| 输入 | 含义 | 去向 |
|------|------|------|
| `CGBOT_HOST` | SSH 目标 `user@host` | 开发机环境变量(必需,脚本无默认值) |
| `CGBOT_RUN_DIR` / `CGBOT_BUILD_DIR` / `CGBOT_CAGED_DIR` / `CGBOT_HOST_DATA_DIR` | 远端目录;默认为远端 `$HOME` 下的 `cgbot-run`、`cgbot-build/bot`、`caged`、`cgbot-data` | 开发机环境变量(可选) |
| `HOST_PROXY_USER` / `HOST_PROXY_PASS` / `HOST_PROXY_GROUP` | 宿主机 mihomo 认证监听器的凭据与出站代理组 | 远端 `$CGBOT_CAGED_DIR/.env`(用户自行写入,不入库、不由脚本同步) |
| NapCat 配置文件路径与 bot QQ 号 | 反向 WS 配置位置 | NapCat 配置(§3.6) |
| `[scope] friends` / `groups` / `owner` | 允许服务的好友、群、主人 QQ 号 | 远端 `config.toml` |
| `[group] wake_keywords`、`timezone` | 群唤醒词与 IANA 时区 | 远端 `config.toml` |
| `[coding.docker] host_data_dir` | 与 `CGBOT_HOST_DATA_DIR` 相同的宿主机绝对路径 | 远端 `config.toml` |
| `provider_api_key`、`coding_token`、`gateway_api_key`(可选 `imagegen_key`) | 上游与网关密钥 | 远端 `secrets.toml`(0600);由用户填写,代理不读取、不回显 |

## 3. 部署步骤

下列命令在开发机仓库根执行,先 `export CGBOT_HOST=user@host`。每一步都需用户授权。

### 3.1 创建宿主机目录

`deploy.sh` 不创建目录。路径取 §2 的值(下为默认):

```bash
ssh "$CGBOT_HOST" 'mkdir -p "$HOME/cgbot-run/state" "$HOME/cgbot-data" "$HOME/cgbot-build"'
```

预期:无输出。失败:检查 SSH 账户权限与磁盘空间。

### 3.2 安装宿主机出站代理监听器

caged-egress 经宿主机 mihomo 的认证监听器出站。在宿主机上(需 sudo,由用户确认后执行)运行 `caged/egress/install_host_listener.py`,它向宿主机 mihomo 配置加入专用监听器与来源规则并写带时间戳备份(0600),不重启服务。该脚本须以仓库中的 `caged/` 目录运行(先执行 §3.3 的同步,或手工拷贝),凭据与 `HOST_PROXY_*` 一致。详见 [`caged/README.md`](../../caged/README.md)。

预期:脚本报告监听器已安装。失败:确认宿主机 mihomo 配置路径与 `HOST_PROXY_GROUP` 是已存在的代理组。

### 3.3 启动 caged 栈

1. 在远端 `$CGBOT_CAGED_DIR`(默认 `$HOME/caged`)由用户创建 `.env`(模板 `caged/.env.example`,含 `HOST_PROXY_*`)。`--caged-up` 同步时排除 `.env`,不会覆盖或删除它,且缺少 `.env` 时拒绝继续。
2. 创建 NapCat 与 cgbot 共用的外部网络 `cgbot_net`(`caged/` 不创建它;若 NapCat 已在某网络中,先让该网络以此名存在或把 NapCat 接入):

```bash
ssh "$CGBOT_HOST" 'docker network inspect cgbot_net >/dev/null 2>&1 || docker network create cgbot_net'
```

3. 同步并启动(compose 项目 `caged`,创建 `caged_internal`、`caged_ctl`、`caged_egress`):

```bash
deploy/deploy.sh --caged-up
```

预期输出末行:`deploy: caged stack started`。失败:`caged/.env missing` → 回到步骤 1;`docker compose` 报 `HOST_PROXY_*` 未设置 → 补全 `.env`;端口或子网冲突 → 调整 `.env` 中的 `EGRESS_SUBNET`/`EGRESS_IP`。

### 3.4 构建沙盒镜像

`deploy.sh --build/--up` 只构建 bot 镜像(rsync 排除 `/sandbox`),沙盒镜像须单独构建:

```bash
rsync -a --delete sandbox/ "$CGBOT_HOST:cgbot-build/sandbox/"   # 相对远端 $HOME;或使用绝对路径
ssh "$CGBOT_HOST" 'docker build -t cgbot-sandbox:latest "$HOME/cgbot-build/sandbox"'
```

沙盒镜像首次构建较慢:编译 C++ 合成核心 `cgsynth`(C++ 仅剩此一处,依赖 fluidsynth 与 sqlite3),其余预处理、后处理与编排为 Python 工具(cgscore、cgmidi、cgmaster、cgmusic),以 `pip install --no-deps` 安装,依赖见 `sandbox/requirements.txt`。构建后可做最小自检:`ssh "$CGBOT_HOST" docker run --rm --entrypoint cgsynth cgbot-sandbox:latest soundfonts`(目录未挂载时应输出 JSON 错误或空列表,而不是动态库缺失)。

**音乐合成(`[music] enabled = true`)**:音色目录默认 `$CGBOT_HOST_DATA_DIR/soundfonts`(默认 `$HOME/cgbot-data/soundfonts`),bot 已把 `CGBOT_HOST_DATA_DIR` 以相同路径读写挂入容器,启动时自动创建并后台下载默认音色(约 38 MiB,经 bot 容器出站;失败只记日志,可用 `scripts/fetch_soundfonts.py --dir <目录>` 在宿主机手工补)。`deploy.sh --up` 重建 bot 后,空闲沙盒因挂载进入配置哈希而在下次使用时自动重建为新镜像;若沙盒正在运行,需待其空闲回收或 `docker stop cgbot-sbx-<id>`。详见 [soundfonts.md](soundfonts.md)。

**Muse Sounds(可选)**:`[music] muse_dir` 指定宿主机目录,相对路径按 `config.toml` 所在目录解析;缺省不启用 Muse 挂载,也不要求 `enabled = true`。沙盒镜像不包含专有库或样本;用户自行从官方安装准备以下固定布局(示例值为 192.168.1.19 上的 `/home/klp/muse-sounds`,不得把库、样本或收据放进仓库):

```text
<muse_dir>/
├── Muse Hub Instruments/       # 含 .instruments 的样本根
├── MuseSampler/lib/
│   └── libMuseSamplerCoreLib.so
├── home/                       # 用户官方生成的 HOME 状态
│   └── .local/share/muse-sounds-manager/.receipt
└── machine-id                  # 与该 receipt 匹配的固定标识文件
```

四项路径都存在且类型正确时,bot 才把它们分别只读挂载为沙盒 `/opt/muse-sounds`、`/opt/musesampler/lib`、`/opt/muse-home`、`/etc/machine-id`,并设置 `MUSESAMPLER_LIB`、`MUSE_SAMPLES_DIR`、`CGMUSIC_MUSE_HOME`;任一项缺失则整组不挂载,INFO 日志列出缺失路径。`.receipt` 与 `machine-id` 是用户提供的敏感安装材料,只描述位置,不记录或提交其内容。MuseSampler 所需的 `/var/lib/MuseSampler/.config` 由沙盒内临时可写目录提供,不回写这些宿主机目录。挂载组与环境进入沙盒配置哈希,变更后空闲沙盒下次使用时自动重建。

预期:`docker image inspect cgbot-sandbox:latest` 成功。失败:包镜像源不可达时按 [sandbox-image.md](sandbox-image.md) 调整 `ENV`。

### 3.5 写入 config.toml 与 secrets.toml

将 `deploy/config.toml` 拷到远端 `$CGBOT_RUN_DIR/config.toml`,按 §2 补全并删除所有 `【占位】` 标注;将 `deploy/secrets.toml.example` 拷为 `secrets.toml`,由用户填写真实值后:

```bash
ssh "$CGBOT_HOST" 'chmod 600 "$HOME/cgbot-run/secrets.toml"'
```

预期:`--check` 报 `config.toml has no 【占位】 entries` 与 `secrets.toml mode 600`。失败:按 `--check` 的 `MISSING` 行修正。代理不得读取或回显 `secrets.toml`。

### 3.6 配置 NapCat 反向 WS

在 NapCat 的 OneBot11 配置中,把 `websocketClients` 中 cgbot 的地址设为 `ws://cgbot:6200/ws`,确保 NapCat 容器已接入 `cgbot_net`,修改前先备份,然后重启 NapCat(NapCat 需重启才生效)。

预期:cgbot 启动后日志出现 NapCat 连接。失败:`cgbot` 无法解析 → NapCat 未接入 `cgbot_net`;认证失败 → 对照 NapCat 与 cgbot 的 token 设置。

### 3.7 只读检查

```bash
deploy/deploy.sh --check
```

检查项:docker、compose、`caged_internal`/`caged_ctl`/`cgbot_net` 三个网络、`caged-docker-proxy`/`caged-egress` 容器存在、无冲突的 `cgbot` 容器、数据目录、`state/`、`config.toml` 无占位、`secrets.toml` 为 0600。预期末行 `deploy: check passed`;任一 `MISSING` 即非零退出,按对应步骤修复。

### 3.8 部署

```bash
deploy/deploy.sh --up
```

流程:检查 → rsync 源码并构建 `cgbot:<UTC时间戳>-<sha>` 与 `cgbot:latest` → 写 `.env`(`CGBOT_TAG`、`CGBOT_HOST_DATA_DIR`)→ `compose up -d --no-deps cgbot` → 等待 healthy。预期:`deploy: cgbot healthy`。失败:`unhealthy`/超时 → `ssh "$CGBOT_HOST" docker logs --tail 100 cgbot`;可调大 `CGBOT_HEALTH_TIMEOUT`;必要时按 §5 回滚。


## 4. 部署后验证

```bash
ssh $CGBOT_HOST docker ps | grep cgbot
ssh $CGBOT_HOST docker logs cgbot --tail 50
```

确认:
- cgbot 容器运行中,状态 healthy。
- 日志无 ERROR 级别条目。
- NapCat 已连接(WebSocket 握手成功)。

## 5. 回滚与升级

### 回滚

```bash
deploy/deploy.sh --rollback <previous_tag>
```

查看可用标签:
```bash
ssh $CGBOT_HOST docker images cgbot
```

### 升级

正常 `--up` 流程会构建新镜像并替换容器。

## 6. 已知限制

- **Docker 代理白名单缺口**:caged/docker-proxy 的 `haproxy.cfg` 只允许 containers create/inspect/start/stop/exec/delete、networks inspect、`/version`、`/_ping`、`/containers/json`。不允许 `/images/*`、`/containers/*/logs`、`/containers/*/wait`。若 M5 manager 需要这些调用,须扩展白名单。
- **健康检查依赖 coding 服务器**:在 slice A/D 交付前,容器为 `unhealthy`,这是预期的。
- **NapCat 配置路径**:取决于 NapCat 部署方式,通常在 NapCat 容器的数据卷中。

## 7. 拓扑

单进程 `cgbot` 容器同时承载:NapCat 反向 WS 服务端(容器内 `6200`)、coding 网关 + bridge aiohttp 服务(`8000`)、沙盒 manager。镜像不发布端口。

| 网络 | 类型 | 用途 | 谁在其中 |
|---|---|---|---|
| `cgbot_net` | 外部,有出站 | NapCat 连入 `ws://cgbot:6200/ws`;bot 出站访问上游 LLM | napcat、cgbot |
| `caged_internal` | 外部,`internal` | 沙盒访问 `http://cgbot:8000` 与 `caged-egress`(出站代理) | cgbot、沙盒 |
| `caged_ctl` | 外部,`internal` | cgbot 访问 `caged-docker-proxy:2375` | cgbot、caged-docker-proxy |

`caged_internal`、`caged_ctl`(以及仅供 `caged-egress` 出站的 `caged_egress`)由 `caged/` 栈(compose 项目 `caged`)创建;`cgbot_net` 由部署者单独创建(NapCat 与 cgbot 共用)。cgbot compose 只以 `external: true` 引用这些网络,从不创建或删除。

栈拓扑:`caged/`(`caged-docker-proxy` HAProxy 白名单 :2375、`caged-egress` mihomo 出站代理 :7890)→ `cgbot`(单容器,经 `caged_ctl` 调 Docker API、经 `caged_internal` 与沙盒互通)→ 沙盒(仅在 `caged_internal`,出站只能走 `caged-egress`)。`cgbot` 与 NapCat 经 `cgbot_net` 互通。`cgbot` 启动前须保证 `caged-docker-proxy` 与 `caged-egress` 在运行(`--check` 会校验)。

## 8. 镜像

- 文件:`Dockerfile`、`.dockerignore`(白名单:仅 `pyproject.toml`、`uv.lock`、`src/`)。构建上下文为仓库根。
- 基础镜像 `python:3.12-slim`。builder 阶段装 `build-essential`(因 `pilk==0.2.4` 只有 sdist)与 `uv==0.12.23`;执行 `uv export --frozen --no-dev --no-emit-project --format requirements-txt` 导出带 sha256 的依赖清单,再 `uv pip install --require-hashes --no-deps` 到 `/opt/venv`,最后安装项目本身。runtime 阶段只拷贝 venv 并安装 `ffmpeg`、`ca-certificates`、`tzdata`。
- 镜像源:Debian 与 PyPI 均使用 `mirrors.aliyun.com`(apt 设为 DIRECT,与 `sandbox/Dockerfile` 的策略一致)。事实:构建容器访问 `mirrors.tuna.tsinghua.edu.cn` 的 Debian 包返回 403,改用 aliyun 后成功;构建容器访问 `files.pythonhosted.org` 连接超时(宿主机直连正常),因此不使用 `uv sync`(它会按 lock 中的 pythonhosted URL 下载),改用 hash 校验的 `--require-hashes` 从镜像安装。推论:每个文件仍由 lock 中的 sha256 校验,镜像只替换下载来源。
- 入口:`python -m cgbot --config /app/config/config.toml --secrets /run/secrets/cgbot.toml`。
- 健康检查:容器内请求 `http://127.0.0.1:8000/healthz`,依赖 coding 服务器(slice A/D 交付)。在其存在前,容器为 `unhealthy`,这是预期的。
- 以 root 运行(推论,有取舍):manager 需要把每用户工作区 `chown` 为 `1000:1000`。compose 用 `cap_drop: ALL` 仅加回 `CHOWN`、`DAC_OVERRIDE`、`FOWNER`,并设 `no-new-privileges` 与只读根文件系统(`/tmp` 为 tmpfs)。替代方案(容器以 uid 1000 运行,并预先 chown 宿主机目录)未采用,因为会改变旧 manager 已验证的权限模型。

## 9. 配置与密钥

- `deploy/config.toml` 是模板。真实文件位于宿主机运行目录的 `config.toml`(0644,不含密钥),挂载为只读 `/app/config/config.toml`。补全所有带 `【占位】` 的项后,`deploy.sh` 才会放行。
- `deploy/secrets.toml.example` 只列名字。真实运行目录的 `secrets.toml` 必须 `chmod 600`,挂载为只读 `/run/secrets/cgbot.toml`。需要的名字:`provider_api_key`(Bot 对话)、`coding_token`、`gateway_api_key`(网关上游),可选 `imagegen_key`。
- 容器内路径约定:数据目录 `[storage] data_dir = "/app/data"`(绝对路径);`[coding.bridge] temp_dir = "/app/data/bridge"`(绝对路径)。宿主机运行目录的 `state/` 挂载为 `/app/data`。
- 占位值:`[provider] context_window = 128000`(标注待查证);`[scope]` 的 `friends`/`groups` 为空列表(全部拒绝,安全默认);`[coding.imagegen]` 省略(图像生成不可用)。
- 模型降级(2026-11-07):`[[provider.fallbacks]]` 有序数组。模板中为 `deepseek-v4.1-flash`(主)→ `gemini-3.8-flash`(`wire_api = "gemini"`,`base_url = "https://api.cicori.com/v1beta"`)。省略项继承 `[provider]`(`wire_api` 例外,默认 responses),共用 `provider_api_key`;主模型在适配器重试用尽后失败才降级,每轮从主模型重新开始;流式已产生输出后不降级;降级成功记 INFO、每次失败记 WARNING 日志。

## 10. 部署命令

在仓库根执行:

```bash
deploy/deploy.sh --check            # 只读:docker/网络/目录/配置占位/secrets 权限
deploy/deploy.sh --build            # rsync 源码 + docker build(只构建,不启动)
deploy/deploy.sh --caged-up         # 同步 caged/ 并启动 caged compose stack
deploy/deploy.sh --up               # check + build + 写 .env + compose up -d --no-deps cgbot + 等待 healthy
deploy/deploy.sh --rollback <tag>   # 切换到已存在的 cgbot:<tag> 并重建 cgbot
```

- 镜像标签形如 `cgbot:<UTC时间戳>-<git短SHA>`,同时打 `cgbot:latest`。回滚时使用 `docker images cgbot` 查得的旧 tag。
- `--up` 不会自动回滚。`unhealthy` 或超时即退出并给出排障与回滚提示。
- 脚本只操作 compose 项目 `cgbot` 的 `cgbot` 服务。若已存在名为 `cgbot` 且不属于该项目的容器,脚本拒绝继续。
- 脚本不执行 `docker system prune`、`builder prune` 或 `image prune`。清理需人工判断,避免影响其他项目镜像。

## 11. 资源与风险

- bot 容器:`mem_limit 1g`、`cpus 2`、`pids_limit 512`(推论,待实测)。
- 沙盒容器按旧 manager 的 2 GB 上限估算(推论)。`per_user` 并发与沙盒数量叠加后,可能超过宿主机剩余内存。上线前须实测。
- 本部署不清理 Docker 镜像与构建缓存(脚本不执行任何 prune),清理由用户判断。

## 12. Gemini 搜索

`gemini_search` 工具直接调用上游中转(`[search.gemini] base_url`,如 `https://api.cicori.com/v1beta`)的 Gemini `generateContent`,启用 `google_search` grounding;不再需要宿主机 relay(`omp-search-relay` 已移除)。
bot 工具使用 `[provider] api_key`;沙盒桥(`/bridge/gemini-search`)使用 `[coding] upstream_api_key`,缺省回退 `[provider] api_key`。沙盒本身不持有任何密钥。

`x_search` 工具(仅 bot,不进沙盒搜索桥)优先调用 `[search.x] base_url`(如 `https://api.cicori.com/v1`)的 Responses `POST /responses`,使用 grok 原生 `x_search`(`recency` 换算为 `from_date`),密钥为 `[provider] api_key`。Grok 报错(除参数无效/安全拒绝外)或上游未实际执行搜索(`usage.num_server_side_tools_used` 为 0 且无 `url_citation`,错误码 `E_NOT_SEARCHED`)时,若 `[search.gemini]` 已启用则回退到限定 X 的 Gemini 搜索(结果头注明非实时、链接为跳转链接);Gemini 未启用则不回退,直接报错。2026-11-07 起 CLIProxyAPI 会剥离 `x_search` 工具,实际走 Gemini 回退。
