"""Mainland index snapshots and provider-defined sector rankings."""

import json
import re
from datetime import datetime, timezone, timedelta

from .errors import DataError
from .transport import get_json, get_text

_CST = timezone(timedelta(hours=8))
_INDICES = [
    ("1.000001", "sh000001", "上证"),
    ("0.399001", "sz399001", "深证"),
    ("0.399006", "sz399006", "创业板"),
    ("1.000688", "sh000688", "科创50"),
    ("1.000300", "sh000300", "沪深300"),
]


def _number(value):
    return None if value in (None, "", "-", "--") else float(value)


def _stamp(epoch):
    return datetime.fromtimestamp(float(epoch), _CST).isoformat() if epoch else None


def _result(source, rows, **extra):
    stamps = [r["timestamp"] for r in rows if r.get("timestamp")]
    return dict(
        source=source,
        data_timestamp=max(stamps) if stamps else None,
        last_trading_date=max(stamps)[:10] if stamps else None,
        data=rows,
        **extra,
    )


def _fallback(providers):
    failures = []
    for label, provider in providers:
        try:
            return provider()
        except (DataError, ValueError, KeyError, TypeError, IndexError) as exc:
            failures.append(f"{label}: {exc}")
    raise DataError("; ".join(failures))


def _east_row(item):
    advances, declines = _number(item.get("f104")), _number(item.get("f105"))
    return {
        "symbol": item["f12"],
        "name": item["f14"],
        "price": _number(item.get("f2")),
        "change_pct": _number(item.get("f3")),
        "change": _number(item.get("f4")),
        "turnover": _number(item.get("f6")),
        "advances": advances,
        "declines": declines,
        "advance_decline": (advances - declines)
        if advances is not None and declines is not None
        else None,
        "timestamp": _stamp(item.get("f124")),
    }


def overview():
    """Return five index quotes; turnover is CNY, breadth is provider-defined."""

    def eastmoney():
        payload = get_json(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            {
                "secids": ",".join(x[0] for x in _INDICES),
                "fltt": 2,
                "fields": "f2,f3,f4,f6,f12,f14,f104,f105,f124",
            },
        )
        records = (payload.get("data") or {}).get("diff") or []
        if isinstance(records, dict):
            records = list(records.values())
        by_code = {row["f12"]: row for row in records}
        rows = []
        for secid, symbol, label in _INDICES:
            row = _east_row(by_code[secid.split(".")[1]])
            if row["price"] is None:
                raise DataError("Missing index quote")
            row.update(symbol=symbol, label=label)
            row["unavailable"] = [
                key for key in ("change", "turnover", "advances", "declines") if row[key] is None
            ]
            rows.append(row)
        return _result(
            "eastmoney",
            rows,
            breadth_scope="Provider-defined index/market universe, not necessarily index constituents",
        )

    def tencent():
        text = get_text("https://qt.gtimg.cn/q=" + ",".join(x[1] for x in _INDICES))
        quotes = dict(re.findall(r'v_(\w+)="([^"]*)"', text))
        rows = []
        for _, symbol, label in _INDICES:
            fields = quotes[symbol].split("~")
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=_CST).isoformat()
            row = {
                "symbol": symbol,
                "name": fields[1],
                "label": label,
                "price": _number(fields[3]),
                "change": _number(fields[31]),
                "change_pct": _number(fields[32]),
                "turnover": _number(fields[37]) * 10000
                if _number(fields[37]) is not None
                else None,
                "advances": None,
                "declines": None,
                "advance_decline": None,
                "timestamp": stamp,
                "unavailable": ["advances", "declines", "advance_decline"],
            }
            if row["price"] is None:
                raise DataError("Missing Tencent index quote")
            rows.append(row)
        return _result("tencent", rows)

    return _fallback([("Eastmoney indices", eastmoney), ("Tencent indices", tencent)])


def sectors(kind="industry", direction="top", limit=10):
    """Rank industry or concept boards by daily percentage change.

    Sector classifications differ between vendors. Sina industry fallback is
    not a claim that its sector baskets match Eastmoney's classification.
    """
    kind = {"sector": "industry", "concepts": "concept"}.get(kind, kind)
    if kind not in ("industry", "concept") or direction not in ("top", "bottom"):
        raise DataError("kind must be industry/concept; direction must be top/bottom")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise DataError("limit must be an integer between 1 and 10000")

    def eastmoney():
        rows, page = [], 1
        while len(rows) < limit:
            payload = get_json(
                "https://push2.eastmoney.com/api/qt/clist/get",
                {
                    "pn": page,
                    "pz": 100,
                    "po": 1 if direction == "top" else 0,
                    "np": 1,
                    "fltt": 2,
                    "fid": "f3",
                    "fs": "m:90+t:" + ("2" if kind == "industry" else "3"),
                    "fields": "f2,f3,f4,f6,f12,f14,f104,f105,f124",
                },
            )
            data = payload.get("data") or {}
            records = data.get("diff") or []
            if isinstance(records, dict):
                records = list(records.values())
            rows.extend(_east_row(record) for record in records)
            if not records or len(rows) >= int(data.get("total", len(rows))):
                break
            page += 1
        if not rows:
            raise DataError("No sector quotes returned")
        return _result("eastmoney", rows[:limit], kind=kind, direction=direction)

    def sina():
        url = (
            "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
            if kind == "industry"
            else "https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"
        )
        text = get_text(url)
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", text, re.S)
        if not match:
            raise DataError("Missing Sina sector data")
        records = json.loads(match.group(1))
        rows = []
        for value in records.values():
            fields = value.split(",")
            if len(fields) < 8:
                raise DataError("Malformed Sina sector record")
            change = _number(fields[5])
            if change is None:
                continue
            rows.append(
                {
                    "symbol": fields[0],
                    "name": fields[1],
                    "price": _number(fields[3]),
                    "change": _number(fields[4]),
                    "change_pct": change,
                    "turnover": _number(fields[7]),
                    "advances": None,
                    "declines": None,
                    "advance_decline": None,
                    "timestamp": None,
                    "unavailable": ["timestamp", "advances", "declines", "advance_decline"],
                }
            )
        if not rows:
            raise DataError("No Sina sector data")
        rows.sort(key=lambda r: r["change_pct"], reverse=direction == "top")
        return _result(
            "sina",
            rows[:limit],
            kind=kind,
            direction=direction,
            caveat="Sina sector baskets differ from Eastmoney; source supplies no quote timestamp",
        )

    return _fallback([("Eastmoney sectors", eastmoney), ("Sina sectors", sina)])
