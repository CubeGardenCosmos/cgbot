# cgbot 运行手册

上层:[TODO 索引](../todos/README.md)、[Roadmap 索引](../ROADMAP.md)。本文记录如何启动、配置、排障与离线门禁;不记录任何真实密钥或部署地址。

## 1. 启动

```bash
cp config.example.toml config.toml        # 本地配置,不入库
uv run python -m cgbot --config config.toml [--secrets secrets.toml]
```

- 配置错误(未知键、缺失必填项、非法取值)启动即失败,退出码 2,错误信息给出键路径。
- 配置只在启动时加载一次,无热重载。修改后需重启。
- `SIGINT` / `SIGTERM` 触发正常收尾:关闭 NapCat 服务、调度器、会话与记忆存储。

## 2. 密钥

配置文件只写引用名(如 `api_key_ref = "provider_api_key"`),值按以下顺序取得:

1. 环境变量 `CGBOT_SECRET_<名字大写>`(优先)。
2. secrets 文件的 `[secrets]` 表。路径取 `--secrets` 参数,否则环境变量 `CGBOT_SECRETS_FILE`。

secrets 文件权限必须为 `0600`(组和其他用户不得有任何权限),否则拒绝启动。secrets 文件与 `config.toml` 均不入库。

## 3. NapCat 反向 WS

框架是 WebSocket 服务端,NapCat 以反向 WS 客户端连入:

- 地址:`[napcat] host`/`port`/`path`(默认 `127.0.0.1`、必填端口、`/ws`)。
- 握手要求 `x-self-id` 头(缺失或非数字返回 400);路径错误 404;配置了 `token_ref` 时 `Authorization: Bearer` 不符返回 401。
- 同一 self_id 的新连接替换旧连接。
- **NapCat 侧的反向 WS 条目只能由用户亲自或明确授权后新增;原有条目不得改动。** 框架不修改 NapCat 配置。

## 4. 服务范围

- `[scope] friends`:私聊白名单(QQ 号)。`all_friends = true`(默认 false)时,所有真实 QQ 好友也进入私聊范围(以 NapCat 私聊事件 `sub_type == "friend"` 判定,临时会话不算)。`groups`:群白名单,群内任何成员均在范围内,不受 `all_friends` 影响。
- 范围外消息在进入身份、会话与 LLM 之前丢弃。省略整节即全部拒绝。
- 群聊仅在 `groups` 内生效;群内容不进入任何私聊会话。

## 5. 群聊配置

`[group]` 键(默认值多为推论,待实测调整):

| 键 | 含义 |
| --- | --- |
| `wake_keywords` | 唤醒关键词;需 @ 机器人且消息含关键词才唤醒 |
| `mute_keywords` | 闭嘴关键词;默认空 |
| `idle_timeout_seconds` | 群会话空闲结束秒数(默认 600) |
| `max_turns` | 单次群会话最多轮数(默认 30) |
| `skill_topics` / `skill_memes` / `skill_rules` / `skill_members` / `skill_habits_per_member` | 群 SKILL 各类条目上限 |

后台每 30 秒执行一次群会话超时扫描。进程启动时群会话从存储恢复。

## 6. 语音(可选)

- 省略 `[voice]` 整节即语音关闭:私聊语音仅记为 `[语音]`,不注册 `send_voice` 工具。
- 启用时需要:`ffmpeg` 可执行、`[voice.asr]` 与 `[voice.tts]` 指向**已在运行**的守护进程。框架不启动、不重启、不改动 SoVITS/ASR 服务端。
- 语音转写失败时私聊内容为 `[语音识别失败]`。
- `[voice.tts] path_rules` 用于把远端返回路径映射为本机可读路径;映射结果须为可读 WAV。

## 7. 数据目录

`[storage] data_dir`(相对配置文件目录,默认 `data`)下每个域一个独立 SQLite 文件,互不共用:

| 文件 | 内容 |
| --- | --- |
| `profile.sqlite` | 身份与入口 |
| `sessions.sqlite` | 会话日志(私聊与群会话均按会话 id 区分) |
| `scheduler.sqlite` | 调度器系统事件 |
| `memory.sqlite` | 记忆本(私聊与群可见域) |
| `group_state.sqlite` | 群门控状态 |
| `group_skill.sqlite` | 群 SKILL |

数据目录不入库;删除任一文件会丢失对应数据,操作前先备份。

## 8. 日志

`[logging] level`:`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`。日志格式为 `时间 级别 模块: 消息`。

## 9. 离线质量门禁

在仓库根目录执行(顺序不限,全部应通过):

```bash
uv run ruff check src tests
uv run ruff format src tests
uv run pyright            # 0 errors
uv run pytest
```

以上均为离线测试:不访问网络、NapCat、上游模型或语音服务,不使用真实密钥。

## 10. 未经真实环境验证的边界

以下项目仅有离线测试证据,上线前须在授权环境中实测:

- 真实 NapCat 反向 WS 收发与 `get_record` 语音取回路径。
- 上游 Responses 的 `previous_response_id`、`reasoning.effort`、`instructions`、并发与 429。
- 真实 ASR/TTS 转写与合成(需守护进程在线)。
- `pilk` 未安装:SILK 编码不可用,`send_voice` 调用会失败并返回错误结果;需先在授权环境添加依赖后再验证 record 发送。
- 群聊真实边界(唤醒、静音、会话结束与总结的真实群行为)。
- 配置中所有标注 `[推论]` 的默认值(上限、档位映射、超时)。

## 11. 部署(M5 容器)

镜像构建、compose、宿主机目录与切换步骤见 [部署参考](deploy.md)。
