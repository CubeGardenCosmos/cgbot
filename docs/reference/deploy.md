# cgbot 部署参考(M5 容器)

上层:[运行手册](runbook.md)、[Roadmap 索引](../ROADMAP.md)。本文记录 bot 镜像、compose 与宿主机布局。不记录任何真实密钥;宿主机地址仅作为运维事实列出。

状态:构建产物已在仓库中定义,**尚未部署**。切换到 M5 栈是需要用户授权的人工步骤(见 §7)。

## 1. 拓扑

单进程 `cgbot` 容器同时承载:NapCat 反向 WS 服务端(容器内 `6200`)、coding 网关 + bridge aiohttp 服务(`8000`)、沙盒 manager。镜像不发布端口。

| 网络 | 类型 | 用途 | 谁在其中 |
|---|---|---|---|
| `qqbot_astrbot_network` | 外部,有出站 | NapCat 连入 `ws://cgbot:6200/ws`;bot 出站访问上游 LLM | napcat(172.18.0.2)、cgbot |
| `dsh_caged_internal` | 外部,`internal` | 沙盒访问 `http://cgbot:8000` 与 `dsh-egress`(出站代理) | cgbot、沙盒 |
| `dsh_caged_ctl` | 外部,`internal` | cgbot 访问 `dsh-docker-proxy:2375` | cgbot、dsh-docker-proxy |

三个网络均由旧 `dsh-docker-caged` 项目创建,compose 只以 `external: true` 引用,从不创建或删除。

依赖的旧项目容器(切换前须保持运行):`dsh-docker-proxy`(Docker API 白名单)与 `dsh-egress`(沙盒出站代理)。

## 2. 镜像

- 文件:`Dockerfile`、`.dockerignore`(白名单:仅 `pyproject.toml`、`uv.lock`、`src/`)。构建上下文为仓库根。
- 基础镜像 `python:3.12-slim`。builder 阶段装 `build-essential`(因 `pilk==0.2.4` 只有 sdist)与 `uv==0.12.23`;执行 `uv export --frozen --no-dev --no-emit-project --format requirements-txt` 导出带 sha256 的依赖清单,再 `uv pip install --require-hashes --no-deps` 到 `/opt/venv`,最后安装项目本身。runtime 阶段只拷贝 venv 并安装 `ffmpeg`、`ca-certificates`、`tzdata`。
- 镜像源:Debian 与 PyPI 均使用 `mirrors.aliyun.com`(apt 设为 DIRECT,与 `sandbox/Dockerfile` 的策略一致)。事实(已在 .19 实测):构建容器访问 `mirrors.tuna.tsinghua.edu.cn` 的 Debian 包返回 403,改用 aliyun 后成功;构建容器访问 `files.pythonhosted.org` 连接超时(宿主机直连正常),因此不使用 `uv sync`(它会按 lock 中的 pythonhosted URL 下载),改用 hash 校验的 `--require-hashes` 从镜像安装。推论:每个文件仍由 lock 中的 sha256 校验,镜像只替换下载来源。
- 入口:`python -m cgbot --config /app/config/config.toml --secrets /run/secrets/cgbot.toml`。
- 健康检查:容器内请求 `http://127.0.0.1:8000/healthz`,依赖 coding 服务器(slice A/D 交付)。在其存在前,容器为 `unhealthy`,这是预期的。
- 以 root 运行(推论,有取舍):manager 需要把每用户工作区 `chown` 为 `1000:1000`。compose 用 `cap_drop: ALL` 仅加回 `CHOWN`、`DAC_OVERRIDE`、`FOWNER`,并设 `no-new-privileges` 与只读根文件系统(`/tmp` 为 tmpfs)。替代方案(容器以 uid 1000 运行,并预先 chown 宿主机目录)未采用,因为会改变旧 manager 已验证的权限模型。

## 3. 配置与密钥

- `deploy/config.toml` 是模板。真实文件位于宿主机 `/home/klp/cgbot-run/config.toml`(0644,不含密钥),挂载为只读 `/app/config/config.toml`。补全所有带 `【占位】` 的项后,`deploy.sh` 才会放行。
- `deploy/secrets.toml.example` 只列名字。真实 `/home/klp/cgbot-run/secrets.toml` 必须 `chmod 600`,挂载为只读 `/run/secrets/cgbot.toml`。需要的名字:`provider_api_key`、`coding_token`,可选 `imagegen_key`。
- 容器内路径约定:数据目录 `[storage] data_dir = "/app/data"`(绝对路径);`[coding.bridge] temp_dir = "/app/data/bridge"`(绝对路径)。宿主机 `/home/klp/cgbot-run/state` 挂载为 `/app/data`。
- 占位值:`[provider] context_window = 128000`(标注待查证);`[scope]` 的 `friends`/`groups` 为空列表(全部拒绝,安全默认);`[coding.imagegen]` 省略(图像生成不可用)。

## 4. 宿主机布局(手工一次性创建,deploy.sh 不创建)

```
/home/klp/cgbot-build/bot/        源码同步目录(deploy.sh 维护;docker build 在此进行)
/home/klp/cgbot-run/              运行目录
  docker-compose.yml              由 deploy.sh 从仓库复制
  .env                            由 deploy.sh 写入:CGBOT_TAG、HOST_DATA_DIR(非密钥)
  config.toml                     手工,0644
  secrets.toml                    手工,0600
  state/                          手工创建;bot SQLite 与 bridge 临时文件
/home/klp/cgbot-data/             沙盒工作区;容器内同路径挂载(Docker 守护进程按宿主机路径解析)
```

## 5. 部署命令

在仓库根执行:

```bash
deploy/deploy.sh --check            # 只读:docker/网络/目录/配置占位/secrets 权限
deploy/deploy.sh --build            # rsync 源码 + docker build(只构建,不启动)
deploy/deploy.sh --up               # check + build + 写 .env + compose up -d --no-deps cgbot + 等待 healthy
deploy/deploy.sh --rollback <tag>   # 切换到已存在的 cgbot:<tag> 并重建 cgbot
```

- 镜像标签形如 `cgbot:<UTC时间戳>-<git短SHA>`,同时打 `cgbot:latest`。回滚时使用 `docker images cgbot` 查得的旧 tag。
- `--up` 不会自动回滚。`unhealthy` 或超时即退出并给出排障与回滚提示。
- 脚本只操作 compose 项目 `cgbot` 的 `cgbot` 服务。若已存在名为 `cgbot` 且不属于该项目的容器,脚本拒绝继续。
- 脚本不执行 `docker system prune`、`builder prune` 或 `image prune`。清理需人工判断,避免影响旧 dsh 镜像。

## 6. 资源与风险

- 宿主机:4 CPU,约 7.8 GB RAM(部署前约 5.9 GB 已用),`/` 剩余约 26 GB。
- bot 容器:`mem_limit 1g`、`cpus 2`、`pids_limit 512`(推论,待实测)。
- 沙盒容器按旧 manager 的 2 GB 上限估算(推论)。`per_user` 并发与沙盒数量叠加后,可能超过宿主机剩余内存。上线前须实测。
- 旧 dsh 的镜像与构建缓存体量较大(部署前 images 42 GB,其中 26 GB 可回收;build cache 41 GB)。本部署不清理它们。

## 7. 切换清单(需用户授权,逐项人工执行)

1. 创建 §4 的宿主机目录;写入 `config.toml`(补全占位)与 `secrets.toml`(0600)。
2. 确认 `dsh-docker-proxy` 与 `dsh-egress` 保持运行。
3. **Docker 代理白名单缺口**:`dsh-docker-proxy` 的 `haproxy.cfg` 只允许 containers create/inspect/start/stop/exec/delete、networks inspect、`/version`、`/_ping`、`/containers/json`。不允许 `/images/*`、`/containers/*/logs`、`/containers/*/wait`。M5 manager 若需要这些调用,须先扩展白名单。该文件属于旧 dsh 项目,本部署未修改。
4. NapCat 反向 WS:在 `/home/klp/qqbot/napcat/config/onebot11_1059657671.json` 中把 `cgbot` 的 `websocketClients` 地址改为 `ws://cgbot:6200/ws`(当前为临时探测地址 `ws://192.168.1.19:6200/ws`)。改前先备份。NapCat 需重启才生效。bot 必须已加入 `qqbot_astrbot_network` 才能解析 `cgbot`。
5. 运行 `deploy/deploy.sh --up`。
6. 旧栈(旧 manager、gateway、astrbot)的停止与清理由用户决定。本部署不停止任何既有容器。

## 8. 验证记录

已实测(均在 `192.168.1.19`,只读或仅构建,未启动任何 cgbot 容器):

- 本地:`bash -n deploy/deploy.sh` 通过;`docker compose -f deploy/docker-compose.yml config -q` 通过。
- 远端只读检查:`deploy/deploy.sh --check` 报告 docker daemon、compose、三个外部网络均存在,且 `cgbot` 容器不存在;`/home/klp/cgbot-data`、`/home/klp/cgbot-run/{state,config.toml,secrets.toml}` 缺失(预期,需手工创建)。脚本以非零退出。
- 构建:`deploy/deploy.sh --build` 成功,产出 `cgbot:20261030115559-58e12bf` 与 `cgbot:latest`(构建上下文 `/home/klp/cgbot-build/bot`)。过程中两次失败并已修复:tuna Debian 镜像对构建容器返回 403(改为 aliyun);`files.pythonhosted.org` 连接超时(改为 hash 校验的 `uv export` + `uv pip install --require-hashes`)。
- 烟测(`docker run --rm --entrypoint ...`):`import cgbot` 成功(site-packages 安装、Python 3.12.16);`ffmpeg -version` 为 7.1.5;`/opt/venv/bin/python` 为解释器;`TZ=Asia/Shanghai`;`python -m cgbot --help` 退出码 0。
- compose 校验(远端,临时目录 `/tmp/cgbot-cfgcheck`,已删除):`docker compose -p cgbot-check -f ... config -q` 通过;渲染结果含 `read_only`、`cap_drop/cap_add`、`mem_limit 1g`、`pids_limit 512`、三个外部网络、secrets 与 `/home/klp/cgbot-data` 挂载。
- 清理:删除 `cgbot:latest` 与版本标签镜像;`docker image prune -f`(仅悬空镜像)。该步骤使镜像总量从 42GB 降到 29GB。无法判断被删悬空镜像的来源。`python:3.12-slim`(177MB,ID aa943ce41142)为本次拉取,未删除。构建缓存未清理(21GB 可回收,其中包含旧项目的缓存)。

## 9. 未验证项

- 未执行 `compose up`:真实 NapCat 连入、coding 服务 `/healthz`(当前容器 HEALTHCHECK 预期失败)、沙盒并发内存、`context_window` 实际值。
- Docker 代理白名单是否满足 M5 manager 的全部调用(`/images/*`、logs、wait 未在旧 haproxy 白名单中)。
- 容器内 `/healthz` 与 `bridge` 端到端路径未测,依赖 slice A/D/E。
