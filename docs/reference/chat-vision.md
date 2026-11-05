# 聊天多模态图片输入(参考)

2026-11-05 用户决定(`[已定]`,[DESIGN §13.2b](../DESIGN.md)):聊天必须把图片交给多模态模型。任务见 [R1.8](../todos/R1.8-chat-multimodal-images.md)。

## 数据流

1. 入站 `image` 段(私聊 `App.handle_message`、群聊 `GroupSessionManager`)取 `url`,缺省取 `file`。文字照旧渲染为 `[图片]`。
2. `vision/fetch.py`:仅公网 http(s),不走代理、不跟随重定向,解析出的每个地址都校验,20 MiB 上限,魔数只认 PNG/JPEG/WebP/GIF。沙盒收件箱(`coding/staging.py`)用同一实现。
3. `vision/image.py`:Pillow 解码(GIF 取首帧、应用 EXIF 方向),长边缩到 `max_edge`;有透明保留 PNG,否则 JPEG(质量 85)。
4. `vision/store.py`:字节落在 `<data_dir>/images/<private|group>-<范围 ID>/<时间戳>-<随机>.<jpg|png>`(目录 0700、文件 0600)。会话历史的用户项只多一个内部片段 `{"type": "cgbot_image", "ref": <文件名>}`,不存 base64。
5. 每次构造请求时,`AgentRunner` 取窗口中最近 `recent_image_turns` 个带图用户轮次的引用,按**本会话的范围**读取并渲染为 `{"type": "input_image", "image_url": "data:<mime>;base64,..."}`;其余引用(更早的、已被清理的、不属于本会话的)直接丢弃,文字里的 `[图片]` 保留。
6. 供应商:`responses.py` 原样透传;`chat.py`(`wire_api = "chat"`)把用户消息的 `input_image` 映射为 `image_url` 片段,其他角色的图片丢弃。

## 隔离与降级

- 引用只是文件名,读取时由会话 ID 决定目录(`private:<用户>` → `private-<用户>`,`group:<群>` → `group-<群>`),跨范围解析为空;文件名严格正则校验,拒绝路径穿越与符号链接。
- 下载或解码失败:跳过该图,日志只记错误码(如 `code=download_failed`),不含 URL;该轮照常进行。
- 压缩:`transcript()` 只取文字片段;压缩请求与 token 估算都经 `render_items` 丢弃图片引用,不含 blob。压缩保留的尾部行仍带引用,所以保留的最近几轮之后依然可展开。
- 群主动回话:缓冲只记最近消息的图片地址(不下载),触发主动回话时只取最新的 `max_images_per_turn` 张下载。

## 保留

每个范围最多保留 `retention_images` 张、`retention_days` 天;保存时清理该范围,启动及之后每小时清理所有范围,并删除超过 1 小时的遗留临时文件。

## 配置

```toml
[provider.vision]
enabled = false            # 默认关闭
max_images_per_turn = 4
recent_image_turns = 3
max_edge = 1536
retention_images = 200
retention_days = 30
```

`deploy/config.toml` 模板启用;`config.example.toml` 保持关闭。

## 未验证

上游对 `input_image` 数据 URL 的真实接受、NapCat 真实图片地址的可下载性与有效期;引用消息图片未支持。
