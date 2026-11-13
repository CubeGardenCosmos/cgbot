# muse-setup 参考(一次性 Muse Sounds 初始化容器)

更新日期:2026-11-13。位置:`tools/muse-setup/`(Dockerfile、入口、驱动、浏览器 shim、machine-id 辅助脚本、README)。上下文:[MuseSampler 参考](musesampler.md) §13–§19。

## 1. 是什么、为什么

渲染沙盒需要库(`libMuseSamplerCoreLib.so`)、样本、Manager 写出的 `.receipt` 和固定 machine-id。这些只能由官方 Muse Sounds Manager 产生。`muse-setup` 是用户按需运行一次的容器:无头运行官方 Manager,驱动它自己的界面下载并写 receipt,打印摘要后退出。它**不属于 bot 镜像**;之后 Manager 不再运行。

约束(用户裁定):不破解、不钩子、不注入、不伪造或编辑 receipt/令牌;只驱动官方 UI;不烤入任何库/样本/状态;不打印密钥。

## 2. 调研结论

### 2.1 界面工具包与无障碍 [已验证]

- Manager 是 Avalonia 12.0.4 + .NET 10 NativeAOT,Skia/X11 渲染(不是 Qt/Chromium/Electron)。登录与下载在原生库 `libServiceCore.so`。
- Avalonia 自己启动 AT-SPI 服务,`QT_ACCESSIBILITY`/`GTK_MODULES`/`NO_AT_BRIDGE` 无作用。实测:Manager 连上无障碍总线并暴露应用根 "MuseSounds Manager",但根下 0 个子节点,registry 的 desktop 也是 0。尝试过设置 `org.a11y.Status.IsEnabled`、注册事件监听、聚焦窗口、悬停/Tab、等待——均无变化。**控件无法经 AT-SPI 按名称取得。**
- 回退:用 tesseract 对截图做词框识别,**按文字标签**(`Download`、包名)在运行时定位,每次点击前重新截图识别,找不到即非零退出。没有写死的坐标。这不是模板匹配,也不依赖固定布局。局限:OCR 可能读错字;界面改版会导致找不到标签而失败(而不是误点)。

### 2.2 登录 [已验证:Linux 无登录路径]

- 界面只有 11 个包卡片(每个一个 `Download`)和右上角齿轮;设置对话框含 Updates、Language、安装位置、自动更新、Clear cache、日志、致谢/支持/隐私、版本,**没有登录项**。
- 二进制里有 `AccountClickedCommand`/`DisplayAccountButton`/`SignInToContinue`,说明账号按钮存在但在此环境不显示(看起来由功能开关控制,本任务不尝试让它出现)。
- 对全部 11 个包逐一点击 `Download`(未登录、新 /state、shim 已安装):每次都直接进入 "Preparing to download library",**没有调用浏览器,没有登录链接,没有登录对话框**。
- 回调侧(仅字符串 [推论]):`libServiceCore.so` 内含 OAuth2+PKCE(Azure AD B2C)、`redirect_uri`、`127.0.0.1` 与内嵌 "Welcome Back" 页面,即回环 HTTP 监听。从未被触发,端口与浏览器启动方式**未验证**。
- 因此原先"点 Download 触发登录并抓链接"的方案不成立。浏览器 shim 仍保留作安全网(打印 `==> OPEN THIS LINK IN YOUR BROWSER: <url>` 并写 `/state/login-url.txt`),但**没有任何真实触发验证**。

### 2.3 许可数据来源(用户裁定)

§18/§20:真正让重新生成的 receipt 接受各包的是 Windows Muse Hub 的许可/资料缓存(`.licensecache_v2`、`.profilecache` 等),不是 `.authconf`。容器因此让用户只读挂载 Windows Muse Hub 数据目录,驱动只复制白名单可移植文件(`.profilecache .licensecache_v2 .config2 .bdconfig .dlcache .rccache .session_id .unified_id .sysidcache`,权限 600,不打印内容),再由 **Manager 自己重写 receipt**。用户需在 Windows 上已登录并最近打开过 Muse Hub(登录过期可能使许可失效:用户猜测,未验证)。

## 3. 驱动行为

状态机,每步有显式等待与超时,失败即非零退出并说明:导入状态 → 启动 Manager(Xvfb)→ 等窗口 + OCR 见到包名(3/4)→ 等 `.receipt` 相对启动前 mtime/大小/哈希变化(6)→ 等 Manager 自己发起的后台下载结束 → 对每个 `Download` 标签点击并等 Manager 日志 State 离开 Downloading(5/7/8)→ receipt 稳定 20 秒 → 摘要(包状态、样本根与磁盘占用、receipt 大小、GUID 样式字符串数量、库路径与 sha256,库复制到 `/out`)→ 退出 0。

注意:
- `.receipt` 在 Manager 刚启动(未登录)时就会生成(约 2.4 KB),所以"receipt 存在"不是完成信号,只有"相对启动前变化"和"稳定"有意义。
- Manager 的 `Installed` 来自其缓存文件,从 Windows 复制缓存后可能与磁盘不符;从 Windows 状态启动时 Manager 还会自己在后台续传(实测 Percussion),所以驱动先等这些下载结束。摘要打印磁盘占用供核对。
- 幂等:已安装的包界面不再显示 `Download`,不会点击。
- 免费包假设:界面不显示价格,驱动下载它看得到的全部包(Manager 的 Muse Sounds 免费库)。README 说明用户如何核对,并提供 `MUSE_SETUP_ONLY` 限定。

## 4. 测试记录

已真实执行(容器 `muse-setup:dev`,构建成功,uid 1000,machine-id 只读挂载,HOME=/state):

- 镜像构建;入口、Xvfb、dbus 会话、Manager 启动、窗口出现。
- OCR 对 11 个包按标签定位 `Download` 与包名(含截断的 "Muse Guitars Vo...")。
- 全部 11 个包逐一点击 `Download`:无浏览器调用、无登录链接(见 2.2)。
- 匿名模式驱动路径:警告、receipt 重写检测、按标签点击、日志等待(见下方"匿名运行结果")。
- Windows 状态导入路径:用 `home-run3` 的**副本**(只含白名单文件)作为 `/winhub`:复制 8 个文件(权限 600),Manager 写出 5596 字节 receipt,含 16 个 GUID 样式字符串(与 §18 一致),报告 8 个包 Installed;驱动退出 0。
- `ensure-machine-id.sh`:创建、幂等不覆盖。
- 容器与临时目录已清理(镜像保留)。

未验证:
- 登录(Linux 无入口);浏览器 shim 被 Manager 真实触发;OAuth 回调与 `--network host`。
- 全部包下载到完成(只下载了 Handbells 做验证);全量下载的耗时与磁盘需求。
- 以本容器产出的 receipt 在渲染沙盒里 `ms_init_3`/乐器数(§18 的流程另有记录)。
- Windows 登录过期导致许可失效的猜测。
- `.sysidcache` 的导入(测试副本里没有该文件)。

## 5. 限制

- OCR 依赖界面文字与分辨率(Xvfb 1280×900);Manager 改版可能需要调整。
- 官方 DEB 地址不含版本号,上游更新后构建的 sha256 校验会失败(设计如此)。
- 渲染沙盒的挂载接线(库、样本、状态、machine-id 只读)属部署变更,未在此做。
