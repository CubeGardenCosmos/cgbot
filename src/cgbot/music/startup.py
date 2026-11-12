"""启动时在宿主机音色目录后台确保默认 SoundFont(失败软处理,只记日志)。"""

from __future__ import annotations

import logging

from cgbot.config.settings import MusicConfig
from cgbot.music.soundfont import SoundfontStore

log = logging.getLogger(__name__)

SOUNDFONT_DIR_MODE = 0o755


def prepare_soundfont_dir(config: MusicConfig) -> bool:
    """同步创建音色目录(0755,沙盒 uid 1000 可读),使首个沙盒容器即带上只读挂载。

    音乐关闭或创建失败返回 False;不抛异常。
    """
    if not config.enabled:
        return False
    try:
        config.soundfont_dir.mkdir(mode=SOUNDFONT_DIR_MODE, parents=True, exist_ok=True)
        config.soundfont_dir.chmod(SOUNDFONT_DIR_MODE)
    except OSError as exc:
        log.warning("soundfont dir unavailable: error=%s", type(exc).__name__)
        return False
    return True


async def ensure_default_soundfont(
    config: MusicConfig, store: SoundfontStore | None = None
) -> bool:
    """下载并校验推荐默认音色;已安装则直接返回。任何失败只记日志并返回 False。"""
    if not config.enabled or not config.auto_download_soundfont:
        return False
    try:
        path = await (store or SoundfontStore(config.soundfont_dir)).ensure()
    except Exception as exc:
        # 不记录异常详情:下载 URL 与路径不进日志。
        log.warning("default soundfont not ensured: error=%s", type(exc).__name__)
        return False
    log.info("default soundfont ready: file=%s", path.name)
    return True
