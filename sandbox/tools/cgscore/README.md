# cgscore

DAW 前处理(乐谱工作):把 MusicXML / MXL / SMF 拆成**每声部一个 MIDI 文件**,并生成
`plan.json`(声部元信息与混音默认值),供合成核心(`cgsynth`)与母带(`cgmaster`)使用。

## CLI

```bash
cgscore prepare <in.mid|.midi|.musicxml|.xml|.mxl> -o <outdir>
# 产物:<outdir>/parts/P1.mid ... Pn.mid, <outdir>/plan.json
# stdout: {"plan": ..., "parts": N, "duration_s": ...};出错时 stderr 输出 JSON 并非零退出
```

## 行为(以 C++ 核心 musicxml.cpp / midi.cpp / balance.cpp / sends.cpp / muse/mapping.cpp 为基准)

- **MusicXML**:partwise/timewise、tempo(`<sound tempo>`、`<metronome>` 含附点拍值)、
  `kDynamics` 力度表与 `<sound dynamics>` 百分比力度、连音(tie/tied)合并、装饰音
  (固定 30 tick)、和弦、backup/forward、反复与跳房子(volts)展开、移调
  (`<transpose>`)、鼓组词与 `midi-unpitched` 音高映射、由乐器名猜 GM 程序(最长匹配)。
- **拨奏**:拨奏段写 GM 程序 45(Pizzicato Strings),`arco` 还原为声部原程序
  (契约第 1 条;不再依赖核心私有拨奏扩展)。
- **每声部一个文件**:通道固定 0(鼓 9),无 16 通道碰撞;双手钢琴等多轨同声部
  合并为一个文件。每个文件都写完整速度表/拍号(C++ 只写第一轨,这里为独立渲染
  在每个文件复制指挥表)。
- **SMF 拆分**:按 (通道, 程序) 分组;通道内程序变化不拆声部(核心按事件逐通道
  渲染),鼓组合并为一个声部;保留全部 CC/弯音/触后与速度表。
- **plan.json 默认值**:
  - `family`:GM 程序族。
  - `mix.gain_db`:`balance.cpp` 家族电平(基准 -6 dB,木管/手风琴 -6、铜管 +6;
    鼓组声部级取 OtherDrum -6;Kick +6/Snare 0 为单音规则,未折入声部默认值)
    **再折入** `muse/render.cpp` 的 -12 dB 规则(FluidSynth 回退与 Muse 打击乐家族)。
    `cgmaster` 直接使用该值作为最终推子电平,不再叠加其他平衡。
  - `mix.reverb_send`:`sends.cpp` 的 CC91/127;`echo_send` 默认 0;`pan` 默认 0。
  - `muse_name`:目录精确乐器名(声部名关键词优先,含 Flute 2 / Violins 2 的出现
    次序规则),空串表示该程序保留给 FluidSynth(如 GM 4/5 电钢琴、手风琴)。
  - `engine_hint` 固定 `"muse|fluidsynth|null"`,由编排器/用户选择。

## 库选型

- **mido**:SMF 读写与编码(沙箱已预装,与 cgmidi 一致)。
- **自研紧凑 MusicXML 解析**(`musicxml.py`,约 600 行,基于标准库
  `xml.etree.ElementTree` + `zipfile`):评估过 music21(pm.musicxml 解析约 2.1 s,
  依赖 numpy 等,力度/反复/拨奏语义需大量二次转换才能达到 C++  parity)与
  partitura(约 3.7 s,拉入 scipy/pandas,且丢弃 metronome 方向等事件)。
  两者依赖 footprint 大、语义不贴合既有核心行为,故按 AGENTS 约束直接移植
  C++ 解析规则,保持数值完全一致(480 PPQ、四舍五入、同刻事件次序)。

## 扩展点:ABC 记谱

未来支持 `abc` 输入时,在 `prepare.py` 增加 `_SUFFIXES` 条目与一个
`prepare_abc(path) -> list[Part]` 函数(复用 `model.Part` / `write_plan` 与
`defaults` 表即可),契约不变。当前**未实现**。

## 开发

```bash
cd sandbox/tools/cgscore
uv run --offline pytest        # 单元测试(合成 XML/MIDI fixtures)
```
