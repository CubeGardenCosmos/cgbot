# muse-setup

一次性、按需运行的 Muse Sounds 初始化容器。**不属于 bot 镜像**,不进入部署栈。它驱动官方 Muse Sounds Manager(Linux 版,无头 Xvfb),让 Manager 自己下载样本、安装 MuseSampler 库、写出 `.receipt`,然后退出。之后 Manager 不再运行;渲染沙盒只只读挂载库、样本、状态和固定的 machine-id。

## 先读这几条(重要限制)

1. **Linux 版 Manager 没有登录控件。** 界面只有每个包的 `Download` 按钮和右上角设置齿轮;设置里没有登录项,点 Download 也不会弹登录或调用浏览器(已实测,见 `docs/reference/muse-setup.md`)。所以**无法在容器里登录**。
2. **许可数据来自你自己 Windows 上的 Muse Hub。** 容器把你挂载进来的 Windows Muse Hub 数据目录里的可移植状态文件(许可/资料缓存等,清单见下)复制进 `/state`,由 Manager 自己据此重写 `.receipt`。因此你必须在 Windows 上登录过 Muse Hub,且**最近打开过它**(登录过期可能导致许可失效,这是猜测,未验证)。
3. **没有挂载 `/winhub` 就是匿名模式**:能下载,但 receipt 不含任何音色包许可,渲染时所有 Muse 乐器都会被拒绝。驱动会大声警告。
4. **"免费包"的假设**:界面不显示价格,驱动下载界面上看得到的全部 Muse Sounds 包(Manager 的 Muse Sounds 免费库,共约 11 个)。请自己核对:Manager 里列出的包应与你在 musehub.com 的免费 Muse Sounds 一致;可用 `-e MUSE_SETUP_ONLY=Muse Harp,Muse Keys` 限定只下载某几个。
5. 没有任何专有内容被烤进镜像:镜像只含官方 DEB(构建时下载并校验 sha256)和我们的脚本。

## 准备:持久的 machine-id

receipt 绑定 `/etc/machine-id`。同一个文件必须只读挂载到 setup 容器**和**以后所有渲染容器,并且生成 receipt 后不能再换。

```bash
tools/muse-setup/ensure-machine-id.sh            # 默认 ~/.local/share/cgbot/muse/machine-id;已存在则不动
tools/muse-setup/ensure-machine-id.sh /path/to/machine-id
```

请备份该文件。

## 构建

```bash
docker build -t muse-setup tools/muse-setup
```

- 基础镜像 `debian:bookworm` 固定到摘要;apt 使用 `mirrors.aliyun.com`(与仓库其他 Dockerfile 相同的网络策略,可用 `--build-arg DEBIAN_MIRROR=deb.debian.org` 覆盖)。
- Manager 来自官方下载页 <https://support.musehub.com/en/articles/15070606-downloads>,构建时下载 `https://muse-cdn.com/Muse_Sounds_Manager_x64.deb`(2.2.1.953)并校验 sha256(`53209132…2813`)。官方地址不带版本号:上游更新后校验会失败,而不是悄悄换版本;确认新版后用 `--build-arg MUSE_DEB_SHA256=...` 更新。DEB 不存入仓库。

## 运行

```bash
MID=~/.local/share/cgbot/muse/machine-id
WINHUB='/mnt/c/Users/<你的用户名>/AppData/Local/Muse Hub'
mkdir -p ~/muse/state ~/muse/samples ~/muse/out
sudo chown 1000:1000 ~/muse/state ~/muse/samples ~/muse/out   # 容器内用户 uid 1000

docker run --rm -it --name muse-setup \
  -v "$MID":/etc/machine-id:ro \
  -v ~/muse/state:/state \
  -v ~/muse/samples:/samples \
  -v ~/muse/out:/out \
  -v "$WINHUB":/winhub:ro \
  muse-setup
```

| 卷 | 内容 |
| --- | --- |
| `/etc/machine-id`(只读,必需) | 你的持久 machine-id |
| `/state` | muse 用户的 HOME:`.local/share/muse-sounds-manager/`(缓存、日志、`.receipt`)、`.local/share/MuseSampler/`(`.config`、`lib/libMuseSamplerCoreLib.so`) |
| `/samples` | 样本根(Manager 的下载位置)。实际样本在 `/samples/Muse Sounds/`,其中有 `.instruments` |
| `/out` | 可选:额外复制一份 `lib/libMuseSamplerCoreLib.so` |
| `/winhub`(只读,可选但几乎必需) | Windows 的 Muse Hub 数据目录 |

驱动只会从 `/winhub` 复制这些文件,权限 600,绝不打印内容:`.profilecache .licensecache_v2 .config2 .bdconfig .dlcache .rccache .session_id .unified_id .sysidcache`。`.authconf`、`.receipt`、`cache/` 等其余一律不碰。

可选环境变量:`MUSE_SETUP_ONLY`(逗号分隔包名前缀)、`MUSE_SETUP_NO_DOWNLOAD=1`、`MUSE_SETUP_UI_TIMEOUT`、`MUSE_SETUP_RECEIPT_TIMEOUT`、`MUSE_SETUP_STALL_TIMEOUT`(下载无进度超时,默认 900 秒)。

## 流程与退出码

1. 复制 Windows 状态(或警告进入匿名模式)。
2. 启动 Manager,等窗口与界面(OCR 读到包名)。退出码 3/4。
3. 等 Manager **自己重写 `.receipt`**(完成信号 = mtime/大小/哈希相对启动前变化;注意 receipt 在刚启动时就会生成,所以不是"下载完成"信号)。退出码 6。
4. 等 Manager 自己在后台发起的续传/更新结束;再对界面上每个 `Download` 按钮**按 OCR 识别的文字标签**定位并点击(每次点击前重新截图识别,从不使用写死的坐标;找不到就非零退出),等待 Manager 日志里的 State 回到非 Downloading。退出码 5/7/8。
5. 等 receipt 稳定,打印摘要(包状态、样本根、磁盘占用、receipt 大小与 GUID 样式字符串数量、库路径与 sha256),退出 0。

幂等:已安装的包 Manager 自己识别,界面不再显示 `Download`,驱动不会点击。注意 Manager 的 "Installed" 来自它的缓存文件,可能与磁盘不符(从 Windows 复制缓存后尤其如此),摘要会打印磁盘占用,请以 `.instruments` 和磁盘占用为准。

## 浏览器 shim(安全网)

镜像里 `xdg-open`、`x-www-browser`、`sensible-browser`、`gio` 等都指向 `muse-open-url`:如果 Manager 将来调用浏览器,链接会打印为

```
==> OPEN THIS LINK IN YOUR BROWSER: <url>
```

并保存到 `/state/login-url.txt`。当前版本从未触发过(Manager 无登录控件)。若某天出现并需要回调,OAuth 重定向到 `127.0.0.1` 的本地端口,需要 `--network host`;**这条路径未验证**。

## 产物位置

| 内容 | 位置 |
| --- | --- |
| 库 | `/state/.local/share/MuseSampler/lib/libMuseSamplerCoreLib.so`(和 `/out/lib/`) |
| receipt、日志 | `/state/.local/share/muse-sounds-manager/` |
| 样本 | `/samples/Muse Sounds/` |

## 渲染沙盒如何使用(只读)

渲染容器不运行 Manager,只只读挂载:库目录、`/samples/Muse Sounds`(作为样本根)、`/state/.local/share/muse-sounds-manager`(含 `.receipt`)、`MuseSampler/.config` 对应路径,以及**同一个** machine-id(`-v "$MID":/etc/machine-id:ro`)。具体接线属于部署变更,未在本任务中做。

## 安全

镜像、仓库、日志都不含令牌、Cookie、密码。Windows 状态文件只存在于你的 `/state` 卷(600 权限)。不要提交 `/state`、`/samples`、`/out`。
