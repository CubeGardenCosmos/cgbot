#!/usr/bin/env python3
"""muse-setup 驱动:只通过 Manager 自己的界面(按 OCR 读到的文字标签定位控件,从不硬编码坐标)
与它自己的日志/状态文件工作。不改库、不改 receipt、不伪造令牌、不注入进程。

阶段(任何一步失败都以非零退出码结束,并说明原因):
  1 导入 Windows Muse Hub 的可移植许可状态(只复制白名单文件,不打印内容)   退出码 2
  2 启动官方 Manager(headless Xvfb),等待界面出现                            退出码 3/4
  3 等待 Manager 自己重写 receipt(mtime/大小/哈希相对启动前发生变化)         退出码 6
  4 按 OCR 标签点击每个可见的 "Download",等待 Manager 日志报告 Installed      退出码 4/5/7/8
  5 再等 receipt 稳定,打印摘要,复制库到 /out,退出 0
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/state"))
MGR_DIR = HOME / ".local" / "share" / "muse-sounds-manager"
LOG = MGR_DIR / "logs" / "current.txt"
RECEIPT = MGR_DIR / ".receipt"
AUTHCONF = MGR_DIR / ".authconf"
SAMPLER_DIR = HOME / ".local" / "share" / "MuseSampler"
LIB = SAMPLER_DIR / "lib" / "libMuseSamplerCoreLib.so"
LOGIN_URL = HOME / "login-url.txt"
MANAGER_STDOUT = HOME / "manager-stdout.log"

# 只复制这些文件;其余(尤其 .authconf、.receipt、cache.db)永远不碰。
PORTABLE_FILES = (
    ".profilecache", ".licensecache_v2", ".config2", ".bdconfig", ".dlcache",
    ".rccache", ".session_id", ".unified_id", ".sysidcache",
)

PACK_RE = re.compile(
    r"Name: (?P<name>.+?), Item Type: Instrument, (?:Version: (?P<ver>\S+), )?State: (?P<state>\w+)"
    r"(?:, Progress: (?P<prog>[0-9.eE+-]+))?")


def say(msg: str) -> None:
    print(f"[muse-setup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\n!!! {msg}\n", flush=True)


class Fail(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def run(cmd, **kw):
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- 状态文件

def file_sig(p: Path):
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return (st.st_mtime_ns, st.st_size, h)


def import_winhub(src: Path) -> list[str]:
    if not src.is_dir():
        return []
    MGR_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in PORTABLE_FILES:
        f = src / name
        if not f.is_file():
            continue
        dst = MGR_DIR / name
        tmp = MGR_DIR / (name + ".tmp-import")
        shutil.copyfile(f, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        copied.append(f"{name} ({dst.stat().st_size} B)")
    return copied


def seed_settings(samples: Path) -> None:
    """首次运行时让 Manager 把样本放到 /samples。只写 Manager 自己的设置文件格式。"""
    f = MGR_DIR / "settings.json"
    if f.exists():
        try:
            loc = json.loads(f.read_text()).get("MuseDownloadLocation")
        except (OSError, ValueError):
            loc = None
        if loc != str(samples):
            say(f"注意: 已有 settings.json,MuseDownloadLocation={loc!r},不是 {samples};保持不变。")
        return
    MGR_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "AppLanguage": "en-US",
        "MuseDownloadLocation": str(samples),
        "AutomaticallyKeepApplicationsUpToDate": False,
        "AutomaticallyKeepSoundsUpToDate": True,
    }, indent=2))
    os.chmod(f, 0o600)


# ---------------------------------------------------------------- Manager 日志

def read_log() -> str:
    try:
        return LOG.read_text(errors="replace")
    except FileNotFoundError:
        return ""


def pack_states(text: str) -> dict:
    """name -> (state, progress, 最后一次更新行号);取每个包最后一条日志。"""
    out = {}
    for i, line in enumerate(text.splitlines()):
        m = PACK_RE.search(line)
        if m:
            out[m["name"]] = (m["state"], float(m["prog"]) if m["prog"] else None, i)
    return out


def initial_states(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "InitialStateCallbackHandler" in line:
            m = PACK_RE.search(line)
            if m:
                out[m["name"]] = m["state"]
    return out


# ---------------------------------------------------------------- OCR(按标签找控件)

def screenshot(path: Path) -> None:
    r = run(["import", "-window", "root", str(path)])
    if r.returncode != 0:
        raise Fail(4, f"截屏失败: {r.stderr.strip()}")


def ocr_words(tmp: Path) -> list[dict]:
    """多个阈值各识别一次再合并,坐标换算回屏幕像素。每次调用都重新截屏。"""
    shot = tmp / "shot.png"
    screenshot(shot)
    words: list[dict] = []
    for thr in (45, 35, 55):
        proc = tmp / f"ocr-{thr}.png"
        r = run(["convert", str(shot), "-resize", "300%", "-colorspace", "Gray",
                 "-threshold", f"{thr}%", str(proc)])
        if r.returncode != 0:
            raise Fail(4, f"convert 失败: {r.stderr.strip()}")
        r = run(["tesseract", str(proc), "stdout", "--psm", "11", "-l", "eng", "tsv"])
        for line in r.stdout.splitlines()[1:]:
            p = line.split("\t")
            if len(p) < 12 or not p[11].strip():
                continue
            try:
                conf = float(p[10])
                x, y, w, h = (int(v) for v in p[6:10])
            except ValueError:
                continue
            if conf < 30:
                continue
            cx, cy = x / 3 + w / 6, y / 3 + h / 6
            text = p[11].strip()
            if any(o["text"] == text and abs(o["cx"] - cx) < 8 and abs(o["cy"] - cy) < 8 for o in words):
                continue
            words.append({"text": text, "cx": cx, "cy": cy})
    return words


def download_buttons(words: list[dict]) -> list[dict]:
    """"Download" 按钮,附带 OCR 读到的包名(同一卡片名称行,仅用于显示和 --only 过滤)。"""
    btns = [w for w in words if w["text"].lower() == "download"]
    muse = [w for w in words if w["text"] == "Muse"]
    res = []
    for b in sorted(btns, key=lambda w: (round(w["cy"] / 40), w["cx"])):
        name_y = [m for m in muse if 15 < b["cy"] - m["cy"] < 60 and abs(m["cx"] - b["cx"]) < 90]
        label = "?"
        if name_y:
            ny = min(name_y, key=lambda m: abs(b["cy"] - m["cy"] - 35))["cy"]
            parts = sorted((w for w in words if abs(w["cy"] - ny) < 8 and abs(w["cx"] - b["cx"]) < 72),
                           key=lambda w: w["cx"])
            label = re.sub(r"\s*Vo\.+.*$", "", " ".join(w["text"] for w in parts))
        res.append({**b, "pack": label})
    return res


def click(w: dict) -> None:
    r = run(["xdotool", "mousemove", str(int(w["cx"])), str(int(w["cy"])), "click", "1"])
    if r.returncode != 0:
        raise Fail(5, f"xdotool 点击失败: {r.stderr.strip()}")


def match_only(label: str, only: list[str]) -> bool:
    if not only:
        return True
    lab = label.replace("...", "").strip().lower()
    return any(o.lower() in lab or lab in o.lower() for o in only if lab and lab != "?")


# ---------------------------------------------------------------- 主流程

class Setup:
    def __init__(self, a):
        self.a = a
        self.proc = None
        self.tmp = Path("/tmp/muse-setup-ocr")
        self.tmp.mkdir(exist_ok=True)
        self.seen_url = None

    def poll_url(self):
        """安全网:Manager 若调用了浏览器(经 shim),把链接打印出来。"""
        try:
            url = LOGIN_URL.read_text().strip()
        except FileNotFoundError:
            return
        if url and url != self.seen_url:
            self.seen_url = url
            print(f"\n==> OPEN THIS LINK IN YOUR BROWSER: {url}\n"
                  "    (Manager 调用了浏览器;链接也保存在 /state/login-url.txt。注意 Linux 版 Manager 没有登录控件,\n"
                  "     登录应在 Windows Muse Hub 完成,见 README。)\n", flush=True)

    def wait(self, cond, timeout, what, code, interval=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.poll_url()
            if self.proc.poll() is not None:
                raise Fail(3, f"Manager 进程提前退出(返回码 {self.proc.returncode}),见 {MANAGER_STDOUT}")
            v = cond()
            if v:
                return v
            time.sleep(interval)
        raise Fail(code, f"等待超时({timeout}s): {what}")

    def start_manager(self):
        MGR_DIR.mkdir(parents=True, exist_ok=True)
        out = open(MANAGER_STDOUT, "ab")
        self.proc = subprocess.Popen(["muse-sounds-manager"], cwd=str(HOME), stdout=out, stderr=out,
                                     start_new_session=True)
        say(f"已启动 Manager(pid {self.proc.pid})")
        self.wait(lambda: run(["xdotool", "search", "--name", "MuseSounds Manager"]).stdout.strip(),
                  self.a.ui_timeout, "Manager 窗口出现", 3)
        say("Manager 窗口已出现,等待界面加载(OCR 读到包名)")

        def ui_ready():
            return sum(1 for w in ocr_words(self.tmp) if w["text"] == "Muse") >= 3
        self.wait(ui_ready, self.a.ui_timeout, "界面出现 'Muse' 包名标签", 4, interval=3)

    def stop_manager(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def main(self):
        a = self.a
        samples = Path(a.samples)
        only = [s.strip() for s in a.only.split(",") if s.strip()]

        # 1 许可状态导入
        winhub = Path(a.winhub)
        copied = import_winhub(winhub)
        if copied:
            say(f"已从 {winhub} 复制 {len(copied)} 个可移植状态文件(内容不打印): " + ", ".join(copied))
            licence_mode = "windows-hub-state"
        else:
            licence_mode = "anonymous"
            warn("未找到 /winhub(或其中没有白名单文件):匿名模式。Manager 生成的 receipt 将不含任何音色包许可,\n"
                 "    之后渲染时所有 Muse 乐器都会被拒绝。要得到可用的 receipt,请按 README 挂载 Windows Muse Hub 数据目录。")
        seed_settings(samples)

        before = file_sig(RECEIPT)
        say("receipt 启动前: " + ("不存在" if before is None else f"{before[1]} B"))

        # 2 启动
        self.start_manager()

        # 3 等 receipt 被 Manager 重写
        def receipt_changed():
            now = file_sig(RECEIPT)
            return now is not None and now != before
        self.wait(receipt_changed, a.receipt_timeout, "Manager 重写 .receipt(mtime/大小/哈希变化)", 6)
        say("Manager 已写出 .receipt(相对启动前发生变化)")

        init = initial_states(read_log())
        present = sorted(n for n, s in init.items() if s == "Installed")
        say("Manager 自己报告已安装的包: " + (", ".join(present) if present else "(无)"))

        # 4 下载
        started: dict[str, int] = {}
        # Manager 自己可能已在后台续传/更新(缓存声称已安装但磁盘上没有时尤其如此):先等它们结束
        self.wait_downloads()

        if a.no_download:
            say("--no-download: 跳过下载阶段")
        else:
            for rnd in range(1, 4):
                words = ocr_words(self.tmp)
                btns = [b for b in download_buttons(words)
                        if match_only(b["pack"], only) and b["pack"] not in started]
                if not btns and rnd == 1:
                    for _ in range(12):  # 目录可能还在加载:最多再等 60 秒,每次重新 OCR
                        time.sleep(5)
                        btns = [b for b in download_buttons(ocr_words(self.tmp))
                                if match_only(b["pack"], only)]
                        if btns:
                            break
                if not btns:
                    if rnd == 1 and not present and not only:
                        raise Fail(4, "界面上找不到任何 Download 按钮,且 Manager 没有已安装的包(界面未加载?)")
                    if rnd == 1 and only and not all(any(o.lower() in p.lower() for p in present) for o in only):
                        raise Fail(4, "没有任何 Download 按钮匹配 --only(可能已安装,或名称不对): " + ", ".join(only))
                    say("界面上没有(符合过滤的)Download 按钮" + ("" if rnd == 1 else ",下载轮次结束"))
                    break
                say(f"第 {rnd} 轮: 界面上 {len(btns)} 个 Download 按钮: " + ", ".join(b["pack"] for b in btns))
                for b in btns:
                    # 点击前重新 OCR,按标签+包名找回按钮
                    fresh = [x for x in download_buttons(ocr_words(self.tmp)) if x["pack"] == b["pack"]]
                    if not fresh:
                        raise Fail(4, f"点击前重新 OCR 找不到 '{b['pack']}' 的 Download 按钮")
                    n_before = read_log().count("Preparing to download library")
                    click(fresh[0])
                    self.wait(lambda: read_log().count("Preparing to download library") > n_before,
                              30, f"点击 '{b['pack']}' 的 Download 后 Manager 开始下载", 5)
                    started[b["pack"]] = n_before
                    say(f"已点击 Download: {b['pack']}")
                time.sleep(8)  # Manager 的 Downloading 状态日志略有延迟
                self.wait_downloads()
            else:
                pass
            left = [b["pack"] for b in download_buttons(ocr_words(self.tmp)) if match_only(b["pack"], only)]
            if left:
                raise Fail(8, "下载轮次用尽后界面上仍有 Download 按钮: " + ", ".join(left))

        # 5 receipt 稳定
        say("等待 receipt 稳定(20 秒内无变化)")
        last = file_sig(RECEIPT)
        stable_since = time.monotonic()
        end = time.monotonic() + a.receipt_timeout
        while time.monotonic() - stable_since < 20:
            if time.monotonic() > end:
                raise Fail(6, "receipt 持续变化,超时")
            time.sleep(2)
            self.poll_url()
            cur = file_sig(RECEIPT)
            if cur != last:
                last, stable_since = cur, time.monotonic()
        self.summary(licence_mode, before, last)

    def wait_downloads(self):
        say("等待下载完成(以 Manager 日志里的 State 为准)")
        last_change = time.monotonic()
        snapshot = None
        while True:
            self.poll_url()
            if self.proc.poll() is not None:
                raise Fail(3, "Manager 在下载期间退出")
            st = pack_states(read_log())
            active = {n: v for n, v in st.items() if v[0] in ("Downloading", "Installing")}
            prog = tuple(sorted((n, v[1], v[2]) for n, v in active.items()))
            if prog != snapshot:
                snapshot, last_change = prog, time.monotonic()
                if active:
                    say("进行中: " + ", ".join(f"{n} {int((v[1] or 0) * 100)}%" for n, v in sorted(active.items())))
            if not active:
                return
            if time.monotonic() - last_change > self.a.stall_timeout:
                raise Fail(7, f"下载 {self.a.stall_timeout}s 无进度: " + ", ".join(active))
            time.sleep(5)

    def summary(self, licence_mode, before, after):
        st = {**initial_states(read_log())}
        st.update({n: v[0] for n, v in pack_states(read_log()).items()})
        rc = RECEIPT.read_bytes() if RECEIPT.exists() else b""
        guids = set()
        try:
            import base64
            body = b"".join(l for l in rc.splitlines() if not l.startswith(b"-----"))
            raw = base64.b64decode(body, validate=False)
            guids = {m.lower() for m in re.findall(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw)}
            if not guids:
                u = raw.decode("utf-16le", errors="ignore")
                guids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", u.lower()))
        except Exception:  # 只是摘要的计数,不影响结果
            pass
        cfg = SAMPLER_DIR / ".config"
        root = cfg.read_text().strip() if cfg.exists() else ""
        inst = (Path(root) / ".instruments").exists() if root else False

        print("\n==================== muse-setup 摘要 ====================")
        for n in sorted(k for k, v in st.items() if v):
            print(f"  包 {n}: {st[n]}")
        print(f"样本根(MuseSampler/.config): {root or '(未生成)'}  .instruments 存在: {inst}")
        if root and Path(root).is_dir():
            total = sum(f.stat().st_size for f in Path(root).rglob("*") if f.is_file())
            print(f"样本根磁盘占用: {total / 1e6:.1f} MB(Manager 的 Installed 状态来自其缓存,"
                  "请以磁盘占用与 .instruments 为准)")
        print(f"receipt: {RECEIPT}  大小 {after[1]} B(启动前 {'无' if before is None else str(before[1]) + ' B'});"
              f"其中 GUID 样式字符串 {len(guids)} 个(只计数,不打印)")
        if LIB.exists():
            h = hashlib.sha256(LIB.read_bytes()).hexdigest()
            print(f"库: {LIB}  {LIB.stat().st_size} B  sha256 {h}")
            out = Path(self.a.out)
            if out.is_dir() and os.access(out, os.W_OK):
                (out / "lib").mkdir(exist_ok=True)
                shutil.copyfile(LIB, out / "lib" / LIB.name)
                print(f"库已复制到 {out}/lib/{LIB.name}")
            else:
                print("(/out 未挂载或不可写,未复制库)")
        else:
            print(f"库: 未找到 {LIB}(Manager 通常在第一次下载后自行安装)")
        if licence_mode == "anonymous":
            warn("匿名模式:receipt 不含任何音色包许可。要让渲染可用,请挂载 Windows Muse Hub 数据目录重跑。")
        else:
            warn("许可数据来自你 Windows Muse Hub 的登录/缓存:请确认在 Windows 上已登录并最近打开过 Muse Hub。\n"
                 "    登录过期可能导致许可失效(未验证)。Linux 版 Manager 没有登录控件。")
        print("完成。Manager 之后不需要再运行。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    e = os.environ.get
    ap.add_argument("--winhub", default=e("MUSE_SETUP_WINHUB", "/winhub"))
    ap.add_argument("--samples", default=e("MUSE_SETUP_SAMPLES", "/samples"))
    ap.add_argument("--out", default=e("MUSE_SETUP_OUT", "/out"))
    ap.add_argument("--only", default=e("MUSE_SETUP_ONLY", ""), help="只下载这些包(逗号分隔,按名称前缀匹配)")
    ap.add_argument("--no-download", action="store_true", default=e("MUSE_SETUP_NO_DOWNLOAD") == "1")
    ap.add_argument("--ui-timeout", type=int, default=int(e("MUSE_SETUP_UI_TIMEOUT", "120")))
    ap.add_argument("--receipt-timeout", type=int, default=int(e("MUSE_SETUP_RECEIPT_TIMEOUT", "180")))
    ap.add_argument("--stall-timeout", type=int, default=int(e("MUSE_SETUP_STALL_TIMEOUT", "900")))
    a = ap.parse_args()

    s = Setup(a)
    try:
        s.main()
        return 0
    except Fail as f:
        print(f"\n错误(退出码 {f.code}): {f}", file=sys.stderr, flush=True)
        return f.code
    except KeyboardInterrupt:
        print("\n被中断。", file=sys.stderr)
        return 130
    finally:
        s.stop_manager()


if __name__ == "__main__":
    sys.exit(main())
