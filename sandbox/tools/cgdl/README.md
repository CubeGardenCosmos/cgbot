# cgdl

沙箱内的 MuseScore 搜索与乐谱下载 CLI。所有输出为 JSON(成功写 stdout,失败写 stderr 并以非零码退出)。

## 版权说明

本工具仅供个人学习与研究使用。请遵守乐谱的版权与授权条款,不要用于商业分发或再发布。下载限制只是请求频率与体积的工程约束,不是版权或访问控制的替代品。

## 命令

```sh
cgdl musescore-search <关键词> [--limit N]        # 搜索,默认 --limit 10,上限 50
cgdl musescore-info <乐谱 URL>                    # 元数据
cgdl musescore-get <乐谱 URL> --kind mid|mxl|mp3|pdf|png [--page N] [-o 目录]
```

- 乐谱必须使用完整 URL `https://musescore.com/user/<uid>/scores/<id>`。纯数字 id 无法解析:`musescore.com/scores/<id>` 返回 404,因此以 `bad_url` 拒绝。
- 默认输出目录 `/workspace/artifacts/music/`,必须是绝对路径。
- 文件名由标题与乐谱 id 生成(`Title_Words-<id>.<ext>`),写入采用临时文件 + fsync + 原子替换。

成功示例(stdout 单行 JSON):

```json
{"kind": "mid", "path": "/tmp/cgdl-live/Gymnopedie_No._1-6969530.mid", "bytes": 3146, "score_id": 6969530, "title": "Gymnopedie No. 1", "url": "https://musescore.com/user/39593079/scores/6969530"}
```

失败示例(stderr):

```json
{"error": "not_found", "message": "MuseScore offers no mxl file for this score"}
```

### 错误码

| 码 | 含义 |
| --- | --- |
| `blocked` | 搜索页挑战 15 秒、乐谱与下载页挑战 60 秒内未通过 |
| `not_found` | 页面 404,或该类型文件不存在(例如无 MusicXML、无页图) |
| `parse_failed` | 页面或接口结构与预期不符 |
| `download_failed` | 下载失败、接口拒绝、超过 40 MiB 上限或文件魔数不符 |
| `unsupported_kind` | `--kind` 不在 mid/mxl/mp3/pdf/png 中 |
| `bad_url` | URL 不在允许的主机内,或格式不对(含纯数字 id) |
| `bad_input` | 参数为空、过长或输出目录非法(补充码) |
| `browser_unavailable` | 无法启动 Chromium 或 Playwright(补充码) |

## 工作方式

- **先普通 HTTP,被拦再用浏览器。** 搜索页与乐谱页先用 httpx 请求;遇到 403/429/503 或挑战页时,改用 Playwright 驱动的 Chromium 通过挑战并取得渲染后的 HTML。
- **文件地址走页面内接口。** 浏览器上下文内调用 `/api/jmuse?id=&index=&type=`,请求头 `authorization` 为 `md5(id + type + index + secret)` 的前 4 位十六进制。`secret` 在运行时从页面构建脚本中读取(匹配 `+"<secret>").substr(0,4)`),代码中不写死。
- **mid / mp3** 为单次接口调用后的文件下载;**mxl** 接口返回空 url 时报 `not_found`;**pdf** 逐页请求 `img` 类型(index 从 0 起),遇到空 url 或 404 视为页数结束,最多 200 页,随后把 SVG 页嵌入 A4 HTML 并由 Chromium 打印为 PDF。
- **节流:** 所有请求(HTTP 与浏览器)共享一个限速器,两次请求间隔至少 1 秒。
- **体积上限:** 单次下载与 PDF 合计均不超过 40 MiB。
- **主机白名单:** `musescore.com`、`www.musescore.com`、`static.musescore.com`、`s3w.musescore.com`、`cdn.ustatik.com`。非白名单 URL 在 HTTP 层拒绝,浏览器层中止请求。

## 令牌与 blocked 规则

- **secret 来源:** 先读本地缓存(`CGDL_CACHE_DIR`,默认 `~/.cache/cgdl/token_secret.json`,只保存构建脚本 URL 与 secret);缓存缺失时从页面构建脚本发现并写回缓存。令牌按请求计算,不缓存。
- **422 拒绝:** jmuse 返回 HTTP 422 时清除缓存,改用字面量回退 `9654,4e`(仅在本进程内使用,从不写入缓存),等待 2 秒后重试;每个 jmuse 请求最多发起 3 次,仍被拒绝则报 `download_failed`。
- **blocked:** 搜索页等待 Cloudflare 挑战最多 15 秒,乐谱与下载页最多 60 秒;挑战未通过即报 `blocked`,不换用镜像或其他通道重试。

## 实测矩阵

验证环境:开发机 Chromium 155(`/usr/bin/chromium-browser`),经本机代理 `http://127.0.0.1:10808` 访问,日期 2026-11-12。测试乐谱:Satie《Gymnopédie No.1》,id 6969530(公有领域)。

| 功能 | 状态 | 结果 |
| --- | --- | --- |
| `musescore-search gymnopedie` | 未复测 | 前次(2026-11-12)实测返回搜索结果 JSON |
| `musescore-info <url>` | 未复测 | 前次实测:标题、作曲、调号、上传者、价格、缩略图完整 |
| `musescore-get --kind mid` | 未复测 | 前次实测:3146 字节,`MThd` 魔数 |
| `musescore-get --kind mp3` | 未复测 | 前次实测:3,004,707 字节,MPEG 帧同步字节 `ff fb` |
| `musescore-get --kind pdf` | 未复测 | 前次实测:1 页,90,429 字节,`%PDF-1.4` |
| `musescore-get --kind mxl` | 未复测 | 前次实测:返回 `not_found`(该乐谱无 MusicXML) |
| 纯数字 id | 未复测 | 前次实测:返回 `bad_url` |
| 普通 HTTP 403 挑战后回退浏览器 | 未复测 | 前次实测:httpx 得 403,浏览器通过挑战后取得页面 |
| 集成测试 `pytest -m integration` | 未执行 | 本轮未运行;本轮未对 musescore.com 发起任何实网访问 |
| 离线单元测试 `uv run pytest` | 已实测(本轮) | 123 passed, 1 deselected(集成用例按默认跳过) |
| `uv run ruff check` | 已实测(本轮) | All checks passed! |
| `uv run ruff format --check` | 已实测(本轮) | 23 files already formatted |
| pyright | 未执行 | 本轮未运行;前次记录 0 errors,已过期 |
| 沙箱内 `/usr/bin/chromium` | 未验证 | 开发机无此路径;沙箱镜像内需实际启动确认 |
| 沙箱内访问 musescore.com | 未验证 | 需按下节的白名单确认出网 |
| 多页乐谱的 PDF 拼接 | 未验证 | 仅单元测试(假浏览器) |
| 40 MiB 上限与超限报错 | 未验证 | 仅单元测试 |
| 1 秒节流 | 未验证 | 仅单元测试(假时钟) |
| 原子写入失败清理 | 未验证 | 仅单元测试 |
| `blocked` 的 60 秒超时路径 | 未验证 | 仅单元测试 |

## 出网失败模式与所需白名单

沙箱出网经 mihomo 代理,外部流量仅允许 HTTP/HTTPS(80/443)。若 musescore.com 被拦截:

- 普通 HTTP 连接失败,报 `download_failed`;
- 浏览器无法通过 Cloudflare 挑战,报 `blocked`;
- 浏览器内的请求被路由层中止,无法取得 `/api/jmuse` 与构建脚本。

需要在沙箱出网策略中放行 HTTPS 443 到以下主机:

```
musescore.com
www.musescore.com
static.musescore.com
s3w.musescore.com
cdn.ustatik.com
```

`musescore.com` 同时承担挑战页(`/cdn-cgi/challenge-platform`)、构建脚本(`/static/public/build/`)与 `/api/jmuse`,因此该主机必须整体放行。`cdn.ustatik.com` 只用于搜索缩略图。仓库中 `caged/egress/whitelist.txt` 未找到,出网规则以 `caged/egress/render.py` 为准,本包未修改该文件。

## 安装(镜像内)

`sandbox/Dockerfile` 中的现有行(与 `findata` 相同模式):

```dockerfile
COPY tools/cgdl /opt/cgbot/tools/cgdl
RUN python3 -m pip install --no-cache-dir --no-deps /opt/cgbot/tools/cgdl \
    && ln -sf "$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cgdl" /usr/local/bin/cgdl \
    && cgdl --help >/dev/null
```

Playwright 与 httpx 已由 `sandbox/requirements.txt` 安装到默认 Python,因此 `--no-deps`。浏览器通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium` 指定,镜像不下载 Playwright 浏览器(`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`)。

## 开发与测试

```sh
cd sandbox/tools/cgdl
uv run --with pytest --with ruff --with pyright --with httpx --with playwright --with-editable . python -m pytest -q
uv run --with ruff --with-editable . ruff check src tests
uv run --with pyright --with httpx --with playwright --with-editable . pyright
```

- 单元测试使用内嵌 HTML/JSON 夹具与假传输层,不访问网络。
- 集成测试标记为 `integration`,默认跳过。运行方式:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser HTTPS_PROXY=http://127.0.0.1:10808 \
  uv run --with pytest --with httpx --with playwright --with-editable . pytest -m integration -q
```

测试仅发起少量请求(搜索 1 次、信息 1 次、mid 与 mp3 各 1 次)。
