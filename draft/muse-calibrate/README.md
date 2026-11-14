# Muse 乐器电平校准(参考,未采纳)

来源:2026-11-14 回滚的 `stash`(CalibratedMix 代理的产物),基于 2026-11-13 的 Muse 真实渲染(`parasailing`、`pm.musicxml`)的听感反馈:钢琴太小、弦乐过大、木管/手风琴偏弱。C++ 核心随后冻结为只合成,此实现按规则不得进入核心,故留作参考。

## 文件

| 文件 | 说明 |
|---|---|
| `calibrate.{hpp,cpp}` | `cgmusic calibrate --engine muse`:对每个可选的 Muse 乐器和 FluidSynth 回退乐器渲染同一标准测试乐句(固定音区、mf 力度、固定时长),测量响度,与目标电平比较得出增益,可输出 JSON 或生成 `levels.cpp` |
| `levels.{hpp,cpp}` | 生成产物:每个乐器的校准增益表,文件头记录目标电平的文献依据 |
| `integration.patch` | 当时对 `muse/render.cpp`、`main.cpp`、`render.hpp` 的接线(`mix_hybrid` 应用校准表、`calibrate` 子命令),基于旧 C++ 路径 |

## 方法(可迁移的部分)

1. 目标电平来自声学文献,而非针对某首曲子的手调:J. Meyer《Acoustics and the Performance of Music》,Fletcher & Rossing《The Physics of Musical Instruments》,以及非相干功率叠加 `10·log10(N)` 的编制平衡约定。
2. 测量:每个乐器用同一测试乐句渲染干声,取积分响度,增益 = 目标 − 实测,表驱动,不在渲染时再做自动均衡。
3. 弱记号(p/pp)声部不抬增益;增益设上限。

## 未验证与已知问题

- 校准表是在 2026-11-13 的库版本(0.106.0)和 `home-run3` 授权状态下生成的;乐器被授权拒绝(鼓组、打击乐)的条目无效。
- 听感没有人确认;`CalibratedMix` 被中止,未产出 v5 mp3。
- 目标电平对 Muse 样本的适用性只做过文献推断,没有对照真实录音。
- 旧 C++ 路径已改名为 `sandbox/tools/cgsynth`,`integration.patch` 不能直接 `git apply`。

## 若要采纳

在 `cgmaster`(后处理)里做:把校准表放进 Python(JSON),`cgscore` 的 `plan.json` 的 `mix.gain_db` 或 `cgmaster` 的声部增益读取它;校准程序本身用 `cgsynth render` 渲染测试乐句 + `pyloudnorm` 测量,不要在 C++ 里加子命令。
