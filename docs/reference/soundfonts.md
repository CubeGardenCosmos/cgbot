# SoundFont 音色库

更新日期:2026-11-12。下载脚本、只读挂载接线与启动时后台下载默认音色已实现并有离线测试;真实沙盒内 `cgmusic` 合成与 192.168.1.19 部署验证见 [R5.8](../todos/R5.8-music-synth.md)。

代码位置:`src/cgbot/music/soundfont.py`(目录、校验、`list_presets`)、`scripts/fetch_soundfonts.py`(命令行)。

## 目录

共 9 个条目,`*` 为推荐默认(`recommended`,只有一个)。大小与 sha256 均来自 2026-11-12 对真实下载文件的计算(下载经 `scripts/fetch_soundfonts.py --preset all` 走目录 URL 完成,`--verify` 9/9 通过;临时文件已删除)。

| id | 名称 | 格式 | 大小(字节) | 许可证 | 标签 |
| --- | --- | --- | --- | --- | --- |
| `musescore-general` * | MuseScore General | SF3 | 39,900,972(38.1 MiB) | MIT | general |
| `musescore-general-lite` | MuseScore General Lite | SF3 | 39,978,561(38.1 MiB) | MIT | general, lite |
| `musescore-general-full` | MuseScore General Full | SF3 | 85,967,114(82.0 MiB) | MIT | general |
| `fluidr3mono-gm` | FluidR3 Mono GM | SF3 | 23,614,353(22.5 MiB) | MIT | general, lite |
| `generaluser-gs` | GeneralUser GS 2.0.3 | SF2 | 32,319,396(30.8 MiB) | GeneralUser GS License v2.0 | general, keys, piano, epiano |
| `masterpiece-gm` | Masterpiece GM | SF2 | 29,208,234(27.9 MiB) | CC0 1.0(rKhive 声明) | general, keys, piano |
| `dx7-rom1a` | Yamaha DX7 ROM 1A | SF2 | 18,219,796(17.4 MiB) | Unlicense(仓库声明) | fm, epiano |
| `neo-orchestra` | Neo Orchestra Realistic | SF2 | 13,886,634(13.2 MiB) | CC0 1.0(仓库声明) | orchestra, chamber, general |
| `timgm6mb` | TimGM6mb | SF2 | 5,969,788(5.7 MiB) | GPL-2.0 | general, lite |

sha256:

| id | sha256 |
| --- | --- |
| `musescore-general` | `5b85b6c2c61d10b2b91cddd41efcce7b25cd31c8271d511c73afafbef20b6fa3` |
| `musescore-general-lite` | `916aaca6b0eb9f9083eb42399614acb2ff4ec72ce28dffff1814592a9696b479` |
| `musescore-general-full` | `f9a37b36655f23e167c66ec91bef6097c8e99be29286219a54062919eb41aeae` |
| `fluidr3mono-gm` | `cda013d8c370a48ae8dad271e761078d2e77455488dabdedbfbe5fc76a38c682` |
| `generaluser-gs` | `9575028c7a1f589f5770fccc8cff2734566af40cd26ed836944e9a5152688cfe` |
| `masterpiece-gm` | `52d854c93853ec97380af351595fdcce0571242763b7879fd96caac6f86aaf79` |
| `dx7-rom1a` | `9a725f89866e1e4c8b43542493d5a515adb2f13f69166d258fba3cc122cce708` |
| `neo-orchestra` | `4ab9c03ebdbdccde19b72531be27c02cb39df8c05b4bdc132ebaf54d16173586` |
| `timgm6mb` | `c5378b62028c920cb11e4803327983fee2f2cdff5dc89c708e39da417e51c854` |

标签含义:`keys`/`piano`/`epiano` 键盘,`fm` DX7 类 FM 音色,`orchestra`/`chamber` 管弦与室内乐,`general` 完整 GM,`lite` 体积小。

### 来源与预设抽样(`list_presets` 读取 phdr 的结果)

| id | 来源(URL 固定到提交 SHA 或镜像) | 预设数 | 抽样预设名(bank/program) |
| --- | --- | --- | --- |
| `musescore-general` 系列 | `ftp.osuosl.org`、`people.debian.org/~tg/sf/` | 311(Lite/Full) | Cello(0/42)、Contrabass(0/43)、Detuned FM EP(8/5)、Halo Pad(0/94) |
| `fluidr3mono-gm` | `people.debian.org/~tg/sf/` | 197 | Alto Sax(0/65)、Harp(0/46)、Harmonica(0/22)、Sitar(0/104) |
| `generaluser-gs` | `raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/684543d…/GeneralUser-GS.sf2` | 287 | Grand Piano(0/0)、Tine Electric Piano(0/4)、FM Electric Piano(0/5)、Harpsichord(0/6)、Clavinet(0/7) |
| `masterpiece-gm` | `bratpeki/soundfonts@e1dcd9d`(`SF2/GM/Masterpiece.sf2`,原始来源 rKhive) | 137 | Yamaha 9ft Grand(0/0)、E.Piano 1(0/4)、Solo Violin Room(0/40)、Solo Cello Room(0/42)、Organ w/Pedals(0/19) |
| `dx7-rom1a` | `Caskexe/DX@895a568`(`DX7/Factory ROM/Yamaha DX7 ROM 1A.sf2`) | 32 | E.PIANO 1、BASS 1、TUB BELLS、MARIMBA、HARPSICH 1 |
| `neo-orchestra` | `NeoSoundFonts/neo-orchestra-hq-realistic-sf2@7aad23d` | 121 | VSCO2CE Violin(0/40)、VSCO2CE Strings(0/48)、VSCO2CE Oboe(0/68)、Petrof Grand(0/0)、Dexed FM EP(0/5) |
| `timgm6mb` | `bratpeki/soundfonts@e1dcd9d`(`SF2/GM/TimGM.sf2`) | 136 | Flute TB(0/73)、Gun Shot(0/127)、Applause(0/126)、Orchestra(128/48 鼓组) |

- SF2 条目由 GitHub raw 提供(HTTP 200,`Accept-Ranges: bytes`,Content-Length 与目录一致)。SF3 条目沿用 2026-11-12 对 OSUOSL/Debian 镜像的 Range 请求结果。
- `dx7-rom1a` 是 DX7 出厂 ROM1 A 组;DX9 的音色是 DX7 的子集,该仓库另含 DX7 ROM 1B–4B、DX7S、DX7 II、TX7 等 SF2(未加入,按需追加条目即可)。
- 体积最小的室内乐/弦乐方案是 `neo-orchestra`(13 MiB);它不是纯室内乐音色,而是带 VSCO-2 CE 弦、木管、铜管的紧凑 GM。

## 许可证与证据

- **MuseScore General / Lite / Full**:MIT。样本中含 CC0 或公有领域素材。原文见 OSUOSL 镜像的 `MuseScore_General_License.md` 与 Debian 镜像的 `*.copyright`。
- **FluidR3 Mono GM**:MIT,版权归 Michael Cowgill 与 Frank Wen。原文见 `FluidR3Mono_GM.copyright`。
- **GeneralUser GS**:自有许可证 v2.0(`documentation/LICENSE.txt`,2026-11-12 读取)。允许私用与商用,允许在软件项目中使用并修改音色库与包装;要求若在自己网站展示不要直链作者下载文件。**注意**:作者声明部分样本来自互联网上的免费音色,出处无法 100% 确认,但从未有版权投诉。仓库只从 GitHub 镜像下载,不再分发。
- **Masterpiece GM**:rKhive 站点 legal 页声明 CC0 1.0。样本出处(如 Yamaha 9ft Grand)未独立核实,按声明使用。**[推论]** 若严格要求可核实的 CC0 来源,不应启用。
- **DX7 ROM 1A**:仓库 `Caskexe/DX` 的 `LICENSE` 为 The Unlicense(GitHub API 亦识别为 `unlicense`)。原文已读取。音色来自 Yamaha 出厂 patch 的 SYSEX,经软件渲染为采样;patch 本身的版权状况未核实,按仓库声明使用。
- **Neo Orchestra Realistic**:仓库 `LICENSE` 为 CC0 1.0。README 列出的来源包括 VCSL、VSCO-2 CE(CC0)、FreePats、Karoryfer 及若干作者声明"免费可商用"的 VSTi 采样(DSK 等),混合来源未逐项核实。**[推论]** 与上同。
- **TimGM6mb**:GPL-2.0(MuseScore 手册与 `bratpeki/soundfonts` 的 LICENCES 标注)。
- 再分发时须保留各自版权与致谢声明。仓库只下载、不分发音色文件。

## 已调研但未加入

| 候选 | 原因 |
| --- | --- |
| Salamander Grand Piano(CC-BY 3.0,现为公有领域) | 官方只有 SFZ 与 296 MiB 的 SF2 `.tar.xz` 归档(FreePats);目录只处理单文件下载,不做解压或转换,故不加入。想用请自行放入目录(`.sf2`/`.sf3` 即可识别)。 |
| FreePats Upright Piano KW(CC0 1.0) / YDP Grand Piano(CC-BY 3.0) | 仅以 `.7z`/`.tar.bz2` 归档发布(`UprightPianoKW-small` 归档 6,093,984 字节,解出 SF2 9,456,310 字节)。同上,不加入。 |
| VSCO 2 Community Edition / Versilian VCSL(CC0) | 官方只有 SFZ+WAV(GitHub `sgossner/VSCO-2-CE`、`sgossner/VCSL`),无 SF2/SF3,跳过。其样本已部分包含在 `neo-orchestra` 中。 |
| Sonatina Symphonic Orchestra(CC Sampling Plus 1.0) | 官方只有 SFZ+WAV(`peastman/sso`);社区 SF2 在 Musical Artifacts。许可证"Sampling Plus"禁止广告用途且需要署名,不属于干净的自由许可,不加入。 |
| Aegean Symphonic Orchestra | 作者声明仅限个人使用,禁止出售或重新打包;衍生自多个上游,再分发约束复杂。拒绝。 |
| Arachno SoundFont | 作者许可为仅限个人、非商业使用,不允许再分发;官方文件需经作者网站下载页。拒绝。 |
| SGM-V2.01 | 作者要求署名;社区常标为 CC-BY,但内部样本来自商业合成器与采样库,无逐项授权。出处不清,拒绝。 |
| Musyng Kite | 作者声明不得作为商业音色分发;文件需 Musical Artifacts 或 KVR 论坛下载,且衍生自第三方样本。拒绝。 |
| Chamberlin | 未找到许可证允许再分发的单文件 SF2/SF3 来源,跳过。 |
| Rhodes / Wurlitzer / Clavinet 专用字体(jRhodes3d、Crystal Rhodes 等) | jRhodes3d 官方为 SFZ+FLAC;SF2 版本托管于 Musical Artifacts/Polyphone,前者被 Cloudflare 拦截(HTTP 403,需人机验证,无法程序化获取),后者无法确认直链。跳过。GeneralUser GS 与 Masterpiece 的 GM 电钢、大键琴、击弦古钢琴已覆盖这些音色的基本需求。 |
| Musical Artifacts 全站 | 页面与下载均受 Cloudflare 保护(`HTTP 403`),需验证码,跳过。 |
| FluidR3 Mono(beicause 镜像) | 大小(14,540,067 字节)与 Debian 构建不一致,未加入。 |
| marmooo/free-soundfonts(Cloudflare Pages) | 按乐器拆成每个 ≤10 MB 的 SF3 文件(`<name>/<bank>/<program>.sf3`),不是单个完整音色库;不加入。 |

SF2 体积评估:目录内最大的 SF2 是 GeneralUser GS(30.8 MiB),都不需要主机侧转换。Salamander 296 MiB 的 SF2 若要放进沙箱,转换为 SF3 可显著缩小,但本仓库不提供转换工具(按用户指示)。

## 格式校验

- `SoundfontStore` 与脚本接受 `.sf3` 与 `.sf2`。校验:大小在 1 MiB–600 MiB 之间;RIFF 头为 `RIFF`+`sfbk`,RIFF 大小与文件长度一致;存在 `LIST`/`sdta` 且首块为非空 `smpl`。`smpl` 以 `OggS` 开头时必须含 Vorbis 标识(即 SF3),否则按 SF2 的 16 位 PCM 接受。
- 部分 SF3 写入器省略奇数长度块的补位字节,读取器按下一块头是否合法兼容。
- `list_presets(path)`:纯 Python 读取 `pdta/phdr`,返回 `PresetHeader(name, bank, program)` 列表(不含终止记录 EOP),用于诊断与测试,无第三方依赖。

## 运行

在仓库根目录执行:

```
uv run python scripts/fetch_soundfonts.py --list
uv run python scripts/fetch_soundfonts.py
uv run python scripts/fetch_soundfonts.py --preset all
uv run python scripts/fetch_soundfonts.py --preset keys,fm,chamber
uv run python scripts/fetch_soundfonts.py --preset musescore-general-lite,fluidr3mono-gm
uv run python scripts/fetch_soundfonts.py --verify
uv run python scripts/fetch_soundfonts.py --print-mount
```

- `--dir PATH`:音色目录,默认 `./data/soundfonts`。
- `--list`:列出 id、名称、大小、许可证(超长截断)、标签与是否已安装,不下载。
- `--preset`:逗号分隔的记号,可混用。`recommended`(默认,即 `musescore-general`)、`all`、标签(`keys`、`piano`、`epiano`、`fm`、`orchestra`、`chamber`、`general`、`lite`)或音色 id。记号先按 `all`/`recommended`、再按 id、最后按标签解析;未知记号为用法错误(退出码 2)。结果去重并保持顺序。
- `--verify`:重新校验已安装的文件,不下载。未指定 `--preset` 时校验目录中全部已安装的目录条目。
- `--print-mount`:打印 `-v <绝对目录>:/opt/soundfonts:ro` 后退出。
- 进度条写入 stderr。下载文件权限为 `0644`,只读挂载可直接读取。
- 已安装且通过校验的条目不会重复下载;未完成的 `.part` 文件从断点续传。校验失败时删除 `.part`,并以非零退出码结束。
- 用户自行放入目录的 `.sf2`/`.sf3`(扩展名大小写不敏感)会出现在 `list()` 中,id 为文件名去扩展名。

## 挂载

音色目录由 `[music] soundfont_dir`(宿主机路径)以只读方式绑定到每个 Codex 沙盒的 `/opt/soundfonts`(`HostConfig.Binds` 条目 `<目录>:/opt/soundfonts:ro`,实现于 `src/cgbot/coding/sandbox.py`)。

- 仅当 `[music] enabled = true` 且该目录存在时才挂载;沙盒不写入、不下载。目录内容变化(新增 `.sf3`/`.sf2`)无需重建容器,绑定挂载直接可见;但"是否挂载"进入沙盒配置哈希,首次启用后空闲沙盒会在下次使用时自动重建。
- 默认值:`[coding.docker] host_data_dir` 下的 `soundfonts/`(无 `[coding]` 时为 `data_dir/soundfonts`);相对路径按配置文件目录解析。该路径是 **宿主机 Docker 守护进程解析的路径**,而 bot 运行在容器里:`deploy/docker-compose.yml` 已把 `${CGBOT_HOST_DATA_DIR}` 以相同路径挂入 bot 容器(读写),所以 bot 写入的音色目录与沙盒读到的是同一宿主机目录。
- bot 启动时(`[music] auto_download_soundfont = true`)后台调用 `SoundfontStore.ensure()` 确保默认音色 `musescore-general`,失败只记 WARNING 日志,不影响启动。bot 以 root 运行,目录创建为 0755、文件 0644,沙盒 uid 1000 可读。
- 192.168.1.19 上默认路径为 `$CGBOT_HOST_DATA_DIR/soundfonts`(未设置 `CGBOT_HOST_DATA_DIR` 时 `deploy.sh` 取远端 `$HOME/cgbot-data`,即 `$HOME/cgbot-data/soundfonts`)。也可以手工放入其它 `.sf3`/`.sf2`,`cgmusic soundfonts` 以小写文件名主干作为 id。
- `scripts/fetch_soundfonts.py --print-mount` 仍可打印等价的 `-v` 参数,用于手工 `docker run` 调试。
