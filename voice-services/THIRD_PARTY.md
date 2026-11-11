# 第三方声明

## GPT-SoVITS(`sovits/core/`,MIT)

`sovits/core/` 是上游 [RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 推理/训练代码的一份拷贝(含少量宿主机本地改动,具体不可与上游 diff,见 README「本地改动」)。

上游 LICENSE(取自 `https://raw.githubusercontent.com/RVC-Boss/GPT-SoVITS/main/LICENSE`):

```
MIT License

Copyright (c) 2024 RVC-Boss

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

宿主机拷贝里没有 LICENSE 文件,也没有 git 元数据;上游提交号无法确定。文件头原样保留。

## 上游代码内嵌的其他来源(文件头中可见,已保留)

| 位置 | 版权声明(文件头原文) | 许可证 |
|---|---|---|
| `core/text/zh_normalization/*`、`core/text/tone_sandhi.py`、`core/text/g2pw/dataset.py`、`g2pw/utils.py` | Copyright (c) 2020–2022 PaddlePaddle Authors | 文件头标明 Apache License 2.0 |
| `core/AR/modules/optim.py`、`scaling.py` | Copyright 2022 Xiaomi Corp. (Daniel Povey) | 文件头未在此核对;上游 icefall 为 Apache-2.0 [推论] |
| `core/module/core_vq.py`、`distrib.py`、`quantize.py` | Copyright (c) Meta Platforms, Inc. and affiliates | 文件头未在此核对;源自 Meta encodec,MIT [推论] |
| `core/module/core_vq.py` | Copyright (c) 2020 Phil Wang | [未核对] |
| `core/AR/models/t2s_lightning_module.py` | 注释:modified from yangdongchao/SoundStorm;reference lifeiteng/vall-e | [未核对] |

`cmudict.rep` / `cmudict-fast.rep` 源自 CMU Pronouncing Dictionary(许可证 [未核对])。

## FunASR(`asr/`)

`asr/server.py` 为本项目所写,通过 pip 依赖 `funasr`(不 vendor 其源码)。FunASR 许可证与模型许可证(SenseVoiceSmall)请以其官方页面为准 [未核对]。

## 不随仓库分发的内容

预训练与角色模型、`lid.176.bin`、日语词典:见 [MODELS.md](MODELS.md);各自许可证须在获取时确认。

## 本目录自有代码

`sovits/server.py`、`sovits/client.py`、`sovits/test_gpu_squeeze.py`、`asr/server.py` 与 `systemd/`:GPL-3.0,同本仓库。
