# deploy/

cgbot 容器部署产物(M5)。完整说明见 [docs/reference/deploy.md](../docs/reference/deploy.md)。

| 文件 | 用途 | 入库 |
|---|---|---|
| `docker-compose.yml` | `cgbot` 服务定义(外部网络、只读根、capability 收窄、挂载) | 是 |
| `config.toml` | 配置模板(复制到宿主机运行目录后补全【占位】项) | 是 |
| `secrets.toml.example` | 密钥名字清单(占位值) | 是 |
| `deploy.sh` | `--check` / `--build` / `--caged-up` / `--up` / `--rollback <tag>` | 是 |

音乐合成(`[music]`)的部署要点:沙盒镜像须在宿主机重新构建(含 `cgmusic`);音色目录默认 `$CGBOT_HOST_DATA_DIR/soundfonts`(192.168.1.19 上默认 `$HOME/cgbot-data/soundfonts`),由 bot 启动时后台下载默认音色并只读挂载到沙盒 `/opt/soundfonts`。步骤见 [deploy.md §3.4](../docs/reference/deploy.md)。`deploy.sh --build` 把 `sandbox/skills/music-synth/SKILL.md` 一并同步进 bot 镜像上下文。

真实的 `config.toml` 与 `secrets.toml` 只存在于宿主机运行目录,不入库。

```bash
deploy/deploy.sh --check
```
