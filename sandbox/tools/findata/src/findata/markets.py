"""Market quotes and bounded, ascending OHLCV history (stdlib only).

Volumes are shares, except indices whose provider-defined volume is retained.
Historical adjustment never silently falls back to an unadjusted series.
US qfq uses Yahoo split/distribution auto-adjustment or Stooq adjustment;
US raw (none) and hfq history are unsupported.
"""

import csv
from datetime import date, datetime, timedelta, timezone
import io
import math
import re
from zoneinfo import ZoneInfo

from .errors import DataError
from .symbols import identify, tencent_symbol, yahoo_symbol
from .transport import get_json, get_text

_PERIODS = {"daily": "day", "weekly": "week", "monthly": "month"}


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite price")
    return number


def _row(day, opening, close, high, low, volume):
    return dict(
        date=date.fromisoformat(str(day)[:10]).isoformat(),
        open=_number(opening),
        high=_number(high),
        low=_number(low),
        close=_number(close),
        volume=_number(volume),
    )


def _date(value, label):
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        raise DataError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DataError(f"{label} must be YYYY-MM-DD") from exc


def _timezone(info):
    return ZoneInfo(
        "America/New_York"
        if info["market"] == "US"
        else "Asia/Hong_Kong"
        if info["market"] == "HK"
        else "Asia/Shanghai"
    )


def _timestamp(raw, info):
    value = str(raw).replace("/", "-")
    if re.fullmatch(r"\d{14}", value):
        stamp = datetime.strptime(value, "%Y%m%d%H%M%S")
    else:
        stamp = datetime.fromisoformat(value)
    return (
        stamp.replace(tzinfo=_timezone(info)).isoformat()
        if stamp.tzinfo is None
        else stamp.isoformat()
    )


def _envelope(info, source, rows, **extra):
    return dict(
        info,
        source=source,
        data_timestamp=extra.pop(
            "data_timestamp", rows[-1].get("timestamp", rows[-1]["date"]) if rows else None
        ),
        last_trading_date=rows[-1]["date"] if rows else None,
        data=rows,
        **extra,
    )


def _parse_tencent(payload, code, period, adjust, info):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise DataError("Tencent returned invalid history data")
    block = data.get(code, {})
    key = (adjust if adjust != "none" else "") + _PERIODS[period]
    values = block.get(key)
    # Indices have no corporate actions and legitimately use the bare key.
    if values is None and (adjust == "none" or info["kind"] == "index"):
        values = block.get(_PERIODS[period])
    if not values:
        raise DataError("Tencent returned no matching adjusted history")
    multiplier = 100 if info["market"] == "CN" and info["kind"] != "index" else 1
    rows = [_row(v[0], v[1], v[2], v[3], v[4], _number(v[5]) * multiplier) for v in values]
    return rows


def _tencent_history(info, period, start, end, adjust, limit):
    code = tencent_symbol(info)
    endpoint = "hkfqkline" if info["market"] == "HK" and adjust != "none" else "fqkline"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/{endpoint}/get"
    # A bound equal to today's date omits Tencent's current session bar.
    # Request one extra calendar day, then apply the caller's inclusive bound.
    cursor = end + timedelta(days=1) if end and end < date.max else end
    collected = {}
    while True:
        params = {
            "param": ",".join(
                (
                    code,
                    _PERIODS[period],
                    start.isoformat() if start else "",
                    cursor.isoformat() if cursor else "",
                    "640",
                    "" if adjust == "none" else adjust,
                )
            )
        }
        payload = get_json(url, params)
        batch = _parse_tencent(payload, code, period, adjust, info)
        for row in batch:
            collected[row["date"]] = row
        earliest = min(row["date"] for row in batch)
        eligible = [
            r
            for r in collected.values()
            if (not start or r["date"] >= start.isoformat())
            and (not end or r["date"] <= end.isoformat())
        ]
        if len(eligible) >= limit or (start and earliest <= start.isoformat()) or len(batch) < 640:
            break
        next_cursor = date.fromisoformat(earliest) - timedelta(days=1)
        if cursor and next_cursor >= cursor:
            raise DataError("Tencent history pagination made no progress")
        cursor = next_cursor
    return list(collected.values()), "tencent"


def _eastmoney_id(info):
    return (
        "116." if info["market"] == "HK" else "1." if info["exchange"] == "sh" else "0."
    ) + info["symbol"]


def _eastmoney_history(info, period, start, end, adjust, limit):
    params = dict(
        secid=_eastmoney_id(info),
        klt={"daily": 101, "weekly": 102, "monthly": 103}[period],
        fqt={"none": 0, "qfq": 1, "hfq": 2}[adjust],
        beg=start.strftime("%Y%m%d") if start else "0",
        end=end.strftime("%Y%m%d") if end else "20500101",
        lmt=max(limit, 640),
        fields1="f1,f2,f3,f4,f5,f6",
        fields2="f51,f52,f53,f54,f55,f56,f57,f59",
    )
    block = (
        get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", params).get("data") or {}
    )
    values = block.get("klines") or []
    if not values:
        raise DataError("Eastmoney returned no history")
    factor = 100 if info["market"] == "CN" and info["kind"] != "index" else 1
    rows = []
    for line in values:
        v = line.split(",")
        row = _row(v[0], v[1], v[2], v[3], v[4], _number(v[5]) * factor)
        for index, key in ((6, "amount"), (7, "pct_chg")):
            if len(v) > index and v[index] not in ("", "-", "null"):
                row[key] = _number(v[index])
        rows.append(row)
    return rows, "eastmoney"


def _sina_history(info, period, start, end, adjust, limit):
    # Sina's KLine endpoint has no adjustment option. Never label it qfq.
    if adjust != "none":
        raise DataError("Sina history supports unadjusted prices only")
    area = "hk" if info["market"] == "HK" else "cn"
    service = "HK_StockService" if area == "hk" else "CN_MarketDataService"
    payload = get_json(
        f"https://quotes.sina.cn/{area}/api/json_v2.php/{service}.getKLineData",
        dict(
            symbol=tencent_symbol(info),
            scale=240,
            ma="no",
            datalen=max(
                limit * (23 if period == "monthly" else 5 if period == "weekly" else 1), 1023
            ),
        ),
    )
    if not isinstance(payload, list) or not payload:
        raise DataError("Sina returned no history")
    rows = [
        _row(v["day"], v["open"], v["close"], v["high"], v["low"], v["volume"]) for v in payload
    ]
    return _aggregate(rows, period), "sina"


def _aggregate(rows, period):
    if period == "daily":
        return rows
    grouped = {}
    for row in sorted(rows, key=lambda r: r["date"]):
        day = date.fromisoformat(row["date"])
        key = day.isocalendar()[:2] if period == "weekly" else (day.year, day.month)
        if key not in grouped:
            grouped[key] = dict(row)
            # Daily percent changes cannot be carried into a multi-day bar.
            grouped[key].pop("pct_chg", None)
        else:
            item = grouped[key]
            item.update(
                date=row["date"],
                close=row["close"],
                high=max(item["high"], row["high"]),
                low=min(item["low"], row["low"]),
                volume=item["volume"] + row["volume"],
            )
            if "amount" in item and "amount" in row:
                item["amount"] += row["amount"]
            else:
                item.pop("amount", None)
            if "timestamp" in row:
                item["timestamp"] = row["timestamp"]
    return list(grouped.values())


def _period_start(day, period):
    """Include the full first bucket when aggregating daily provider bars."""
    if day is None or period == "daily":
        return day
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _parse_yahoo(payload, info, adjust):
    if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
        raise DataError("Yahoo returned invalid chart data")
    chart = payload["chart"]
    if chart.get("error") or not chart.get("result"):
        raise DataError(f"Yahoo chart unavailable: {chart.get('error')}")
    block = chart["result"][0]
    values = block.get("indicators", {}).get("quote", [{}])[0]
    adjusted = block.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
    if adjust == "qfq" and adjusted is None and info["kind"] != "index":
        raise DataError("Yahoo omitted adjusted closes")
    rows = []
    for i, timestamp in enumerate(block.get("timestamp", [])):
        prices = [values.get(key, [])[i] for key in ("open", "close", "high", "low")]
        if any(value is None for value in prices):
            continue
        ratio = 1
        if adjust == "qfq" and adjusted is not None:
            if adjusted[i] is None or not prices[1]:
                continue
            ratio = adjusted[i] / prices[1]
        day = datetime.fromtimestamp(timestamp, _timezone(info)).date().isoformat()
        row = _row(day, *(value * ratio for value in prices), values.get("volume", [])[i] or 0)
        row["timestamp"] = datetime.fromtimestamp(timestamp, _timezone(info)).isoformat()
        rows.append(row)
    if not rows:
        raise DataError("Yahoo returned no price rows")
    return rows, block.get("meta", {})


def _yahoo_history(info, period, start, end, adjust, limit):
    if adjust != "qfq":
        raise DataError(
            "Yahoo history supports qfq only: OHLC is already split-adjusted, not raw; hfq is unavailable"
        )
    end_day = end or date.today()
    try:
        begin = start or end_day - timedelta(
            days=max(30, limit * (40 if period == "monthly" else 10 if period == "weekly" else 3))
        )
        begin = _period_start(begin, period)
        params = dict(
            period1=int(datetime.combine(begin, datetime.min.time(), _timezone(info)).timestamp()),
            period2=int(
                datetime.combine(
                    end_day + timedelta(days=1), datetime.min.time(), _timezone(info)
                ).timestamp()
            ),
            interval="1d",
            events="history",
            includeAdjustedClose="true",
        )
    except (OverflowError, ValueError, OSError) as exc:
        raise DataError(
            "Yahoo history date range is not representable; use an end before 9999-12-31 and a smaller limit or explicit start"
        ) from exc
    failures = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            payload = get_json(f"https://{host}/v8/finance/chart/{yahoo_symbol(info)}", params)
            rows, _ = _parse_yahoo(payload, info, adjust)
            return _aggregate(rows, period), "yahoo"
        except (
            DataError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            OverflowError,
            OSError,
        ) as exc:
            failures.append(str(exc))
    raise DataError("; ".join(failures))


def _stooq_history(info, period, start, end, adjust, limit):
    if adjust != "qfq":
        raise DataError("Stooq CSV is adjusted; cannot supply unadjusted/hfq history")
    symbol = {"^GSPC": "^spx", "^DJI": "^dji", "^IXIC": "^ndq"}.get(
        info["symbol"], info["symbol"].lower() + ".us"
    )
    params = {"s": symbol, "i": "d"}
    if start:
        params["d1"] = _period_start(start, period).strftime("%Y%m%d")
    if end:
        params["d2"] = end.strftime("%Y%m%d")
    text = get_text("https://stooq.com/q/d/l/", params)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Date" not in reader.fieldnames:
        raise DataError("Stooq returned no CSV history (possibly browser challenge)")
    rows = [
        _row(v["Date"], v["Open"], v["Close"], v["High"], v["Low"], v.get("Volume") or 0)
        for v in reader
    ]
    if not rows:
        raise DataError("Stooq returned empty history")
    return _aggregate(rows, period), "stooq"


def kline(symbol, market=None, period="daily", start=None, end=None, adjust="qfq", limit=250):
    """Return up to limit ascending bars, inclusive ISO date bounds.

    Adjustment is qfq (forward/auto-adjusted), hfq, or none. US supports
    qfq only: Yahoo split/distribution auto-adjustment or Stooq adjustment,
    not raw historical traded prices. Source dates are retained.
    Yahoo/Stooq weekly/monthly bars include the full first calendar bucket;
    end can truncate the final bucket, which is dated at its last session.
    Unrepresentable provider date/lookback arithmetic raises DataError.
    """
    if period not in _PERIODS:
        raise DataError("period must be daily, weekly or monthly")
    if adjust not in ("qfq", "hfq", "none"):
        raise DataError("adjust must be qfq, hfq or none")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DataError("limit must be a positive integer")
    start, end = _date(start, "start"), _date(end, "end")
    if start and end and start > end:
        raise DataError("start must not be after end")
    info = identify(symbol, market)
    if info["market"] == "US" and adjust != "qfq":
        raise DataError(
            "US history supports qfq only; none is unavailable because Yahoo OHLC is already split-adjusted, and hfq is unsupported"
        )
    providers = (
        (_yahoo_history, _stooq_history)
        if info["market"] == "US"
        else (_tencent_history, _eastmoney_history, _sina_history)
    )
    failures = []
    for provider in providers:
        try:
            rows, source = provider(info, period, start, end, adjust, limit)
            rows = sorted(
                {
                    r["date"]: r
                    for r in rows
                    if (start is None or r["date"] >= start.isoformat())
                    and (end is None or r["date"] <= end.isoformat())
                }.values(),
                key=lambda r: r["date"],
            )[-limit:]
            if not rows:
                raise DataError("no bars in requested date range")
            return _envelope(info, source, rows, period=period, adjust=adjust)
        except (
            DataError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            OSError,
        ) as exc:
            failures.append(str(exc))
    raise DataError("all history sources failed: " + "; ".join(failures))


def _parse_quote(text, info):
    match = re.search(r'="([^"]+)"', text)
    if not match:
        raise DataError("Tencent returned no quote")
    v = match.group(1).split("~")
    if len(v) < 35 or not v[3] or _number(v[3]) <= 0:
        raise DataError("Tencent returned invalid quote")
    stamp = _timestamp(v[30], info)
    factor = 100 if info["market"] == "CN" and info["kind"] != "index" else 1
    return dict(
        info,
        name=v[1],
        source="tencent",
        data_timestamp=stamp,
        last_trading_date=stamp[:10],
        price=_number(v[3]),
        previous_close=_number(v[4]),
        open=_number(v[5]),
        high=_number(v[33]),
        low=_number(v[34]),
        volume=_number(v[6]) * factor,
        change=_number(v[31]),
        change_percent=_number(v[32]),
    )


def _eastmoney_quote(info):
    block = (
        get_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            dict(
                secid=_eastmoney_id(info),
                fields="f43,f44,f45,f46,f47,f57,f58,f59,f60,f86,f169,f170",
            ),
        ).get("data")
        or {}
    )
    scale = 10 ** int(block["f59"])
    stamp = datetime.fromtimestamp(int(block["f86"]), _timezone(info)).isoformat()
    factor = 100 if info["market"] == "CN" and info["kind"] != "index" else 1
    return dict(
        info,
        name=block["f58"],
        source="eastmoney",
        data_timestamp=stamp,
        last_trading_date=stamp[:10],
        price=_number(block["f43"]) / scale,
        open=_number(block["f46"]) / scale,
        high=_number(block["f44"]) / scale,
        low=_number(block["f45"]) / scale,
        previous_close=_number(block["f60"]) / scale,
        volume=_number(block["f47"]) * factor,
        change=_number(block["f169"]) / scale,
        change_percent=_number(block["f170"]) / 100,
    )


def _sina_quote(info):
    code = tencent_symbol(info)
    if info["market"] == "HK":
        code = "rt_" + code
    text = get_text("https://hq.sinajs.cn/list=" + code)
    match = re.search(r'="([^"]+)"', text)
    if not match:
        raise DataError("Sina returned no quote")
    v = match.group(1).split(",")
    if info["market"] == "CN":
        name, opening, previous, price, high, low = v[:6]
        volume, raw = v[8], v[30] + " " + v[31]
    else:
        name, opening, previous, high, low, price = v[1:7]
        volume, raw = v[12], v[17] + " " + v[18]
    stamp = _timestamp(raw, info)
    change = _number(price) - _number(previous)
    return dict(
        info,
        name=name,
        source="sina",
        data_timestamp=stamp,
        last_trading_date=stamp[:10],
        price=_number(price),
        open=_number(opening),
        previous_close=_number(previous),
        high=_number(high),
        low=_number(low),
        volume=_number(volume),
        change=change,
        change_percent=change / _number(previous) * 100 if _number(previous) else None,
    )


def _yahoo_quote(info):
    failures = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            payload = get_json(
                f"https://{host}/v8/finance/chart/{yahoo_symbol(info)}",
                {"range": "5d", "interval": "1d"},
            )
            rows, meta = _parse_yahoo(payload, info, "none")
            latest = rows[-1]
            stamp = datetime.fromtimestamp(meta["regularMarketTime"], _timezone(info)).isoformat()
            previous = meta.get("chartPreviousClose") if len(rows) < 2 else rows[-2]["close"]
            price = _number(meta["regularMarketPrice"])
            return dict(
                info,
                name=meta.get("longName", info["name"]),
                source="yahoo",
                data_timestamp=stamp,
                last_trading_date=stamp[:10],
                price=price,
                previous_close=previous,
                open=latest["open"],
                high=latest["high"],
                low=latest["low"],
                volume=latest["volume"],
                change=price - previous if previous else None,
                change_percent=(price / previous - 1) * 100 if previous else None,
            )
        except (DataError, KeyError, ValueError, TypeError, IndexError) as exc:
            failures.append(str(exc))
    raise DataError("; ".join(failures))


def _stooq_quote(info):
    rows, source = _stooq_history(info, "daily", date.today() - timedelta(days=30), None, "qfq", 2)
    rows = sorted(rows, key=lambda row: row["date"])
    latest = rows[-1]
    previous = rows[-2]["close"] if len(rows) > 1 else None
    return dict(
        info,
        source=source,
        quote_type="daily_close",
        data_timestamp=latest["date"],
        last_trading_date=latest["date"],
        price=latest["close"],
        previous_close=previous,
        open=latest["open"],
        high=latest["high"],
        low=latest["low"],
        volume=latest["volume"],
        change=latest["close"] - previous if previous else None,
        change_percent=(latest["close"] / previous - 1) * 100 if previous else None,
    )


def quote(symbol, market=None):
    """Return a provider timestamped quote, not a fabricated retrieval time."""
    info = identify(symbol, market)
    failures = []
    providers = (
        (_yahoo_quote, _stooq_quote)
        if info["market"] == "US"
        else (
            lambda i: _parse_quote(get_text("https://qt.gtimg.cn/q=" + tencent_symbol(i)), i),
            _sina_quote,
            _eastmoney_quote,
        )
    )
    for provider in providers:
        try:
            return provider(info)
        except (DataError, KeyError, ValueError, TypeError, IndexError) as exc:
            failures.append(str(exc))
    raise DataError("all quote sources failed: " + "; ".join(failures))
