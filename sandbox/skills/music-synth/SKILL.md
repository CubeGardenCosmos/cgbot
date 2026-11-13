---
name: music-synth
description: Preprocess MIDI with the cgmidi CLI, render MIDI / MusicXML / MXL scores to audio (mp3, wav, flac) with the cgmusic CLI, and search or fetch Musescore scores with cgdl. Use when the user sends a score file (.mid .midi .musicxml .xml .mxl), asks to play, synthesize or render music, picks an instrument sound, or asks for a Musescore score.
---

# music-synth

三个命令行工具:`cgmidi`(MIDI 预处理)、`cgmusic`(渲染为音频)、`cgdl`(Musescore 搜索与下载)。
成功时在 stdout 输出一行 JSON;失败时在 stderr 输出 `{"error": code, "message": ...}` 并以非零码退出。
读取 JSON 字段,不要解析人类可读文本。

## 总流程

1. 用户发来的文件位于 `/workspace/inbox/`(聊天消息会给出确切路径)。
2. 若需要改谱(速度、移调、换音色、裁剪、静音、通道改派),先用 `cgmidi` 生成新的 `.mid`,再交给 `cgmusic`。`cgmidi` 只接受 `.mid`;MusicXML 与 MXL 直接交给 `cgmusic`。
3. 用 `cgmusic render` 渲染一次,然后用 `report_progress` 交付(见下文"工作流规则")。
4. 预处理的中间文件放在 `/tmp/cgmidi/`(先 `mkdir -p /tmp/cgmidi`),只有最终音频放入 `/workspace/artifacts/music/`。

## cgmidi(MIDI 预处理,渲染前运行)

```
cgmidi info <input.mid>
cgmidi process <input.mid> -o <output.mid> [--speed F | --bpm B] [--transpose N]
    [--mute CH]... [--solo CH]... [--map-channel SRC:DST]... [--set-program CH=PROG]...
    [--set-bank CH=MSB[:LSB]]... [--map-program SRC=DST]... [--start S] [--end E]
cgmidi hands <input.mid> -o <output.mid> [--channel CH]... [--window-beats F] [--accent-gap N]
cgmidi bars <input.mid> [--channel CH]...
cgmidi pedal <input.mid> -o <output.mid> [--mode auto|bar|chord] [--channel CH]... [--replace] [--gap-ticks N]
```

- `info`:输出速度(tempo)、时长、音符数、通道(1–16)、音色、音库、音域与轨道。修改前先用它看清通道与音色。
- `process` 选项(已用 `cgmidi process --help` 核对):
  - `--speed F`:速度倍率,0.1–10;`1.25` 加快 25%,`0.5` 减半。与 `--bpm` 互斥。
  - `--bpm B`:把全部速度标记替换为固定 BPM,1–999。与 `--speed` 互斥。
  - `--transpose N`:移调半音,-48 到 48。通道 10(鼓组)不移调。
  - `--mute CH`:静音通道 1–16,可重复。
  - `--solo CH`:独奏通道 1–16,可重复;指定后只保留这些通道的音符。
  - `--map-channel SRC:DST`:通道改派,如 `2:3`,可重复。
  - `--set-program CH=PROG`:强制通道音色 0–127,可重复。删除该通道原有的音色切换,并在 tick 0 写入新音色。
  - `--set-bank CH=MSB[:LSB]`:强制通道音库(CC0/CC32),LSB 默认 0。
  - `--map-program SRC=DST`:对所有通道做音色号映射 0–127,可重复。
  - `--start S` / `--end E`:按原乐谱时间(秒)裁剪。`--mute`、`--solo`、`--set-program`、`--set-bank` 使用原始通道号;`--map-channel` 在最后应用。
- 输入只读;输出路径不能与输入相同,且输出目录必须已存在。
- `hands`: 合并同通道同音色多轨(如左右手)力度基线。默认滑动中值窗口 2 拍,与自身基线差 `< 12` 的音符对齐声部基线,大于等于 12 的重音按相对差值保留。输出 JSON 包含 `notes_changed`、`accents_kept` 与声部前后平均力度。
- `bars`: 按拍号与速度标记列出小节网格与小节内和声音高类分段(pitch-class sets)。只读分析,输出 JSON,供 LLM 与编排阅读。
- `pedal`: 自动添加延音踏板(CC64)。在小节/和声交界前释放并在交界处重新踩下。`--mode` 可选 `bar`(每小节换)、`chord`(每和声段换)、`auto`(默认,琶音织体按和声换,其余按小节换)。默认不修改已有 CC64 的文件并返回 `already_pedaled`,加 `--replace` 允许覆盖。
- 成功输出 JSON;失败码:`bad_input`、`parse_failed`、`unsupported_format`、`out_of_range`、`write_failed`。参数错误时 argparse 退出码为 2 并以 JSON 报告。
示例:

```
mkdir -p /tmp/cgmidi
cgmidi info /workspace/inbox/score.mid
cgmidi process /workspace/inbox/score.mid -o /tmp/cgmidi/score_ep.mid --set-program 1=10 --transpose -2
```

## cgmusic(原生合成器)

```
cgmusic soundfonts
cgmusic presets [--soundfont ID|FILE]
cgmusic render <file> [--soundfont ID|FILE] [--font-for-channel CH=ID]...
    [--reverb CH=0-127]... [--reverb-room R] [--reverb-level L]
    [--fmt mp3|wav|flac] [-o OUT] [--gain G]
cgmusic analyze <audio>
```

cgmusic 没有 `--help`(`cgmusic --help` 返回 `unsupported`)。参数错误时返回 `bad_input` 并打印该子命令的用法。

- 输入:`.mid`、`.midi`、`.musicxml`、`.xml`、`.mxl`。输入文件不超过 32 MiB;MIDI 不超过 8 MiB;播放时长不超过 15 分钟(超出返回 `too_long`)。
- `--soundfont ID|FILE`:音色 id,必须来自 `cgmusic soundfonts` 的列表。
- `--font-for-channel CH=ID`:把某个 MIDI 通道(1–16)绑定到另一个音色,可重复。用于混合音色。
- `--reverb CH=0-127`:通道 1–16 的混响发送(CC91),同一通道重复指定会返回 `bad_input`。CLI 指定的值优先于文件内的 CC91 与内置默认表。
- `--reverb-room R`、`--reverb-level L`:全局混响房间大小与电平,范围 0–1。
- `--gain G`:增益(dB),范围 -12 到 12,超出返回 `bad_input`。真峰值上限 -1.5 dBTP 仍然生效。只有用户要求改变响度时才使用。
- `--fmt`:默认 mp3。只有用户明确要无损时才用 `wav` 或 `flac`。
- **`-o` 的扩展名必须与 `--fmt` 一致。** 实测:`-o x.wav` 而不加 `--fmt wav` 写出的是 MP3 内容。最稳妥的做法是同时指定匹配的 `--fmt` 与后缀。
- `-o OUT`:默认输出到 `/workspace/artifacts/music/`。
- 声部电平是内置自动规则,**不能调整**(没有分声部增益参数)。
- `render` 成功输出 JSON 的主要字段:`path`、`fmt`、`duration_s`、`midi_seconds`、`soundfont`、`lufs`、`true_peak_dbtp`、`lra`、`clipped`,以及 `balance`、`reverb`、`fonts`(含 `missing_presets`)、`fallbacks`。
- `soundfonts` 返回 `{"dir": "/opt/soundfonts", "fonts": [{"id", "file", "bytes", "default", ...}]}`。`/opt/soundfonts` 只读且共享,不要尝试写入或下载音色。
- `presets --soundfont ID` 返回 `{"soundfont", "file", "presets": [{"bank", "program", "name"}]}`。
- 错误码:`bad_input`、`too_long`、`no_soundfont`、`synth_failed`、`bad_musicxml`、`unsupported`。把 `message` 告诉用户。`too_long` 时直接说明超长,不要自行拆分或重新编码。

### 选择音色

先运行一次 `cgmusic soundfonts`,按列表中的 id 选择,不要凭文件名猜测。下列 id 为当前镜像内已核对的真实 id。**这些 id 只在宿主机音色目录中实际存在时才会出现**;当前部署目录只有 `musescore_general`。若需要的 id 不在列表中,改用 `default: true` 的音色,并在报告中用一句话说明。

- **通用 / 默认**:`musescore_general*`(例如 `musescore_general`)。`default: true` 的音色属于 General MIDI 音库,用户没有指定音色时使用。
- **钢琴**:`generaluser-gs` 或 `masterpiece`。可先用 `cgmusic presets --soundfont <id>` 确认预设。
- **电钢琴缺省音色(DX7)**:用户要电钢琴,或对钢琴类 MIDI 没有指定键盘音色时,缺省使用 `yamaha_dx7_rom_1a` 的预设 `E.PIANO 1`(bank 0、program 10,已用 `cgmusic presets --soundfont yamaha_dx7_rom_1a` 核对)。
  - 此音库的 program 编号**不是** GM(0 号是 BRASS 1),直接渲染 GM 钢琴 MIDI 会变成铜管。每个要用电钢琴的旋律通道都必须 `--set-program CH=10`(通道用 `cgmidi info` 查看)。
  - 已实测的整首流程:`cgmidi process IN.mid --set-program 1=10 -o ep.mid`,再 `cgmusic render ep.mid --soundfont yamaha_dx7_rom_1a --fmt mp3 -o OUT.mp3`(《Nothing's Gonna Change My Love for You》,约 260 秒,-14.0 LUFS,`missing_presets` 为空)。
  - 混用:主音色 `musescore_general`,电钢琴通道用 `--font-for-channel 1=yamaha_dx7_rom_1a` 绑定,并对该通道 `--set-program 1=10`。
  - **该音库没有鼓组。** 渲染时通道 10 会出现 `No preset found on channel 9` 的 fluidsynth 警告,该通道无声。乐谱含鼓组时,应把鼓组通道用 `--font-for-channel 10=musescore_general` 绑定到通用音库(此绑定未实测,使用前先确认)。
  - 该音库中其他 FM 预设为备选,只在用户明确要求时使用。
- **室内乐 / 管弦乐**:`neo_orch_realistic`。

用户指定某种音色时,优先满足;否则按上面的类别选择。选择音色时不要为了找"更好"的音色而反复渲染。

## cgdl(Musescore 搜索与下载)

```
cgdl musescore-search <query> [--limit N]
cgdl musescore-info <url>
cgdl musescore-get <url> --kind mid|mxl|mp3|pdf [-o DIR]
```

已用 `cgdl <子命令> --help` 核对。当前版本只有上述三个子命令,搜索没有过滤参数,也没有 `musescore-preview`。

- `--limit N`:结果数量 1–50,默认 10。
- 乐谱必须使用完整 URL:`https://musescore.com/user/<uid>/scores/<id>`。纯数字 id 会返回 `bad_url`。
- `--kind`:`mid`、`mxl`、`mp3` 或 `pdf`。`pdf` 逐页获取乐谱页面图像,由 Chromium 合成为 A4 PDF,最多 200 页。`mxl` 对没有 MusicXML 的乐谱返回 `not_found`。
- `-o DIR`:绝对路径,默认 `/workspace/artifacts/music/`。
- 文件名由标题与乐谱 id 生成(`Title_Words-<id>.<ext>`)。单个下载文件与 PDF 的上限为 40 MiB。
- 错误码:`blocked`(浏览器中 Cloudflare 挑战 60 秒内未通过)、`not_found`、`parse_failed`、`download_failed`、`unsupported_kind`、`bad_url`、`bad_input`、`browser_unavailable`。
- 只搜索或下载用户明确要求的内容。不要爬取相关乐谱、其他声部或用户主页。
- 若下载的是 `mid` 或 `mxl` 且下一步要渲染,直接交给 `cgmusic`(`mid` 可先经 `cgmidi`)。

## 工作流规则

1. **一次请求只渲染一次。** 用最合适的音色渲染并交付。不要为了调响度或"试试其他音色"而循环重渲染,除非用户明确要求改动。
2. **渲染失败最多重试一次,且只在错误信息指向明确的修复方法时**(例如音色 id 写错)。否则直接报告失败。
3. **声部电平自动,不能修改。** 可以调整混响(`--reverb`、`--reverb-room`、`--reverb-level`),也可以用 `--gain` 改响度,但只在用户要求时使用。
4. **交付:** 用 `report_progress`,并设置 `files=[{"path": "/workspace/artifacts/music/<文件>", "send_as": "file"}]`。文件必须位于 `/workspace` 之下。`send_as` 只对音频有效:`"file"` 以文件发送(可保存),`"voice"` 以语音消息播放(有损且有时长上限)。用户要听语音消息选 `"voice"`,要拿到文件选 `"file"`;不明确时用一句话问用户后再发送。
5. **报告要简短:** 时长(`duration_s`)、综合响度(`lufs`,目标为 -14 LUFS)、真峰值(`true_peak_dbtp`)与是否削波(`clipped`)。例如:"时长 2:31,响度 -14.0 LUFS,真峰值 -1.6 dBTP,未削波"。不要粘贴整段 JSON,也不要解释指标含义。
6. 若 `clipped` 为 true,在报告中说明。只有用户想要时才提供更低 `--gain` 的渲染,不要自行重渲染。
7. 若 `fonts` 中出现非空 `missing_presets`,或 `fallbacks` 非空,在报告中用一句话说明。
8. **MIDI 的混响 0 在 tick 0 视为未设置。** 文件中在 tick 0 写入的 CC91 为 0 时,会被当作未设置,由 cgmusic 自行决定。要明确指定混响发送为 0,使用 `--reverb CH=0`。
9. 不要用 pip、apt 或下载方式安装合成器、插件或音色。上述工具已足够。
10. **Musescore 个人使用提醒:** 每次抓取了 Musescore 文件后,在最终报告中提醒一次:乐谱仅供用户个人学习与试听,不得再分发,并请尊重作者与授权条款。
