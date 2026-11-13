# cgmusic

原生 C++17 命令行工具:把 MIDI 或 MusicXML(含 .mxl)渲染为音频,经 FluidSynth 或 MuseSampler 合成后按 EBU R128 处理(-14 LUFS,真峰值 ≤ -1.5 dBTP,去首尾静音,末尾淡出),输出 mp3 / wav / flac。

## 构建依赖(Debian bookworm)

`libfluidsynth-dev libebur128-dev libsndfile1-dev libmp3lame-dev libpugixml-dev libzip-dev libsqlite3-dev`

## 构建与测试

```
cmake -S . -B build && cmake --build build -j
ctest --test-dir build --output-on-failure
```

## 用法

```
cgmusic render <file> [--engine fluidsynth|muse] [--soundfont ID|FILE] [--fmt mp3|wav|flac] [-o OUT] [--gain G] [--reverb CH=0-127]... [--reverb-room 0-1] [--reverb-level 0-1]
cgmusic soundfonts
cgmusic analyze <audio>
```

- stdout 输出一行 JSON;错误输出 `{"error":code,"message":...}` 到 stderr,退出码非 0。
- 音色目录:环境变量 `CGMUSIC_SOUNDFONTS`,默认 `/opt/soundfonts`(只读)。
- `--engine` 默认 `fluidsynth`;`muse` 使用真实 MuseSampler 库。库路径由 `MUSESAMPLER_LIB` 指定,缺省 `/opt/musesampler/lib/libMuseSamplerCoreLib.so`;样本根由 `MUSE_SAMPLES_DIR` 指定,缺省 `/opt/muse-sounds`(包含 `.instruments`,均只读)。不将库或样本放入仓库/镜像。
- `--engine muse`: 使用真实 MuseSampler 库。乐器候选集由库实际通过收据许可的乐器表（`list_instruments`）与样本目录库（`.instruments` SQLite）取交集构建。对于 GM 程序号无可用/已许可 Muse 乐器的发声通道，显式回退到 FluidSynth（使用默认 SoundFont 渲染该通道干声），并混合进同一共享混音母带；在输出 JSON 的 `fallbacks` 数组中记录 `{"channel": ch, "program": prog, "reason": "muse_instrument_not_licensed"}`。若所有发声通道均需回退，则返回 `muse_unavailable` 报错。
- 库调用异常保护：所有跨 C ABI 的库调用（`add_track`、`init`、`process_offline` 等）均包裹在 try/catch 中，转换为结构化 JSON 错误输出，避免未捕获异常导致 `std::terminate` 崩溃。
- Muse 与回退通道干声按乐器族进入 `src/mix/` 的自动平衡、混响发送、总线混响与 -14 LUFS / -1.5 dBTP 母带；输出 JSON 的 `muse.tracks` 给出 Muse 轨道报告，`muse.mix` 给出整体混音报告。
- Muse 引擎的母带在线性增益之前多一级动态处理(`src/mix/dynamics.cpp`,仅 `--engine muse`;fluidsynth 路径不经过,逐位不变,有测试):声道联动前馈总线压缩(阈值 = 输入积分响度 +3 dB,比率 2.5:1,6 dB 软拐点,侧链 100 Hz 高通,攻击 30 ms,两级释放 120 ms/900 ms)→ 自动补偿增益至目标响度 → 5 ms 前瞻峰值限制(上限 = 真峰值上限 -0.5 dB)→ 原有迭代真峰值母带。`muse.mix.loudness.dynamics` 报告 `threshold_db`、`ratio`、`max_gain_reduction_db`、`avg_gain_reduction_db`、`makeup_db`。参数取值与实测见 [docs/reference/musesampler.md](../../../docs/reference/musesampler.md) §21;未做听感验收。
- 默认输出目录 `/workspace/artifacts/music/`。
- `--gain G`:相对 -14 LUFS 目标的 dB 偏移,限 ±12;真峰值上限仍为 -1.5 dBTP。
- `--reverb CH=0-127`(可重复):MIDI 通道 1-16 的混响发送(CC91),优先级最高,覆盖文件中的 CC91 与默认表。
- `--reverb-room`、`--reverb-level`:全局混响房间大小与电平,取值 0-1。
- 声部电平平衡为内置固定规则,调用方不能修改:非鼓组声部按 -6 dB 基础电平加偏移后求和;鼓组的踢鼓(+6 dB)、军鼓(0 dB)、其他鼓件(-6 dB)为绝对增益,不叠加基础电平。
- 发送量优先级:`--reverb` 覆盖 > 文件中显式的 CC91/CC93 > 内置默认表(cgmusic 自行调校,非 MuseScore 数值;MuseScore 不向 FluidSynth 发送混响)。文件中 tick 0 处值为 0 的 CC91/CC93 视为未设置,由默认表决定;显式设置一经出现即保持到文件末尾。
- JSON 额外包含 `balance`(各声部偏移与实际增益)与 `reverb`(房间、电平、各通道最终发送量 `reverb_send`/`chorus_send` 及来源 `reverb_source`/`chorus_source`,取值 `cli`、`file`、`table`)。
- 上限:MIDI 字节 8 MiB,播放时长 15 分钟(超出返回 `too_long`)。长乐谱会占用约为时长 × 44100 × 2 × 4 字节 × 3 的内存。

## 内部模块

- `src/mix/`:混响总线与混音器(`reverb`、`mixer`)及可选的母带动态处理(`dynamics`),供 Muse 引擎使用。
- `src/muse/`:MuseSampler 运行库加载器(`loader`,`dlopen` + 版本门)、Muse 目录库读取(`catalog`,SQLite 只读)、声部映射(`mapping`)与离线渲染(`render`)。
- 可选探针 `muse_probe` 由 `-DCGMUSIC_BUILD_PROBE=ON` 打开,默认关闭。

## 依赖选择 / 库调研

- 没有可直接使用的 MusicXML→MIDI 库:Debian bookworm 与 vcpkg 均无此类打包库。
- Grame libmusicxml 功能完整,但未在 bookworm 打包,引入需自建第三方依赖,未采用。
- Verovio 体量过大(渲染为主,面向排版),为转换 MIDI 引入它得不偿失,未采用。
- libsmf 不需要:SMF 由 `src/musicxml.cpp` 直接按字节写出,解析端已有 `midi.cpp`。
- 选定依赖:`libpugixml-dev`(解析 XML)与 `libzip-dev`(读取 .mxl 压缩包,内存读取、无临时文件)。
- 已删除 `src/third_party/` 下的 vendored 副本,改由 CMake 通过 pkg-config 查找系统库。

