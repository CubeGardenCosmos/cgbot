# cgmaster

把 `cgsynth` 生成的 44.1 kHz 干声分轨混合为成品。输入只有 `plan.json` 和 `<stems>/<声部id>.wav`；不读取 MIDI，不依赖 `cgmidi`/`cgscore`，没有 ffmpeg 依赖。

## 命令与库接口

```sh
cgmaster mix plan.json --stems stems --fmt mp3 -o master.mp3
cgmaster mix plan.json --stems stems --fmt wav -o master.wav \
  --gain 3 --reverb-room 0.75 --reverb-level 1.0
```

```python
from cgmaster import master
report = master("plan.json", "stems", "master.flac", fmt="flac")
```

成功时 stdout 是一行 JSON，接口返回同一字典：

```json
{"path":"/work/master.mp3","fmt":"mp3","duration_s":20.0,"lufs":-14.0,
 "true_peak_dbtp":-1.6,"lra":4.2,"clipped":false,"balance":[],
 "reverb":{"room":0.75,"level":1.0,"channels":[]},
 "tracks":[{"id":"P1","rms_dbfs":-18.2,"gain_db":-6.0}]}
```

失败时 stderr 为 `{"error":"bad_input|synth_failed|write_failed","message":"..."}`，退出码为 1；argparse 参数错误退出码为 2。

## 调音台规则

`parts[].mix` 是前处理写好的**最终参数**：

- `gain_db`：声部最终推子电平，直接施加一次。C++ 的自动平衡基础 -6 dB、woodwind/accordion 额外 -6 dB、brass +6 dB、鼓声部绝对电平，以及 Muse 打击乐/FluidSynth 回退的 -12 dB，都应由 `cgscore` 折算到此字段；本包不再叠加，以免重复衰减。兼容未填 `gain_db` 的旧计划时，才按 `family`、`is_drum`、`engine_hint`、`muse_name` 推导上述族级规则（鼓族无法区分 kick/snare，按 other drum -6 dB）。
- `pan`：-1（左）到 +1（右），等功率声像。
- `reverb_send`、`echo_send`：接受 0–1 比例；大于 1 时按 CC 的 0–127 标度解释。发送位于推子/声像之后。
- 所有声部共享一条混响总线：`HighpassFilter(150 Hz) -> Reverb(damping=0.4, width=1, wet=1, dry=0) -> LowpassFilter(8500 Hz)`，再乘 `--reverb-level`。这是旧 C++ Freeverb 总线对应的 pedalboard 实现，不保证逐采样相同。
- 回声总线为 `Delay(0.35 s, feedback=0.25, mix=1)`；旧 C++ 没有回声总线，因此没有伪造对应的旧默认值，未发送时完全关闭。

## 母带与编码

1. 总线压缩器 `ratio=2.5`、attack 30 ms、release 120 ms，阈值 = 输入积分响度 +3 dB。旧 C++ 只在 Muse 混音路径开启此级，新架构的每个分轨已由 `engine_hint` 指定合成器，故在所有 stems 的最终总线上应用；pedalboard Compressor 没有公开 knee/双释放/侧链 HP 参数，保留压缩意图而非宣称逐采样一致。
2. 自动补偿到 `-14 LUFS + clamp(--gain, -12, +12)`。`pedalboard.BrickwallLimiter` 使用 5 ms 前瞻、4x 真峰值侧链、无自动 makeup；先以低于上限 0.5 dB 的预限制，再按旧 `loudness.cpp` 的六轮割线法同时逼近目标响度与 **-1.5 dBTP** 上限。无法同时达到时选响度误差最小者，不牺牲峰值上限。峰值始终不随 `--gain` 放宽。
3. 以任一通道绝对值 >0.001（约 -60 dBFS）定位首尾并裁剪；末尾 0.5 秒线性淡出。全静音或低于响度计门槛时明确报 `synth_failed`。
4. MP3 用 pedalboard `AudioFile` 的 LAME 编码，192 kbit/s，与旧 `encode.cpp` 相同；WAV/FLAC 用 soundfile，分别写 32-bit float 和 24-bit PCM（FLAC 不支持 float 子类型）。输出使用临时文件和原子替换。
5. LUFS/LRA 用 pyloudnorm 的 BS.1770 门控积分响度和 EBU 3342 LRA。真峰值用 `scipy.signal.resample_poly(..., 4, 1)`；它与 libebur128 的滤波器系数不同，因此与旧 C++ 允许小幅测量差异。`clipped` 表示最终样本峰值 >= 0 dBFS；报告中真正的非有限响度用 JSON `null`。

## 依赖选型

- **pedalboard**：成熟的 JUCE 效果器与 LAME 编码接口；负责混响、延迟、压缩、前瞻限制和 MP3。
- **soundfile**：libsndfile 的 WAV/FLAC I/O；不需要外部 ffmpeg。
- **pyloudnorm + numpy**：BS.1770 响度、LRA 和向量混音。
- **scipy**：4x 多相过采样真峰值测量。

## 开发

```sh
cd sandbox/tools/cgmaster
uv sync
uv run pytest -q
uv run ruff check src tests
```

测试使用内存生成的正弦/噪声 stems，不依赖真实音源、网络或 ffmpeg。
