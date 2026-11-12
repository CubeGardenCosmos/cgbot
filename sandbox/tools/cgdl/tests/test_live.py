"""在线测试:访问真实 musescore.com 与本机 Chromium。默认跳过,使用 `pytest -m integration` 运行。

请求数量保持很少:1 次搜索、1 次信息、mid 与 mp3 各 1 次下载。
"""

from pathlib import Path

import pytest

from cgdl.client import MuseScoreClient
from cgdl.urls import parse_score_ref

GYMNOPEDIE = "https://musescore.com/user/39593079/scores/6969530"


@pytest.mark.integration
def test_live_search_info_and_downloads(tmp_path: Path) -> None:
    with MuseScoreClient() as client:
        hits = client.search("gymnopedie", 3)
        assert hits, "live search returned no cards"
        assert all(hit.url.startswith("https://musescore.com/user/") for hit in hits)

        ref = parse_score_ref(GYMNOPEDIE)
        page = client.info(ref)
        assert page.score_id == ref.score_id
        assert "Gymnop" in page.title

        mid = client.get(ref, "mid", tmp_path)
        assert Path(str(mid["path"])).read_bytes()[:4] == b"MThd"

        mp3 = client.get(ref, "mp3", tmp_path)
        mp3_bytes = Path(str(mp3["path"])).read_bytes()
        assert mp3_bytes[:3] == b"ID3" or mp3_bytes[:1] == b"\xff"
