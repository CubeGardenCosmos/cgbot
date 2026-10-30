"""Recorded payload fragments from public sources, captured 2026-10-27.

No test makes network requests. Pagination cases expand the recorded shape.
"""

import importlib
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from findata.errors import DataError

funds = importlib.import_module("findata.funds")
overview = importlib.import_module("findata.overview")

NAV = {"FSRQ": "2026-10-26", "DWJZ": "1.0190", "LJJZ": "3.5920", "JZZZL": "-2.58", "NAVTYPE": "1"}
RANK = {
    "FCODE": "013835",
    "SHORTNAME": "中加优享纯债债券C",
    "FSRQ": "2026-10-26",
    "DWJZ": "3.2343",
    "SYL_1N": "227.19",
}
WEB_RANK = (
    "013835,中加优享纯债债券C,ZJYXCZZQC,2026-10-26,3.2343,3.2343,"
    "0.01,0.04,222.17,223.11,223.98,227.19,225.22,225.55,227.46,223.43"
)
INDEX = {
    "f2": 3648.30,
    "f3": -1.33,
    "f4": -49.22,
    "f6": 672414398244.3,
    "f12": "000001",
    "f14": "上证指数",
    "f104": 291,
    "f105": 2037,
    "f124": 1793088720,
}
SINA_INDUSTRY = (
    "var S_Finance_bankuai_sinaindustry = "
    '{"new_glql":"new_glql,公路桥梁,20,6.7672222222222,0.0083333333333333,'
    '0.1232944270919,190000000,1547937768,sh600000,0,0,0,名称",'
    '"new_sybh":"new_sybh,商业百货,40,8.8174390243902,-0.017560975609756,'
    '-0.19876599445112,10000000,17231508785,sh600000,0,0,0,名称"};'
)


class FundTests(unittest.TestCase):
    def test_recorded_nav(self):
        with patch.object(
            funds, "get_json", return_value={"Data": {"LSJZList": [NAV]}, "TotalCount": 1}
        ):
            result = funds.nav("000001")
        self.assertEqual(result["data"][0]["nav"], 1.019)
        self.assertEqual(result["data"][0]["accumulated_nav"], 3.592)
        self.assertEqual(result["last_trading_date"], "2026-10-26")
        self.assertEqual(result["data_timestamp"], "2026-10-26")
        self.assertIn("retrieved_at", result)

    def test_nav_pagination_keeps_fixed_page_size(self):
        days = [(datetime(2026, 10, 26) - timedelta(days=i)).date().isoformat() for i in range(105)]
        records = [dict(NAV, FSRQ=day) for day in days]
        responses = [
            {"Data": {"LSJZList": records[:100]}, "TotalCount": 105},
            {"Data": {"LSJZList": records[100:]}, "TotalCount": 105},
        ]
        with patch.object(funds, "get_json", side_effect=responses) as fetch:
            result = funds.nav("000001", limit=103)
        self.assertEqual(len(result["data"]), 103)
        self.assertEqual([call.args[1]["pageIndex"] for call in fetch.call_args_list], [1, 2])
        self.assertEqual([call.args[1]["pageSize"] for call in fetch.call_args_list], [100, 100])
        self.assertEqual(result["data"][-1]["date"], days[0])

    def test_chart_fallback_uses_china_dates(self):
        # Chart timestamps are midnight Asia/Shanghai, not midnight UTC.
        epoch = int(datetime.fromisoformat("2026-10-26T00:00:00+08:00").timestamp() * 1000)
        chart = (
            'var fS_code="000001"; var Data_netWorthTrend='
            + json.dumps([{"x": epoch, "y": 1.019, "equityReturn": -2.58}])
            + ";var Data_ACWorthTrend="
            + json.dumps([[epoch, 3.592]])
            + ";"
        )
        with (
            patch.object(funds, "get_json", side_effect=DataError("outage")),
            patch.object(funds, "get_text", return_value=chart),
        ):
            result = funds.nav("000001", start="2026-10-26", end="2026-10-26")
        self.assertEqual(result["source"], "eastmoney_pingzhongdata")
        self.assertEqual(result["data"][0]["date"], "2026-10-26")
        self.assertEqual(result["data"][0]["accumulated_nav"], 3.592)

    def test_rank_parameters_and_mobile_page_cap(self):
        responses = [
            {"Datas": [RANK] * 30, "TotalCount": 100},
            {"Datas": [RANK] * 30, "TotalCount": 100},
        ]
        with patch.object(funds, "get_json", side_effect=responses) as fetch:
            result = funds.rank(period="1y", fund_type="stock", limit=35)
        self.assertEqual(len(result["data"]), 35)
        params = fetch.call_args_list[0].args[1]
        self.assertEqual(params["SortColumn"], "SYL_1N")
        self.assertEqual(params["Sort"], "desc")
        self.assertEqual(params["FundType"], "25")
        self.assertEqual(params["pageSize"], 30)
        self.assertEqual(fetch.call_args_list[1].args[1]["pageIndex"], 2)

    def test_rank_web_fallback(self):
        text = "var rankData = {datas:" + json.dumps([WEB_RANK]) + ",allRecords:1};"
        with (
            patch.object(funds, "get_json", side_effect=DataError("outage")),
            patch.object(funds, "get_text", return_value=text),
        ):
            result = funds.rank(limit=1)
        self.assertEqual(result["source"], "eastmoney_rankhandler")
        self.assertEqual(result["data"][0]["return_pct"], 227.19)

    def test_rank_unknown_date_is_null(self):
        with patch.object(
            funds, "get_json", return_value={"Datas": [dict(RANK, FSRQ="--")], "TotalCount": 1}
        ):
            result = funds.rank(limit=1)
        self.assertIsNone(result["last_trading_date"])
        self.assertIsNone(result["data"][0]["date"])

    def test_recorded_profile_preserves_basic_fields(self):
        item = {
            "FCODE": "000001",
            "SHORTNAME": "华夏成长混合",
            "FULLNAME": "华夏成长证券投资基金",
            "FTYPE": "混合型-灵活",
            "JJGS": "华夏基金",
            "JJJL": "郑晓辉,刘睿聪",
            "NETNAV": "3236830105.93",
            "ENDNAV": "2918669087.76",
            "FEGMRQ": "2026-09-30",
        }
        with patch.object(funds, "get_json", return_value={"Datas": item}):
            result = funds.info("000001")
        self.assertEqual(result["data"]["name"], "华夏成长混合")
        self.assertEqual(result["name"], "华夏成长混合")
        self.assertEqual(result["data"]["company"], "华夏基金")
        self.assertEqual(result["data"]["net_assets"], 2918669087.76)
        self.assertEqual(result["data"]["assets_date"], "2026-09-30")
        self.assertEqual(result["data_timestamp"], "2026-09-30")
        self.assertIsNone(result["last_trading_date"])

    def test_info_sina_fallback(self):
        text = 'var hq_str_f_000001="华夏成长混合A,1.019,3.592,1.046,2026-10-26,23.8905";'
        with (
            patch.object(funds, "get_json", return_value={"Datas": None}),
            patch.object(funds, "get_text", return_value=text),
        ):
            result = funds.info("000001")
        self.assertEqual(result["source"], "sina")
        self.assertEqual(result["data"]["nav"], 1.019)
        self.assertIn("manager", result["unavailable"])

    def test_invalid_input_and_failed_sources_raise(self):
        for kwargs in (
            {"limit": 0},
            {"limit": True},
            {"start": "2026-13-01"},
            {"start": "2026-10-27", "end": "2026-10-26"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(DataError):
                funds.nav("000001", **kwargs)
        with self.assertRaises(DataError):
            funds.info("1")
        with (
            patch.object(funds, "get_json", side_effect=DataError("outage")),
            patch.object(funds, "get_text", return_value="<html>blocked</html>"),
        ):
            with self.assertRaises(DataError):
                funds.rank()


class OverviewTests(unittest.TestCase):
    def test_index_units_and_breadth(self):
        records = [dict(INDEX, f12=secid.split(".")[1]) for secid, _, _ in overview._INDICES]
        with patch.object(overview, "get_json", return_value={"data": {"diff": records}}):
            result = overview.overview()
        self.assertEqual(len(result["data"]), 5)
        row = result["data"][0]
        self.assertEqual(row["price"], 3648.3)
        self.assertEqual(row["change_pct"], -1.33)
        self.assertEqual(row["advance_decline"], -1746)
        self.assertEqual(row["turnover"], 672414398244.3)

    def test_tencent_fallback_missing_breadth(self):
        # Fields 30/31/32/37 are timestamp/change/change-percent/turnover(万元).
        fields = ["0"] * 38
        for index, value in {
            1: "上证指数",
            3: "3648.30",
            30: "20261027161400",
            31: "-49.22",
            32: "-1.33",
            37: "67241440",
        }.items():
            fields[index] = value
        text = "\n".join(
            "v_" + symbol + '="' + "~".join(fields) + '";' for _, symbol, _ in overview._INDICES
        )
        with (
            patch.object(overview, "get_json", return_value={"data": None}),
            patch.object(overview, "get_text", return_value=text),
        ):
            result = overview.overview()
        self.assertEqual(result["source"], "tencent")
        self.assertEqual(result["data"][0]["turnover"], 672414400000)
        self.assertIsNone(result["data"][0]["advances"])
        self.assertIn("advances", result["data"][0]["unavailable"])

    def test_sector_pagination_and_direction(self):
        responses = [
            {"data": {"total": 105, "diff": [INDEX] * 100}},
            {"data": {"total": 105, "diff": [INDEX] * 5}},
        ]
        with patch.object(overview, "get_json", side_effect=responses) as fetch:
            result = overview.sectors(direction="bottom", limit=103)
        self.assertEqual(len(result["data"]), 103)
        self.assertEqual(fetch.call_args_list[0].args[1]["po"], 0)
        self.assertEqual(fetch.call_args_list[1].args[1]["pn"], 2)

    def test_sina_industry_fallback_marks_unavailable_timestamp(self):
        with (
            patch.object(overview, "get_json", side_effect=DataError("outage")),
            patch.object(overview, "get_text", return_value=SINA_INDUSTRY),
        ):
            result = overview.sectors(direction="bottom", limit=1)
        self.assertEqual(result["source"], "sina")
        self.assertEqual(result["data"][0]["name"], "商业百货")
        self.assertIsNone(result["data_timestamp"])
        self.assertIsNone(result["last_trading_date"])

    def test_recorded_sina_concept_fallback(self):
        text = (
            "var S_Finance_bankuai_class = "
            '{"gn_hwqc":"gn_hwqc,华为汽车,97,21.361979166667,-0.61177083333333,'
            '-2.784098450803,1706883117,26412285389,sz002284,10.011,9.670,0.880,亚太股份"};'
        )
        with (
            patch.object(overview, "get_json", side_effect=DataError("outage")),
            patch.object(overview, "get_text", return_value=text) as fetch,
        ):
            result = overview.sectors(kind="concept")
        self.assertEqual(result["data"][0]["name"], "华为汽车")
        self.assertEqual(result["data"][0]["change_pct"], -2.784098450803)
        self.assertIn("param=class", fetch.call_args.args[0])
