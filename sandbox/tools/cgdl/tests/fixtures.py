"""离线测试用的内嵌夹具。内容取自真实页面结构的精简版本,不含任何真实令牌。"""

from __future__ import annotations

import html as html_lib
import json

BUILD_URL = "https://musescore.com/static/public/build/musescore_es6/202611/2946.test.js"
# 测试用假 secret;真实 secret 只在运行时从页面脚本读取,从不写入代码。
FAKE_SECRET = "zq"

SEARCH_HTML = """<!doctype html><html><head>
<title>Search result for 'gymnopedie' | Free sheet music | MuseScore.com</title>
</head><body>
<a aria-label="Gymnopedie No. 1 - Erik Satie (Piano Solo)"
   href="https://musescore.com/user/39593079/scores/6969530">
  <img src="https://cdn.ustatik.com/musescore/scoredata/g/abc/score_0.png" alt="">
</a>
<a aria-label="Gymnopedie No. 3 - Erik Satie" href="https://musescore.com/user/111/scores/222">
  <img src="https://cdn.ustatik.com/musescore/scoredata/g/def/score_0.png" alt="">
</a>
<a href="https://musescore.com/user/111/scores/222">duplicate card link without label</a>
<a aria-label="foreign" href="https://example.com/user/1/scores/2">not musescore</a>
</body></html>
"""

SEARCH_STORE_HTML = (
    "<!doctype html><html><head><title>Search result for 'x' | MuseScore</title></head><body>"
    '<div class="js-store" data-content="'
    + html_lib.escape(
        json.dumps(
            {
                "search": {
                    "scores": [
                        {
                            "id": 5,
                            "title": "Store Title",
                            "url": "https://musescore.com/user/9/scores/5",
                            "thumbnail": "https://cdn.ustatik.com/t.png",
                        }
                    ]
                }
            }
        )
    )
    + '"></div></body></html>'
)

NOT_FOUND_HTML = (
    "<!doctype html><html><head><title>Page not found (404) | MuseScore</title></head></html>"
)

CHALLENGE_HTML = "<!doctype html><html><head><title>Just a moment...</title></head></html>"

SCORE_HTML = """<!doctype html><html><head>
<title>Gymnopédie No.1 by Erik Satie | MuseScore</title>
<meta property="og:url" content="https://musescore.com/user/39593079/scores/6969530">
<meta property="og:title" content="Gymnopédie No.1 - Erik Satie - MuseScore">
<meta name="musescore:author" content="Hal Leonard">
<meta name="musescore:composer" content="Erik Satie">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"MusicComposition",
"name":"Gymnopédie No.1","composer":{"@type":"Person","name":"Erik Satie"},
"musicalKey":"D major, B minor","datePublished":"2016-01-01","dateModified":"2026-01-01",
"thumbnailUrl":"https://cdn.ustatik.com/x.png",
"url":"https://musescore.com/user/39593079/scores/6969530"}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"offers":{"@type":"Offer","price":"1.49","priceCurrency":"USD"}}</script>
</head><body></body></html>
"""

BUILD_JS = 'function t(n,r,i){return md5(n+r+i+"' + FAKE_SECRET + '").substr(0, 4)}'

MID_BYTES = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x00\x60"
MP3_BYTES = b"\xff\xfb\x90\x44" + b"\x00" * 16
MXL_BYTES = b"PK\x03\x04" + b"\x00" * 16
SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


def jmuse_payload(url: str) -> dict[str, object]:
    return {"result": "success", "status": 200, "error": None, "info": {"url": url}}


def jmuse_json(url: str) -> str:
    return json.dumps(jmuse_payload(url))
