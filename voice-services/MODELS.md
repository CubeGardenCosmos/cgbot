# 需单独获取的模型与数据文件

这些文件不入库(见 `.gitignore`)。大小为只读 `ls`/`du` 在原宿主机上观测到的值;下载地址与许可证标注「未验证」的,是依据上游文档的记忆,部署前须核对。

## 一、GPT-SoVITS 预训练模型(必需)

放在 `sovits/core/` 下。`sovits/server.py` 固定从这些位置读取。

| 文件 | 放置位置 | 观测大小 | 上游来源 |
|---|---|---|---|
| `chinese-hubert-base/`(含 `model.safetensors` 及同目录的 config/preprocessor 等小文件) | `sovits/core/GPT_SoVITS/pretrained_models/chinese-hubert-base/` | 181 MB | 上游 GPT-SoVITS 的预训练包(Hugging Face `lj1995/GPT-SoVITS` 下的同名目录)。[未验证] |
| `chinese-roberta-wwm-ext-large/`(含 `model.safetensors`、tokenizer 等) | `sovits/core/GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/` | 1.3 GB | 同上;原模型为 `hfl/chinese-roberta-wwm-ext-large`。[未验证] |
| `lid.176.bin`(fastText 语种识别) | `sovits/core/pretrained_models/fast_langdetect/lid.176.bin` | 126 MB | fastText 官方 `lid.176.bin`。路径由 `core/text/LangSegmenter/langsegmenter.py` 的 `cache_dir` 指定(已读代码确认);是否缺失时自动下载 [未验证]。 |

宿主机上的 `safetensors` 文件是转换/下载后的形式;目录内其余小文件(config.json 等)在迁移时被排除,须随模型一并下载。

> 注意:`TTS_Config` 在这些路径不存在时会回退到默认的相对路径 `GPT_SoVITS/pretrained_models/...`(已读 `core/TTS_infer_pack/TTS.py`),所以路径错误时启动可能以另一种方式失败,而不是明确报缺文件。

## 二、角色模型(必需,由部署者自备)

放在 `CGBOT_SOVITS_MODEL_DIR` 指向的目录(默认 `sovits/models/default/`),文件名固定:

| 文件 | 含义 | 参考大小(原部署) |
|---|---|---|
| `sovits.pth` | SoVITS(v2)权重 | 82 MB |
| `gpt.ckpt` | GPT(s1)权重 | 149 MB |
| `ref.wav` | 参考音频 | 227 KB |

并通过环境变量 `CGBOT_SOVITS_REF_PROMPT_TEXT`(及 `CGBOT_SOVITS_REF_PROMPT_LANG`)给出 `ref.wav` 的逐字文本。原部署目录里还有一个约 2.7 MB 的 RVC 权重,`server.py` 未引用它,故不需要。

> 这些是针对特定角色训练的权重与参考音频,本仓库不提供。使用前自行确认训练数据与角色/声音的授权。

## 三、文本数据(随仓库提供或可再生)

- 随仓库:`core/text/cmudict.rep`、`cmudict-fast.rep`(各约 3.5 MB)、`g2pw/polyphonic-fix.rep`(1.6 MB)等小文件。
- 缓存(未入库,删除安全程度不同,均已读代码核对):
  - `core/text/engdict_cache.pickle`(5.7 MB):缺失时由 `.rep` 重新生成(`english.py: get_dict`)。
  - `core/text/g2pw/polyphonic.pickle`(1.3 MB):缺失或 md5 不符时由 `.rep` 重建(`g2pw.py`)。
  - `core/text/namedict_cache.pickle`(743 KB):**不会**重建,缺失时人名词典为空 `{}`(`english.py: get_namedict`),只影响英文人名读音,不影响启动。
- 可选:`core/text/ja_userdic/userdict.csv`(17 MB,日语用户词典)。缺失时 `japanese.py` 跳过(`os.path.exists` 判断);仅日语合成需要。获取方式:上游 GPT-SoVITS 仓库同路径文件 [未验证]。

## 四、ASR 模型

`asr/server.py` 使用 `iic/SenseVoiceSmall`(ModelScope)。`CGBOT_ASR_MODEL_PATH` 为空或目录不存在时,使用模型 ID 由 FunASR 通过 ModelScope 下载(首次启动需联网)。原宿主机的本地快照路径形如 `…/modelscope/models/iic--SenseVoiceSmall/snapshots/master`;其大小未单独测量。

## 目录总览

```
voice-services/sovits/
├── core/GPT_SoVITS/pretrained_models/{chinese-hubert-base,chinese-roberta-wwm-ext-large}/   # 下载
├── core/pretrained_models/fast_langdetect/lid.176.bin                                       # 下载
├── core/text/ja_userdic/userdict.csv                                                        # 可选
└── models/default/{sovits.pth,gpt.ckpt,ref.wav}                                             # 自备
```
