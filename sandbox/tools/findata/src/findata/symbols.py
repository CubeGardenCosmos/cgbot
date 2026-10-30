"""Canonical CN, HK and US symbols and provider identifiers."""

import json
import re

from .errors import DataError
from .transport import get_json, get_text

_ALIASES = {
    "贵州茅台": ("600519", "贵州茅台", "CN", "stock", "sh"),
    "腾讯控股": ("00700", "腾讯控股", "HK", "stock", "hk"),
    "上证指数": ("000001", "上证指数", "CN", "index", "sh"),
    "上证综指": ("000001", "上证指数", "CN", "index", "sh"),
    "深证成指": ("399001", "深证成指", "CN", "index", "sz"),
    "创业板指": ("399006", "创业板指", "CN", "index", "sz"),
    "沪深300": ("000300", "沪深300", "CN", "index", "sh"),
    "恒生指数": ("HSI", "恒生指数", "HK", "index", "hk"),
    "HSI": ("HSI", "恒生指数", "HK", "index", "hk"),
    "^HSI": ("HSI", "恒生指数", "HK", "index", "hk"),
    "标普500": ("^GSPC", "S&P 500", "US", "index", "us"),
    "SPX": ("^GSPC", "S&P 500", "US", "index", "us"),
    "^GSPC": ("^GSPC", "S&P 500", "US", "index", "us"),
    "^DJI": ("^DJI", "Dow Jones Industrial Average", "US", "index", "us"),
    "道琼斯指数": ("^DJI", "Dow Jones Industrial Average", "US", "index", "us"),
    "^IXIC": ("^IXIC", "NASDAQ Composite", "US", "index", "us"),
    "纳斯达克指数": ("^IXIC", "NASDAQ Composite", "US", "index", "us"),
}


def _record(symbol, name, market, kind="stock", exchange=None):
    result = dict(symbol=symbol, name=name, market=market, kind=kind)
    if exchange:
        result["exchange"] = exchange
    return result


def _market(market):
    if market is None:
        return None
    value = str(market).upper()
    value = {"SH": "CN", "SZ": "CN", "BJ": "CN", "CHINA": "CN", "USA": "US"}.get(value, value)
    if value not in ("CN", "HK", "US"):
        raise DataError("market must be CN, HK or US")
    return value


def resolve(query, market=None):
    """Resolve names or codes; explicit sh/sz prefixes disambiguate CN indices."""
    market = _market(market)
    text = str(query).strip()
    if not text:
        raise DataError("symbol query is empty")
    alias = _ALIASES.get(text) or _ALIASES.get(text.upper())
    if alias and (market is None or alias[2] == market):
        return [_record(*alias)]
    # A pasted terminal/encoding prefix must not make a valid numeric code unusable.
    numeric = re.search(r"(?i)(?:(sh|sz|bj|hk)[.:]?)?(\d{5,6})(?:\.(SS|SH|SZ|BJ|HK))?$", text)
    if numeric:
        prefix, code, suffix = numeric.groups()
        prefix = (prefix or suffix or "").lower()
        if prefix == "ss":
            prefix = "sh"
        target = market or ("HK" if prefix == "hk" or len(code) == 5 else "CN")
        if target == "HK":
            if len(code) > 5:
                raise DataError("HK stock codes must have at most five digits")
            return [_record(code.zfill(5), code.zfill(5), "HK", exchange="hk")]
        if target == "CN" and len(code) == 6:
            exchange = (
                prefix
                if prefix in ("sh", "sz", "bj")
                else ("sh" if code[0] in "569" else "bj" if code[0] in "48" else "sz")
            )
            kind = (
                "index"
                if (exchange == "sh" and code.startswith("000"))
                or (exchange == "sz" and code.startswith("399"))
                else "stock"
            )
            return [_record(code, code, "CN", kind, exchange)]
    ticker = re.sub(r"(?i)^us[.:]", "", text).upper()
    if ticker.endswith(".US"):
        ticker = ticker[:-3]
    if (market in (None, "US")) and re.fullmatch(r"\^?[A-Z][A-Z0-9.\-]{0,14}", ticker):
        return [_record(ticker, ticker, "US", "index" if ticker.startswith("^") else "stock", "us")]
    try:
        response = get_text("https://smartbox.gtimg.cn/s3/", {"q": text, "t": "all"})
        match = re.search(r'="(.*)"', response)
        if not match:
            raise ValueError("missing search results")
        value = json.loads('"' + match.group(1) + '"')
        results = []
        for entry in value.split("^"):
            fields = entry.split("~")
            if len(fields) < 3:
                continue
            exchange, code, name = fields[:3]
            target = (
                "CN"
                if exchange in ("sh", "sz", "bj")
                else "HK"
                if exchange == "hk"
                else "US"
                if exchange == "us"
                else None
            )
            if target and (market is None or target == market):
                if target == "US":
                    code = re.sub(r"(?i)\.(n|oq|o|a|ps)$", "", code).upper()
                kind = (
                    "index" if len(fields) > 4 and fields[4] in ("ZS", "IDX", "INDEX") else "stock"
                )
                results.append(_record(code, name, target, kind, exchange))
        return results
    except (DataError, ValueError, TypeError) as exc:
        first_failure = str(exc)
    try:
        payload = get_json(
            "https://searchapi.eastmoney.com/api/suggest/get",
            {"input": text, "type": 14, "count": 20, "token": "D43BF722C8E33BDC906FB84D85E326E8"},
        )
        results = []
        for item in (payload.get("QuotationCodeTable") or {}).get("Data") or []:
            code, name = item["Code"], item["Name"]
            quote_id = str(item.get("QuoteID", ""))
            classify = str(item.get("Classify", ""))
            if quote_id.startswith("116.") or classify == "HK":
                target, exchange = "HK", "hk"
            elif classify == "US" or quote_id.startswith(("105.", "106.", "107.")):
                target, exchange = "US", "us"
            elif quote_id.startswith(("0.", "1.")):
                target, exchange = "CN", "sh" if quote_id.startswith("1.") else "sz"
            else:
                continue
            if market is None or target == market:
                kind = "index" if "指数" in str(item.get("SecurityTypeName", "")) else "stock"
                results.append(_record(code, name, target, kind, exchange))
        return results
    except (DataError, ValueError, TypeError, KeyError) as exc:
        raise DataError(
            f"symbol search failed for {text}: Tencent: {first_failure}; Eastmoney: {exc}"
        ) from exc


def identify(symbol, market=None):
    results = resolve(symbol, market)
    if not results:
        raise DataError(f"no symbol found: {symbol}")
    return results[0]


def tencent_symbol(info):
    if info["symbol"] == "^GSPC":
        return "us.INX"
    if info["symbol"] == "^DJI":
        return "us.DJI"
    if info["symbol"] == "^IXIC":
        return "us.IXIC"
    return info.get("exchange", info["market"].lower()) + info["symbol"]


def yahoo_symbol(info):
    if info["market"] == "HK":
        return "^HSI" if info["kind"] == "index" else info["symbol"].lstrip("0") + ".HK"
    if info["market"] == "CN":
        return info["symbol"] + (".SS" if info["exchange"] == "sh" else ".SZ")
    return info["symbol"]
