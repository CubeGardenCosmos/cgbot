# cgsynth

原生 C++17 干声合成核心。输入是前处理生成的**单声部标准 MIDI 文件**，输出是该声部的 44.1 kHz、立体声、32 位 IEEE float WAV。核心只负责 MIDI 解码、FluidSynth SoundFont 合成和 MuseSampler 离线合成。

不在此处做 MusicXML 解析、力度/风格决策、轨道映射、电平/声像、混响、回声、混音、响度、真峰值限制、裁切、淡出或 MP3/FLAC 编码；这些分别属于 Python 前处理与后处理。

## 构建依赖（Debian bookworm）

`libfluidsynth-dev libsqlite3-dev`，以及动态加载 MuseSampler 所需的 `libdl`。不再需要 pugixml、libzip、libsndfile、libmp3lame 或 libebur128。

```sh
cmake -S . -B build
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## 命令

```sh
cgsynth render <part.mid> --engine fluidsynth|muse [--soundfont ID|FILE] -o <stem.wav>
cgsynth soundfonts
cgsynth presets <file>
```

- `render` 成功时在 stdout 输出一行 JSON，含 `engine`、`duration_s`、`soundfont`、`fonts`、`missing_presets`、`fallbacks` 和 `muse`。错误以 JSON 写 stderr，退出码非 0。
- 一个文件只允许一个发声 MIDI 通道；同一声部可以有多个 MIDI 轨（例如钢琴双手）。所有通道控制、Program Change 与 CC0/CC32 均直接来自文件。
- 长度为 MIDI 结束时间加 4 秒释音尾巴。FluidSynth 内部 reverb/chorus 固定关闭；输出不做归一化，允许样本值超出 ±1。
- `--soundfont` 仅用于 FluidSynth；未指定时使用默认字体。字体目录由 `CGMUSIC_SOUNDFONTS` 指定，缺省 `/opt/soundfonts`。音色缺失时沿用最近音色选择，并在 `missing_presets`/`fallbacks` 显式报告。
- Muse 库路径由 `MUSESAMPLER_LIB` 指定，缺省 `/opt/musesampler/lib/libMuseSamplerCoreLib.so`；样本根由 `MUSE_SAMPLES_DIR` 指定，缺省 `/opt/muse-sounds`。专有库和样本不放入仓库或镜像。
- Muse 通过实际许可的乐器表与 `.instruments` 目录交集选择音色。MIDI 轨道名只在**精确等于**目录乐器名时优先使用，否则按 GM 程序映射；不做声部名关键词猜测。GM 45 映射为拨奏关节。
- 没有可用 Muse 乐器时返回 `muse_unavailable`，调用方可改用 FluidSynth 单独渲染该声部。核心不会静默换引擎。

## 内部模块

- `src/midi.cpp`：标准 MIDI 0/1/2（metrical timing）解析、速度与通道事件。
- `src/render.cpp`：FluidSynth 单声部事件派发与干声采样。
- `src/muse/`：MuseSampler ABI、动态库加载、SQLite 乐器目录、GM 程序映射、音符配对与离线渲染。
- `src/wav.cpp`：无第三方依赖的 float WAV 写出。
- 可选探针 `muse_probe` 由 `-DCGMUSIC_BUILD_PROBE=ON` 打开，默认关闭。

## 依赖选择

FluidSynth 负责成熟、可复现的 SoundFont 合成；SQLite 只读访问 Muse `.instruments`；MuseSampler 通过既有 C ABI 动态加载。WAV 容器结构简单，直接写出避免为单一格式保留 libsndfile。乐谱与母带算法由 Python 包使用 mido、pedalboard、soundfile、pyloudnorm 等成熟库完成。
