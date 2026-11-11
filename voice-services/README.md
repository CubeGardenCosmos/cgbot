# voice-services

cgbot 语音功能的宿主机侧守护进程:**SoVITS(TTS)**与 **FunASR(ASR)**。cgbot 通过 `[voice]` 配置连接(见 `config.example.toml`、`src/cgbot/voice/`)。完整部署步骤见 [docs/reference/voice-services.md](../docs/reference/voice-services.md)。

```
voice-services/
├── README.md  MODELS.md  THIRD_PARTY.md  .gitignore
├── asr/        server.py  requirements.txt              # FunASR SenseVoiceSmall 守护进程(默认 TCP 10095)
├── sovits/     server.py  client.py  test_gpu_squeeze.py  requirements.txt
│   └── core/   # GPT-SoVITS 推理代码(MIT,见 THIRD_PARTY.md)
└── systemd/    funasr.service  sovits.service  *.env.example
```

## 协议

4 字节大端长度 + UTF-8 JSON,一问一答,连接可复用,长度范围 (0, 50 MiB]。与 `src/cgbot/voice/wire.py` 一致;两个服务同时监听 UNIX socket 与 TCP。动作:`ping`;ASR `transcribe`(`audio_b64` 或 `container_path`,`language`);TTS `synthesize`/`tts`(`text`、`language`,返回 `host_path`、`container_path`),另有 `health`、`status`。

## 收录决定(vendor)

宿主机 `core/` 实测:107 个 py 文件合计约 1.4 MB(按块计),另有三个 1 MB 以上的词典文件 `core/text/{cmudict,cmudict-fast}.rep`、`core/text/g2pw/polyphonic-fix.rep`(合计约 8.7 MB,文本,运行时必需)。代码部分远小于 ~5 MB 阈值,故**收录源码**并保留上游文件头(整个 `sovits/` 约 12 MB,是这三个文本词典使然)。不收录:模型权重、`pretrained_models/`、`*.pickle` 缓存、17 MB 的日语 `userdict.csv`、音频。清单与获取方式见 [MODELS.md](MODELS.md)。上游提交号**无法确定**(宿主机拷贝无 git 元数据、无 LICENSE 文件、目录布局与上游不同:上游 `GPT_SoVITS/` 内容被平铺到 `core/`),所以不能给出 pin;上游仓库为 https://github.com/RVC-Boss/GPT-SoVITS。

## 本地改动

相对宿主机原始文件(上游代码无法 diff,下面只列已读到的):

1. `asr/server.py`、`sovits/server.py`:原先写死的 socket 路径、容器/宿主挂载前缀、监听地址与端口、模型路径、角色名与参考文本,改为环境变量(默认值):`CGBOT_VOICE_DATA_DIR`(`/var/lib/cgbot-voice`,同时是原 `HOST_MOUNT_PREFIX`)、`CGBOT_VOICE_CONTAINER_PREFIX`(`/data`)、`CGBOT_ASR_SOCKET`/`CGBOT_SOVITS_SOCKET`(`<DATA_DIR>/{funasr,sovits}.sock`)、`CGBOT_ASR_HOST`/`CGBOT_SOVITS_HOST`(**`127.0.0.1`**,原为 `0.0.0.0`)、`CGBOT_ASR_PORT`(10095)/`CGBOT_SOVITS_PORT`(10096)、`CGBOT_ASR_MODEL_PATH`(空,原为本地快照路径;空则用 `iic/SenseVoiceSmall` 在线下载)、`CGBOT_SOVITS_MODEL_DIR`(`sovits/models/default`)、`CGBOT_SOVITS_CHARACTER`(`default`)、`CGBOT_SOVITS_REF_PROMPT_TEXT`(空)、`CGBOT_SOVITS_REF_PROMPT_LANG`(`zh`)。
2. 角色模型文件名统一为 `sovits.pth`、`gpt.ckpt`、`ref.wav`(原部署为按角色命名的 `models/<角色>/…` 文件);`REF_PROMPT_TEXT` 原为写死的角色台词,现由环境变量提供。`DEFAULT_CHARACTER` 同理。
3. `sovits/client.py`、`sovits/test_gpu_squeeze.py`:同样改用环境变量,去掉私有路径、角色名和硬件型号;测试句改为中性文本。
4. 注释中的原部署系统名改为中性表述。
5. 未收录 `GPT_SoVITS/configs/tts_infer.yaml`(含绝对路径);`TTS_Config` 在缺省时自行生成。
6. 其余文件逐字拷贝。

宿主机 `core/` 中 mtime 晚于批量解包时间(17:18–17:19)的文件,即很可能被本地修补或新增(**仅凭 mtime 的推论,内容未与上游比对**):`TTS_infer_pack/{__init__,streaming,text_segmentation_method,TextPreprocessor,TTS}.py`、`tools/{acceleration,my_utils,portable_runtime,audio_sr}.py`、`tools/i18n/*`、`feature_extractor/*`、`sv.py`、`text/{chinese,chinese2,tone_sandhi}.py`、`text/LangSegmenter/langsegmenter.py`。其中可从代码直接看到的:`sv.py` 对 `ERes2NetV2` 导入失败做了空类回退;`langsegmenter.py` 把 fast_langdetect 缓存目录指向 `core/pretrained_models/fast_langdetect`;`sovits/server.py` 通过 `sys.modules` 为 `BigVGAN`、`f5_tts`、`torchmetrics`、`pytorch_lightning`、`peft`、`matplotlib` 注入空模块(守护进程不需要训练依赖)。

## 运行要点

- 工作目录必须是 `sovits/`:`TTS_Config` 用相对路径 `GPT_SoVITS/configs/` 并在该处创建目录。
- UNIX socket 创建后被 `chmod 666`,任何本机用户都能连;TCP 无认证。默认只绑定 `127.0.0.1`。若改为 `0.0.0.0`,须自行用防火墙限制。
- 推理由 `asyncio.Lock` 串行化;TTS 一次请求只取生成器的第一段音频(`next(gen)`),`text_split_method` 为 `cut0`。
- TTS 把 wav 写到 `<DATA_DIR>/temp/`(权限 0666),cgbot 通过 `path_rules` 映射读取。

## 依赖

`asr/requirements.txt` 来自宿主机 FunASR venv 的 `dist-info` 目录名(Python 3.12,torch 2.5.1 + CUDA 12 轮子)。SoVITS 无独立 venv,沿用同一清单,为推论,见 `sovits/requirements.txt`。
