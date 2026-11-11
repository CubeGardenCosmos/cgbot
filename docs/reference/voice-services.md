# 语音服务部署参考(SoVITS + FunASR)

上层:[部署参考](deploy.md)、[运行手册](runbook.md)。本文是 AI 部署代理的执行手册:按 §0 规则工作,按 §3 顺序执行。代码与单元文件在 [`voice-services/`](../../voice-services/README.md)(文件清单、本地改动、收录决定见其 README;模型清单见 [MODELS.md](../../voice-services/MODELS.md);第三方声明见 [THIRD_PARTY.md](../../voice-services/THIRD_PARTY.md))。

## 0. 代理规则

- 触及宿主机的命令须用户明确授权;先只读检查。
- 不打印密钥。本服务无密钥。
- 缺少 §2 的输入时停止并询问,不要猜测。
- 本文所有 `<…>` 为占位,不得把真实路径、IP、角色名写入仓库。

## 1. 前置条件

| 检查项 | 验证命令(在语音宿主机) | 预期 |
|---|---|---|
| NVIDIA 驱动与 GPU(可选;无 GPU 退化为 CPU,慢) | `nvidia-smi` | 列出 GPU |
| Python 3.12 | `python3.12 --version` 或 `uv python find 3.12` | `Python 3.12.x`(原 venv 为 uv 管理的 CPython 3.12) |
| 磁盘 | `df -h <install-dir> <model-dir>` | 至少约 3 GB 空闲(预训练模型约 1.6 GB + 角色权重 + 依赖轮子数 GB)[粗估] |
| systemd | `systemctl --version` | 打印版本 |
| cgbot 到语音宿主机的网络 | 见 §3.7 | — |

## 2. 输入表

| 输入 | 含义 |
|---|---|
| `<install-dir>` | 仓库 `voice-services/` 的父目录 |
| `<venv-dir>` | Python 虚拟环境目录 |
| `<service-user>` | 运行服务的非 root 用户 |
| `<data-dir>` | `CGBOT_VOICE_DATA_DIR`:放 socket 与 `temp/` 合成输出;若 cgbot 在容器里,须把它挂载给容器 |
| `<env-dir>` | systemd 环境文件目录 |
| `<model-dir>` | 角色模型目录(`sovits.pth`、`gpt.ckpt`、`ref.wav`) |
| 参考音频逐字文本、语言 | `CGBOT_SOVITS_REF_PROMPT_TEXT` / `_LANG` |
| 绑定地址 | 默认 `127.0.0.1`;cgbot 在另一台机器或容器网络时才改,并配防火墙 |

## 3. 步骤

### 3.1 获取代码

把仓库的 `voice-services/` 放到 `<install-dir>`(rsync 或 git checkout)。验证:`test -f <install-dir>/voice-services/sovits/server.py && echo ok` → `ok`。

### 3.2 建 venv 并安装固定依赖

```bash
python3.12 -m venv <venv-dir>
<venv-dir>/bin/pip install -r <install-dir>/voice-services/asr/requirements.txt
```

`sovits/requirements.txt` 只是 `-r ../asr/requirements.txt`:SoVITS 与 FunASR 在原宿主机上依赖同一环境(推论,见该文件注释)。两服务可共用一个 venv。验证:`<venv-dir>/bin/python -c "import torch, funasr; print(torch.__version__)"` → `2.5.1…`。torch 轮子的 CUDA 版本须与驱动兼容;失败时见 §5。

### 3.3 下载模型

按 [MODELS.md](../../voice-services/MODELS.md) 获取:`chinese-hubert-base`(181 MB)、`chinese-roberta-wwm-ext-large`(1.3 GB)、`lid.176.bin`(126 MB)到 `sovits/core/` 下指定位置;准备 `<model-dir>`。ASR 模型首次启动由 FunASR 在线下载 `iic/SenseVoiceSmall`(或设 `CGBOT_ASR_MODEL_PATH`)。验证:

```bash
cd <install-dir>/voice-services/sovits
ls -l core/GPT_SoVITS/pretrained_models/chinese-hubert-base/model.safetensors \
      core/GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/model.safetensors \
      core/pretrained_models/fast_langdetect/lid.176.bin <model-dir>/{sovits.pth,gpt.ckpt,ref.wav}
```

预期:全部存在。失败:缺哪个补哪个。

### 3.4 写环境文件

```bash
install -d <env-dir>
cp <install-dir>/voice-services/systemd/funasr.env.example <env-dir>/funasr.env
cp <install-dir>/voice-services/systemd/sovits.env.example <env-dir>/sovits.env
```

按 §2 填写占位。`<data-dir>` 须存在且 `<service-user>` 可写(`install -d -o <service-user> <data-dir>`)。

### 3.5 安装并启动单元(需 sudo 授权)

把 `voice-services/systemd/*.service` 复制到 `/etc/systemd/system/`,替换其中 `<service-user>`、`<install-dir>`、`<env-dir>`、`<venv-dir>`;然后:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now funasr sovits
```

SoVITS 的 `WorkingDirectory` 必须是 `sovits/`(`TTS_Config` 使用相对路径 `GPT_SoVITS/configs/`)。预期:`systemctl is-active funasr sovits` 两行 `active`。启动加载模型需要一段时间;`journalctl -u sovits -n 50` 末尾应出现 `TCP server listening at …`。

### 3.6 健康检查

用仓库自带客户端(会真实合成一句话,先确认 GPU 空闲):

```bash
cd <install-dir>/voice-services/sovits
CGBOT_VOICE_DATA_DIR=<data-dir> <venv-dir>/bin/python client.py
```

预期:`ping` 响应含 `"status": "ok"`、`cuda_available`、`vram_*`;`synthesize` 响应含 `container_path`、`host_path`、`duration_sec`。ASR 的 `ping` 响应含 `"status":"ok"` 与 `"model":"SenseVoiceSmall"`(须自写一次 4 字节长度前缀请求,见 `src/cgbot/voice/wire.py`;仓库不含 ASR 客户端)。

### 3.7 对接 cgbot

在 cgbot 的 `config.toml` 启用 `[voice]`(模板见 `config.example.toml` 的 `[voice]` 注释):

```toml
[voice]
[voice.asr]
host = "<asr-host>"     # 或 unix_path = "<data-dir>/funasr.sock"
port = 10095
[voice.tts]
host = "<tts-host>"     # 或 unix_path = "<data-dir>/sovits.sock"
port = 10096
path_rules = [{ remote = "<服务端 CGBOT_VOICE_CONTAINER_PREFIX 视角>", local = "<cgbot 视角的同一目录>" }]
```

要点(均已对照 `src/cgbot/voice/{asr,tts,paths,config}.py`):
- cgbot 的 ASR 请求为 `{"action":"transcribe","audio_b64":…,"language":…}`(不使用 `container_path`);TTS 请求为 `{"action":"synthesize","text":…,"language":…}`。
- TTS 响应只给出服务端写的文件路径(`host_path` 与 `container_path`),cgbot 要**读取该文件**,所以 `<data-dir>/temp/` 必须对 cgbot 可见:同机直接读 `host_path`;cgbot 在容器内时把 `<data-dir>` 挂进容器,并用 `path_rules` 映射 `container_path`。
- 默认 `127.0.0.1` 绑定:cgbot 在容器时通常需要 UNIX socket(挂载 `<data-dir>`)或把绑定地址改为容器可达的接口。

验证:cgbot 日志无语音连接错误,私聊发送语音得到转写;`send_voice` 工具能返回语音。(本机未执行,见 §6。)

## 4. 日常

- 日志:`journalctl -u sovits -u funasr -f`。
- 升级:替换 `voice-services/`,`systemctl restart`。回滚:换回旧目录再重启。
- TTS/ASR 各自串行推理(`asyncio.Lock`);cgbot 侧 `max_connections` 默认 1。

## 5. 故障排查

| 现象 | 处理 |
|---|---|
| SoVITS 启动报找不到 `pretrained_models` 或回退到相对默认路径 | 核对 §3.3 文件与 `WorkingDirectory` |
| `import peft`/`pytorch_lightning` 报错 | 不应出现:`server.py` 已注入空模块;若出现,说明导入顺序被改动 |
| 日语合成失败 | `pyopenjtalk` 不在固定清单中(原 venv 未发现),日语路径需自行安装 [未验证] |
| 显存不足 | `test_gpu_squeeze.py` 是原作者的显存实验脚本,用于评估两服务同卡能否共存 |
| cgbot 读不到 TTS 文件 | 检查 `path_rules` 与 `<data-dir>/temp/` 挂载及权限 |

## 6. 已知缺口

- **未在本机执行**:未安装依赖、未启动服务、未做任何推理;`py_compile` 通过不代表可运行。
- SoVITS 的运行环境与 Python 版本未在宿主机直接观测(无独立 venv);固定清单来自 FunASR venv,SoVITS 依赖是否齐全为推论。
- 清单里的 `torch==2.5.1` 与 `nvidia-*-cu12` 轮子与其他 CUDA 驱动的兼容性未验证;清单含 FunASR 的传递依赖,体积大。
- 上游 GPT-SoVITS 提交号未知;本地补丁只能列到文件级(见 voice-services/README.md)。
- `lid.176.bin` 缺失时是否自动下载、预训练模型的确切下载链接未验证。
- `0666` socket 与无认证 TCP:仅适用于受信本机/内网。
- TTS 一次请求只取生成器的第一段,超长文本可能被截断 [由 `next(gen)` 代码推论,未实测]。
- 无 ASR 客户端脚本、无 compose 文件(原部署未提供 Dockerfile,不臆造)。
