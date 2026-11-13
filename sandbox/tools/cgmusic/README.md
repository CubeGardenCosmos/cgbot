# cgmusic

乐谱到音频的编排器。输入 MIDI 或 MusicXML,输出母带处理后的 mp3 / wav / flac,并在 stdout 输出一行 JSON 报告。

`cgmusic` 是 Python 编排层,负责前处理、分轨调度与母带。实际合成由 C++ 核心 `cgsynth` 完成;它只做合成,每次调用只渲染一个声部。

## 架构

```text
 cgmusic render <score>
        │
        ▼
 ┌──────────────────────────────┐
 │ 1. 输入检查                   │  后缀 · 输入 ≤32 MiB · MIDI ≤8 MiB
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────┐
 │ 2. cgscore prepare           │  MIDI/MusicXML → parts/<id>.mid + plan.json
 └──────────────┬───────────────┘  (Python, 手部合并、踏板、source_channel)
                ▼
 ┌──────────────────────────────┐
 │ 3. 计划整理                   │  时长 ≤15 min · 丢弃无音符声部
 │                              │  引擎 / 音色 / 混响发送量解析
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────────────────────┐
 │ 4. 并发 cgsynth render(每声部一个子进程)      │
 │    P1.mid ─► cgsynth ─► stems/P1.wav          │
 │    P2.mid ─► cgsynth ─► stems/P2.wav   (×jobs)│
 │    …                                          │
 │    任一失败 → 取消其余子进程,原样抛出首个错误  │
 └──────────────┬───────────────────────────────┘
                ▼
 ┌──────────────────────────────┐
 │ 5. cgmaster.master           │  声像 · 增益 · 混响 · 响度 -14 LUFS · 限幅 -1.5 dBTP
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────┐
 │ 6. 汇总 JSON → stdout         │  临时目录在成功与失败时均删除
 └──────────────────────────────┘
```

依赖关系:`cgmusic` → `cgscore`(前处理)、`cgmaster`(母带)、`cgsynth`(合成,子进程)。`cgmusic` 不链接 C++ 代码。

## 安装与运行

```bash
cd sandbox/tools/cgmusic
uv sync            # 或离线场景使用 uv run --offline
uv run cgmusic --help   # 与旧 C++ CLI 一致:不支持 --help,返回 unsupported
```

`cgsynth` 通过以下方式定位:

- 环境变量 `CGSYNTH_BIN`:指向可执行文件的绝对路径(优先);
- 否则在 `PATH` 中查找 `cgsynth`。

找不到时返回 `synth_failed`。

音色目录由 `cgsynth` 读取:环境变量 `CGMUSIC_SOUNDFONTS`,默认 `/opt/soundfonts`。

## 命令

### `cgmusic render <file>`

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--engine fluidsynth\|muse` | `fluidsynth` | 全局引擎。声部的 `engine_hint` 为 `muse` 或 `fluidsynth` 时优先使用该提示。 |
| `--soundfont ID\|FILE` | 默认音色 | 仅作用于 fluidsynth 声部。与 muse 同用报 `bad_input`。 |
| `--font-for-channel CH=ID` | — | 源 MIDI 通道 CH(1–16)所在声部使用的音色,可重复。仅 fluidsynth 声部;指向 muse 声部报 `bad_input`。 |
| `--reverb CH=0-127` | — | 覆盖源通道 CH 所在声部的 `mix.reverb_send`,可重复。 |
| `--reverb-room R` | `0.75` | 混响房间大小,范围 [0, 1]。 |
| `--reverb-level L` | `1.0` | 混响电平,范围 [0, 1]。 |
| `--gain G` | `0` | 总增益(dB),范围 ±12。 |
| `--fmt mp3\|wav\|flac` | `mp3` | 输出格式。 |
| `-o OUT` | `/workspace/artifacts/music/<输入文件名>.<fmt>` | 输出路径。 |
| `--jobs N` | 见下 | 并发子进程数。 |

- 默认并发数:`min(CPU 核数, 4 若含 muse 声部,否则 8)`。
- 所有通道号均指**源 MIDI 通道**(1–16),与分轨后的声部 id 无关。
- 无音符的声部会被跳过(cgscore 为空通道生成的占位 MIDI 不会送入 cgsynth)。整首乐曲没有任何音符时报 `bad_input`。

### `cgmusic presets [--soundfont ID|FILE]`

透传 `cgsynth presets`,列出音色的预设。不指定时,使用 `cgsynth soundfonts` 中 `default: true` 的音色。

### `cgmusic soundfonts`

透传 `cgsynth soundfonts`,列出可用音色。

### `cgmusic analyze <audio>`

用 `cgmaster.quality` 测量响度、真峰值、响度范围、削波与时长。

## 输入限制

| 项目 | 上限 | 超出时错误码 |
| --- | --- | --- |
| 输入文件 | 32 MiB | `too_long` |
| MIDI 文件(含声部) | 8 MiB | `too_long` |
| 乐曲时长 | 15 min | `too_long` |

输入后缀:`.mid` `.midi` `.musicxml` `.xml` `.mxl`。其他后缀报 `unsupported`。

## 输出 JSON

`render` 成功时在 stdout 输出一行 JSON:

| 键 | 含义 |
| --- | --- |
| `path` `fmt` `duration_s` | 输出文件、格式、时长 |
| `lufs` `true_peak_dbtp` `lra` `clipped` | 母带后的响度、真峰值、响度范围、是否削波 |
| `balance` `reverb` | 每声部增益、声像、混响发送量 |
| `fonts` `missing_presets` `fallbacks` | 音色使用情况,及缺失预设/回退记录(均标注所属声部) |
| `muse` | 使用 muse 时的版本与轨道信息,否则为 `null` |
| `parts` | 每个声部的 `{id, name, engine, wall_s}` |
| `wall_s` | 总耗时(秒) |

## 错误

错误为一行 JSON,写入 stderr,退出码非零:

```json
{"error": "synth_failed", "message": "..."}
```

| 错误码 | 含义 |
| --- | --- |
| `bad_input` | 参数或乐谱不合法(重复通道、越界参数、无音符、muse 与音色选项冲突等) |
| `unsupported` | 不支持的命令或后缀(包括 `--help`) |
| `too_long` | 超出大小或时长上限 |
| `synth_failed` | cgsynth 失败;原始错误码(如 `muse_unavailable`)会被保留 |
| `muse_unavailable` | muse 引擎不可用 |
| `write_failed` | 无法创建输出目录或写入输出 |

## 临时文件

工作目录位于 `TMPDIR`(若为目录),否则位于输出目录的同级目录。`TemporaryDirectory` 在成功与失败时均会删除。禁止使用 `/tmp`。

## 验证状态

- 单元与集成测试:使用假 `cgsynth`(`tests/fake_cgsynth.py`)覆盖并发、取消、错误传播与汇总。
- 真实 `cgsynth`(fluidsynth 引擎)端到端渲染见本次验收记录。
- muse 引擎未做端到端验证。
