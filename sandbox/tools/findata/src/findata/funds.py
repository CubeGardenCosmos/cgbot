"""Public mutual-fund NAV, performance rankings and fund profiles.

NAV history and rankings use independent Eastmoney web/mobile backends (not
independent market-data vendors). Profile fallback is a smaller Sina snapshot.
"""

import json
import re
from datetime import datetime, timezone, timedelta

from .errors import DataError
from .transport import get_json, get_text

_MOBILE = "https://fundmobapi.eastmoney.com/FundMNewApi/"
_MOBILE_PARAMS = {"deviceid": "fund", "plat": "Android", "product": "EFund", "version": "6.5.5"}
_CST = timezone(timedelta(hours=8))
_PERIODS = {
    "1d": ("RZDF", "rzdf", 6),
    "1w": ("SYL_Z", "zzf", 7),
    "1m": ("SYL_Y", "1yzf", 8),
    "3m": ("SYL_3Y", "3yzf", 9),
    "6m": ("SYL_6Y", "6yzf", 10),
    "1y": ("SYL_1N", "1nzf", 11),
    "2y": ("SYL_2N", "2nzf", 12),
    "3y": ("SYL_3N", "3nzf", 13),
    "ytd": ("SYL_JN", "jnzf", 14),
    "all": ("SYL_LN", "lnzf", 15),
}
_TYPES = {
    "all": ("0", "all"),
    "stock": ("25", "gp"),
    "hybrid": ("27", "hh"),
    "bond": ("31", "zq"),
    "index": ("26", "zs"),
    "qdii": ("6", "qdii"),
}


def _number(value):
    if value in (None, "", "--", "-"):
        return None
    return float(value)


def _symbol(symbol):
    symbol = str(symbol)
    if not re.fullmatch(r"\d{6}", symbol):
        raise DataError("Fund symbol must be a six-digit code")
    return symbol


def _limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise DataError("limit must be an integer between 1 and 10000")
    return limit


def _date(value):
    if value is None:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise DataError("Dates must use YYYY-MM-DD") from exc


def _result(source, data, **extra):
    dates = [
        row["date"]
        for row in data
        if row.get("date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"])
    ]
    return dict(
        source=source,
        data_timestamp=max(dates) if dates else None,
        retrieved_at=datetime.now(_CST).isoformat(),
        last_trading_date=max(dates) if dates else None,
        data_date_range={"start": min(dates), "end": max(dates)} if dates else None,
        data=data,
        **extra,
    )


def _attempt(providers):
    failures = []
    for name, provider in providers:
        try:
            return provider()
        except (DataError, ValueError, KeyError, TypeError, IndexError) as exc:
            failures.append(f"{name}: {exc}")
    raise DataError("; ".join(failures))


def _js_value(text, name):
    match = re.search(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", text)
    if not match:
        raise DataError(f"Missing fund field {name}")
    return json.JSONDecoder().raw_decode(text[match.end() :])[0]


def _nav_row(row):
    return {
        "date": row["FSRQ"],
        "nav": _number(row["DWJZ"]),
        "accumulated_nav": _number(row.get("LJJZ")),
        "change_pct": _number(row.get("JZZZL")),
    }


def nav(symbol, start=None, end=None, limit=250):
    """Return the newest matching NAV observations in chronological order.

    A NAV is not an OHLC bar. Currency funds may report per-10,000-unit income
    in place of NAV; these rows retain the provider's nav_type for distinction.
    """
    symbol, limit = _symbol(symbol), _limit(limit)
    start, end = _date(start), _date(end)
    if start and end and start > end:
        raise DataError("start must not be after end")

    def history():
        rows, page, total = [], 1, None
        while len(rows) < limit:
            payload = get_json(
                "https://api.fund.eastmoney.com/f10/lsjz",
                {
                    "fundCode": symbol,
                    "pageIndex": page,
                    "pageSize": 100,
                    "startDate": start or "",
                    "endDate": end or "",
                },
            )
            if payload.get("ErrCode", 0) != 0:
                raise DataError(str(payload.get("ErrMsg")))
            records = (payload.get("Data") or {}).get("LSJZList") or []
            if total is None:
                total = int(payload.get("TotalCount", 0))
            for record in records:
                day = record["FSRQ"]
                if (not start or day >= start) and (not end or day <= end):
                    row = _nav_row(record)
                    row["nav_type"] = record.get("NAVTYPE")
                    rows.append(row)
                    if len(rows) == limit:
                        break
            if not records or page * 100 >= total or (start and records[-1]["FSRQ"] < start):
                break
            page += 1
        if not rows:
            # Confirm that the code exists before returning an empty date window.
            if not total:
                raise DataError("No fund NAV history returned")
        return _result("eastmoney_f10", sorted(rows, key=lambda r: r["date"]), symbol=symbol)

    def chart():
        text = get_text(f"https://fund.eastmoney.com/pingzhongdata/{symbol}.js")
        if str(_js_value(text, "fS_code")) != symbol:
            raise DataError("Fund code mismatch")
        trend = _js_value(text, "Data_netWorthTrend")
        cumulative = dict(_js_value(text, "Data_ACWorthTrend"))
        rows = []
        for point in trend:
            day = datetime.fromtimestamp(point["x"] / 1000, _CST).date().isoformat()
            if (not start or day >= start) and (not end or day <= end):
                rows.append(
                    {
                        "date": day,
                        "nav": _number(point["y"]),
                        "accumulated_nav": _number(cumulative.get(point["x"])),
                        "change_pct": _number(point.get("equityReturn")),
                        "nav_type": None,
                    }
                )
        if not trend:
            raise DataError("No fund NAV chart returned")
        return _result(
            "eastmoney_pingzhongdata", sorted(rows, key=lambda r: r["date"])[-limit:], symbol=symbol
        )

    return _attempt([("Eastmoney NAV history", history), ("Eastmoney NAV chart", chart)])


def rank(period="1y", fund_type="all", limit=20):
    """Rank funds by percentage return; percentage values are not fractions."""
    limit = _limit(limit)
    period = {"1n": "1y", "year": "1y", "month": "1m"}.get(period, period)
    fund_type = {"equity": "stock", "mixed": "hybrid"}.get(fund_type, fund_type)
    if period not in _PERIODS or fund_type not in _TYPES:
        raise DataError("Unsupported fund period or type")
    mobile_sort, web_sort, column = _PERIODS[period]
    mobile_type, web_type = _TYPES[fund_type]

    def mobile():
        rows, page = [], 1
        while len(rows) < limit:
            payload = get_json(
                _MOBILE + "FundMNRank",
                dict(
                    _MOBILE_PARAMS,
                    FundType=mobile_type,
                    SortColumn=mobile_sort,
                    Sort="desc",
                    pageIndex=page,
                    pageSize=30,
                ),
            )
            records = payload.get("Datas") or []
            if not isinstance(records, list) or not records:
                break
            for item in records:
                value = _number(item.get(mobile_sort))
                if value is not None:
                    rows.append(
                        {
                            "symbol": item["FCODE"],
                            "name": item["SHORTNAME"],
                            "date": item.get("FSRQ")
                            if item.get("FSRQ") not in ("", "--")
                            else None,
                            "nav": _number(item.get("DWJZ")),
                            "return_pct": value,
                        }
                    )
                    if len(rows) == limit:
                        break
            if len(records) < 30 or page * 30 >= int(payload.get("TotalCount") or 100000):
                break
            page += 1
        if not rows:
            raise DataError("No fund ranking returned")
        return _result("eastmoney_mobile", rows, period=period, fund_type=fund_type)

    def web():
        rows, page = [], 1
        while len(rows) < limit:
            text = get_text(
                "https://fund.eastmoney.com/data/rankhandler.aspx",
                {
                    "op": "ph",
                    "dt": "kf",
                    "ft": web_type,
                    "pi": page,
                    "pn": 100,
                    "sc": web_sort,
                    "st": "desc",
                },
            )
            match = re.search(r"\bdatas\s*:\s*", text)
            if not match:
                raise DataError("Missing fund ranking data")
            records = json.JSONDecoder().raw_decode(text[match.end() :])[0]
            for record in records:
                fields = record.split(",")
                value = _number(fields[column])
                if value is not None:
                    rows.append(
                        {
                            "symbol": fields[0],
                            "name": fields[1],
                            "date": fields[3] or None,
                            "nav": _number(fields[4]),
                            "return_pct": value,
                        }
                    )
                    if len(rows) == limit:
                        break
            if len(records) < 100:
                break
            page += 1
        if not rows:
            raise DataError("No fund ranking returned")
        return _result("eastmoney_rankhandler", rows, period=period, fund_type=fund_type)

    return _attempt([("Eastmoney mobile rank", mobile), ("Eastmoney web rank", web)])


def info(symbol):
    """Return a fund profile, falling back to Sina's smaller NAV snapshot."""
    symbol = _symbol(symbol)

    def mobile():
        payload = get_json(_MOBILE + "FundMNDetailInformation", dict(_MOBILE_PARAMS, FCODE=symbol))
        item = payload.get("Datas") or {}
        if item.get("FCODE") != symbol or not item.get("SHORTNAME"):
            raise DataError("Fund profile unavailable")
        data = {
            "symbol": symbol,
            "name": item["SHORTNAME"],
            "full_name": item.get("FULLNAME"),
            "fund_type": item.get("FTYPE"),
            "inception_date": item.get("ESTABDATE"),
            "manager": item.get("JJJL"),
            "company": item.get("JJGS"),
            "custodian": item.get("TGYH"),
            "benchmark": item.get("BENCH"),
            "net_assets": _number(item.get("ENDNAV")),
            "assets_date": item.get("FEGMRQ"),
            "management_fee": item.get("MGREXP"),
            "custody_fee": item.get("TRUSTEXP"),
        }
        result = _result("eastmoney_mobile", [], symbol=symbol)
        result["data_timestamp"] = (
            item.get("FEGMRQ") if item.get("FEGMRQ") not in ("", "--") else None
        )
        result["data"] = data
        result["name"] = data["name"]
        result["unavailable"] = ["last_trading_date"]
        return result

    def sina():
        text = get_text("https://hq.sinajs.cn/list=f_" + symbol)
        match = re.search(r'="([^"]+)"', text)
        fields = match.group(1).split(",") if match else []
        if len(fields) < 5:
            raise DataError("Sina fund snapshot unavailable")
        result = _result("sina", [], symbol=symbol)
        result["last_trading_date"] = fields[4]
        result["data_timestamp"] = fields[4]
        result["data"] = {
            "symbol": symbol,
            "name": fields[0],
            "nav": _number(fields[1]),
            "accumulated_nav": _number(fields[2]),
            "date": fields[4],
        }
        result["name"] = fields[0]
        result["unavailable"] = [
            "fund_type",
            "manager",
            "company",
            "benchmark",
            "net_assets",
            "fees",
        ]
        return result

    return _attempt([("Eastmoney fund profile", mobile), ("Sina fund snapshot", sina)])
