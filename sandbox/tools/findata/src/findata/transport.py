"""Bounded public HTTP requests; urllib honors HTTP(S)_PROXY and NO_PROXY."""

import gzip
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .errors import DataError


def get_text(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    timeout = float(os.environ.get("FINDATA_TIMEOUT", "10"))
    retries = int(os.environ.get("FINDATA_RETRIES", "1"))
    if timeout <= 0 or not 0 <= retries <= 5:
        raise DataError("FINDATA_TIMEOUT must be positive; FINDATA_RETRIES must be 0..5")
    failures = []
    for attempt in range(retries + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 findata/1.0",
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://fund.eastmoney.com/"
                    if (urlsplit(url).hostname or "").endswith("fund.eastmoney.com")
                    else "https://finance.sina.com.cn/",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise DataError("Source response exceeds 16 MiB")
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                encoding = response.headers.get_content_charset()
            for codec in dict.fromkeys([encoding or "utf-8", "utf-8", "gb18030"]):
                try:
                    return payload.decode(codec)
                except (UnicodeDecodeError, LookupError):
                    continue
            raise DataError("Cannot decode source response")
        except HTTPError as exc:
            failures.append(f"HTTP {exc.code}")
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                break
        except (URLError, TimeoutError, OSError) as exc:
            failures.append(str(exc))
        if attempt < retries:
            time.sleep(0.25 * (2**attempt))
    raise DataError(
        f"HTTP request failed: {url.split('?')[0]}: {'; '.join(failures)}", attempts=failures
    )


def get_json(url, params=None):
    text = get_text(url, params)
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise DataError("Source did not return valid JSON") from exc
