# cgmidi

沙箱内的 MIDI 预处理 CLI。在 `cgmusic` 渲染前调整乐谱:速度、移调、通道/音色/音库映射、静音与独奏、按秒裁剪。底层使用 [mido](https://github.com/mido/mido)(MIT,纯 Python)读写 MIDI,本包只做变换逻辑,不重新实现 MIDI 解析或写出。所有输出为 JSON(成功写 stdout,失败写 stderr 并以非零码退出)。

## 命令

```sh
cgmidi info <输入.mid>                                        # 摘要:速度、时长、通道、音色、音库、音域
cgmidi process <输入.mid> -o <输出.mid> [选项...]             # 预处理并写出新文件
cgmidi hands <输入.mid> -o <输出.mid> [选项...]               # 合并同通道同音色多轨力度基线并保留重音
cgmidi bars <输入.mid> [--channel CH]...                     # 小节划分与和声音高类分析(只读 JSON)
cgmidi pedal <输入.mid> -o <输出.mid> [--mode M] [选项...]    # 延音踏板 CC64 生成与替换
```
`process` 选项:

| 选项 | 说明 |
| --- | --- |
| `--speed F` | 速度倍率,范围 0.1–10。`1.25` 加快 25%,`0.5` 减半。与 `--bpm` 互斥 |
| `--bpm B` | 用固定 BPM(1–999)替换全部速度标记。与 `--speed` 互斥 |
| `--transpose N` | 移调半音数,范围 -48 到 48。鼓组通道 10 不移调 |
| `--mute CH` | 静音通道 1–16,可重复 |
| `--solo CH` | 独奏通道 1–16,可重复。指定后只保留这些通道的音符 |
| `--map-channel SRC:DST` | 通道改派,如 `2:3`,可重复 |
| `--set-program CH=PROG` | 强制通道音色(0–127),可重复 |
| `--set-bank CH=MSB[:LSB]` | 强制通道音库(CC0 / CC32),LSB 默认 0,可重复 |
| `--map-program SRC=DST` | 音色号映射(0–127),作用于所有通道,可重复 |
| `--start S` | 裁剪起点(秒,按原乐谱时间) |
| `--end E` | 裁剪终点(秒,按原乐谱时间) |

`hands` 选项:

| 选项 | 说明 |
| --- | --- |
| `-o, --output OUT` | 输出 .mid 文件路径(必选) |
| `--channel CH` | 只合并指定通道 1–16,可重复;默认所有同通道且同音色的多轨 |
| `--window-beats F` | 滑动中值窗口半径(拍),默认 2.0 拍 |
| `--accent-gap N` | 判定为独立重音的最小力度差(0–127),默认 12 |

`bars` 选项:

| 选项 | 说明 |
| --- | --- |
| `--channel CH` | 仅分析指定通道 1–16,可重复;默认全部非鼓通道 |

`pedal` 选项:

| 选项 | 说明 |
| --- | --- |
| `-o, --output OUT` | 输出 .mid 文件路径(必选) |
| `--mode M` | 换踏板模式: `bar`(每小节换)、`chord`(每和声段换)、`auto`(琶音织体按和声换,其余按小节换,默认) |
| `--channel CH` | 目标通道 1–16,可重复;默认自动识别 GM 键盘通道(0–7)或音符最多的非鼓通道 |
| `--replace` | 替换已有 CC64 事件;未指定时若目标通道已存在踏板事件,则不修改并返回 `already_pedaled` |
| `--gap-ticks N` | 换踏板时边界释放与再按下的偏移 tick,默认 1/16 拍 |

- 输入文件只读,不会被修改。输出路径不能与输入相同。
- 输出采用临时文件 + fsync + 原子替换写入。

成功示例(stdout 单行 JSON):

```json
{"output": "/workspace/artifacts/music/out.mid", "duration_seconds": 61.25, "notes": 1842}
```

`info` 示例(节选):

```json
{"format": 1, "ticks_per_beat": 480, "duration_seconds": 61.25, "notes": 1842,
 "tempo": [{"seconds": 0.0, "bpm": 120.0}],
 "channels": [{"channel": 1, "notes": 920, "note_range": [40, 84], "programs": [0], "banks": [0]}],
 "tracks": [{"index": 0, "name": "Piano", "notes": 920}]}
```

失败示例(stderr):

```json
{"error": "out_of_range", "message": "移调后音高超出 0-127(通道 1,tick 960,原音高 120,移调 12)"}
```

## 通道与数值约定

- 命令行通道号为 **1–16**,与乐谱软件一致。内部与 mido 使用 **0–15**,转换只发生在 `cli.py`。`info` 输出同样为 1–16。
- 音色与音库值为 **0–127**。

## 语义

- **作用范围以源通道为准。** `--mute`、`--solo`、`--set-program`、`--set-bank` 引用的都是输入文件中的通道。`--map-channel` 最后应用,因此不会因为改派而误伤其他通道。
- **鼓组通道不移调。** 通道 10(索引 9)按 GM 惯例跳过 `--transpose`。
- **移调越界直接报错。** 音高超出 0–127 时返回 `out_of_range`,不做静默截断。
- **裁剪时间为原乐谱秒数。** `--start`/`--end` 按输入文件的速度标记换算为 tick,速度调整(`--speed`/`--bpm`)在裁剪之后应用,因此裁剪点不随速度改变。
- **裁剪保留声部状态。**
  - 起点之前的非音符事件(音色、控制器、速度标记等)折叠到 0,使起点处的音色与控制器状态保持正确。
  - 起点时仍在发声的音符在 0 重新开始。
  - 终点时仍在发声的音符补写 note_off,不留悬空音符。
  - 裁剪超出乐谱时长时报 `bad_input`。
- **强制音色与音库为覆盖语义。** `--set-program` 与 `--set-bank` 会删除该通道原有的全部对应消息,再在 tick 0 写入(先 CC0、CC32,后 Program Change)。
- **速度标记的缩放。** `--speed` 缩放每个速度标记;若开头没有速度标记,则显式写入缩放后的默认速度(120 BPM)。缩放后超出 MIDI 可表示范围时报 `out_of_range`。
- **格式 2(异步多轨)不支持。** 返回 `unsupported_format`。
- **两手力度合并(hands)。**
  - MuseScore 导出常将左手与右手分为不同 Track 但共享同一通道与音色,左右手分别带有独立力度记号(如右手 33,左手 64)。
  - 算法在 tick 域按 `--window-beats` 计算各轨的滑动中值作为音轨基线,声部基线取同一时刻各轨基线的最大值。
  - 与自身基线相差 `< accent_gap` 的音符对齐到声部基线;差值大于等于 12 的重音按相对差值叠加在声部基线之上,完整保留音乐重音与力度层次。
- **小节划分与和声分析(bars)。**
  - 自动解析时间线中的 tempo 标记与 time_signature 标记,按拍号分割小节,精准识别首小节弱起(pickup/anacrusis)。
  - 在小节内部以拍为观察窗提取发声的音高类集合(pitch-class sets),识别伴奏低音与和弦结构,为 LLM 与后处理提供结构化乐理表示。
- **延音踏板生成(pedal)。**
  - 在小节或和声交界前提前释放(`CC64=0`),并在交界处或略后重新踩下(`CC64=127`),防止换和声时浑浊。
  - 绝不修改音符时长、音高与力度;默认保护原谱已有踏板(`--replace` 显式允许覆盖)。

## 库选型说明

- **mido (>=1.3)**:
  - 纯 Python 编写,无原生/C 编译依赖,跨平台稳定。
  - 原生支持标准 MIDI 格式 0/1 的流式解析与写出,精准表达 tick delta 与 Meta 事件。
  - 针对音符与控制器事件的变换开销极低,满足沙箱快速预处理需求。

## 错误码

| 码 | 含义 | 退出码 |
| --- | --- | --- |
| `bad_input` | 文件不存在、参数非法或格式不对、输出与输入相同、裁剪点超出时长、输出目录不存在 | 1 |
| `parse_failed` | 输入不是可解析的 MIDI 文件 | 1 |
| `unsupported_format` | MIDI 格式 2 | 1 |
| `out_of_range` | 移调后音高越界,或速度缩放后超出范围 | 1 |
| `write_failed` | 写出失败 | 1 |

argparse 的用法错误(如缺少 `-o`、未知选项)同样以 `bad_input` JSON 写入 stderr,退出码为 2。

## 安装

开发:

```sh
cd sandbox/tools/cgmidi
uv sync
```

镜像内(建议的 Dockerfile 块,与 cgdl 相同模式,待 `sandbox/Dockerfile` 合入):

```dockerfile
COPY tools/cgmidi /opt/cgbot/tools/cgmidi
RUN python3 -m pip install --no-cache-dir --no-deps /opt/cgbot/tools/cgmidi \
    && ln -sf "$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cgmidi" /usr/local/bin/cgmidi \
    && cgmidi --help >/dev/null
```

`mido` 需由 `sandbox/requirements.txt` 安装到默认 Python(与 `--no-deps` 配合),当前锁定版本为 `mido==1.3.3`。

## 开发与测试

```sh
cd sandbox/tools/cgmidi
uv run pytest -q                    # 离线单元测试
uv run ruff check src tests
uv run pyright
uv run pytest -m realfiles -q       # 非门控:用 /tmp/cgtest/*.mid 做 info 与 speed 检查
```

- 单元测试使用内存构造的 MIDI(`tests/midi_helpers.py`),不访问网络,也不依赖外部文件。
- `realfiles` 标记默认不运行(`pyproject.toml` 中 `addopts` 排除)。该组测试读取 `/tmp/cgtest/*.mid`,不存在时跳过。

## 目录

```
src/cgmidi/
  cli.py      命令行、参数解析、JSON 输出
  ops.py      纯内存变换与摘要(transform、describe)
  files.py    读取校验、原子写出
  errors.py   错误码与 CgmidiError
tests/
  midi_helpers.py  测试用 MIDI 构造
  test_ops.py      变换语义
  test_cli.py      参数、退出码、错误码
  test_realfiles.py 真实样本(非门控)
```
