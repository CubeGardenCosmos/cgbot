# deploy/

cgbot 容器部署产物(M5)。完整说明见 [docs/reference/deploy.md](../docs/reference/deploy.md)。

| 文件 | 用途 | 入库 |
|---|---|---|
| `docker-compose.yml` | `cgbot` 服务定义(外部网络、只读根、capability 收窄、挂载) | 是 |
| `config.toml` | 配置模板(复制到宿主机运行目录后补全「占位」项) | 是 |
| `secrets.toml.example` | 密钥名字清单(占位值) | 是 |
| `deploy.sh` | `--check` / `--build` / `--up` / `--rollback <tag>` | 是 |
| `omp-search-relay.py` | 宿主机 Gemini 搜索 relay(标准库 asyncio,调用 `omp search`) | 是 |
| `omp-search-relay.service` | relay 的 systemd **用户**单元示例(不会被自动安装) | 是 |

真实的 `config.toml` 与 `secrets.toml` 只存在于宿主机 `/home/klp/cgbot-run/`,不入库。

```bash
deploy/deploy.sh --check
```

## omp 搜索 relay(宿主机)

bot 的 `gemini_search` 工具经 `host.docker.internal:8791` 调用宿主机上的 relay,relay 再以
`omp search --model=… --limit=… "<query>"` 访问 Gemini grounding。令牌只在宿主机文件与 bot 进程内存在;沙盒看不到令牌。

安装(由人工在 192.168.1.19 执行一次,仓库脚本不会自动安装或启用):

```bash
# 1. 令牌文件:0600,不得被组/其他读取(relay 启动时会校验)
install -d -m 700 ~/.config/omp-search-relay
umask 077 && openssl rand -hex 32 > ~/.config/omp-search-relay/token

# 2. 单元:复制到用户单元目录,确认 ExecStart 中的路径与 --bind 地址
install -D -m 644 ~/cgbot/deploy/omp-search-relay.service ~/.config/systemd/user/omp-search-relay.service
systemctl --user daemon-reload
systemctl --user enable --now omp-search-relay

# 3. 健康检查(无鉴权,不含敏感信息)
curl -fsS http://172.17.0.1:8791/healthz
```

要点:

- `--bind` 默认 `172.17.0.1`(docker 默认网桥网关),使容器可达而公网不可达。不要绑定 `0.0.0.0`。
  如宿主机网桥地址不同,改单元里的 `--bind`;`docker network inspect bridge` 可查看网关。
- bot 侧 `[search.gemini]` 的 `endpoint` 填完整 URL:`http://host.docker.internal:8791/search`
  (`deploy/docker-compose.yml` 已配置 `host.docker.internal:host-gateway`)。`token_ref` 指向 `secrets.toml` 中的令牌名。
- `--allow-model` 可重复指定允许的 model;`--model` 总是被允许。query 长度上限 300 字符,limit 1–10。
- 日志为结构化 JSON 行,由 journald 收集:`journalctl --user -u omp-search-relay -n 50`。日志只含 code、状态与耗时,不含 query、回答或令牌。
- 停止:`systemctl --user stop omp-search-relay`,收到 SIGTERM 后在 `--drain-seconds` 内等待在途请求再退出。
- 本地调试可直接运行 `python3 deploy/omp-search-relay.py --bind 127.0.0.1 --port 8791 --token-file <file>`。
