"""Recorded public responses and provider failure contracts; no network calls."""

from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from findata.errors import DataError
from findata import fetch_many, markets
from findata.symbols import resolve

# Tencent observations, 2026-10-27. OHLC position is open/close/high/low.
CN_RECORDING = {
    "code": 0,
    "data": {
        "sh600519": {
            "qfqday": [
                ["2026-10-26", "1325.400", "1349.980", "1350.000", "1324.790", "41599.000"],
                ["2026-10-27", "1343.88", "1355.90", "1357.00", "1338.01", "38669"],
            ]
        }
    },
}
HK_RECORDING = {
    "code": 0,
    "data": {
        "hk00700": {
            "qfqday": [
                [
                    "2026-10-27",
                    "387.000",
                    "385.200",
                    "390.600",
                    "383.600",
                    "16230914.000",
                    {},
                    "0.180",
                    "627074.675",
                ],
            ]
        }
    },
}
# Yahoo chart values captured 2026-10-27; synthetic adjusted close exercises ratio.
YAHOO_RECORDING = {
    "chart": {
        "error": None,
        "result": [
            {
                "timestamp": [1793021400],
                "meta": {
                    "symbol": "AAPL",
                    "regularMarketTime": 1793044801,
                    "regularMarketPrice": 343.91,
                },
                "indicators": {
                    "quote": [
                        {
                            "open": [343.9200134277344],
                            "high": [347.0899963378906],
                            "low": [341.0299987792969],
                            "close": [343.9100036621094],
                            "volume": [42667300],
                        }
                    ],
                    "adjclose": [{"adjclose": [343.9100036621094]}],
                },
            }
        ],
    }
}

# Synthetic daily regression fixture: the first bucket must not begin at the
# user's mid-bucket start. Values follow the recorded Stooq CSV field order.
JANUARY_BARS = [
    ("2024-01-02", 10, 15, 8, 12, 100),
    ("2024-01-12", 12, 18, 11, 16, 200),
    ("2024-01-16", 16, 20, 15, 18, 300),
    ("2024-01-19", 18, 22, 17, 20, 400),
    ("2024-01-31", 20, 25, 19, 23, 500),
]


class SymbolTests(unittest.TestCase):
    def test_requested_identifiers(self):
        for query, code, market in [
            ("тАЛ600519", "600519", "CN"),
            ("贵州茅台", "600519", "CN"),
            ("AAPL", "AAPL", "US"),
            ("00700", "00700", "HK"),
        ]:
            with self.subTest(query=query):
                item = resolve(query)[0]
                self.assertEqual((item["symbol"], item["market"]), (code, market))
                self.assertIn("name", item)
                self.assertIn("kind", item)

    def test_ambiguous_cn_code(self):
        self.assertEqual(resolve("000001")[0]["kind"], "stock")
        self.assertEqual(resolve("sh000001")[0]["kind"], "index")
        self.assertEqual(resolve("000001.SS")[0]["exchange"], "sh")

    def test_recorded_name_search(self):
        text = r'v_hint="sz~000001~\u5e73\u5b89\u94f6\u884c~payh~GP-A"'
        with patch("findata.symbols.get_text", return_value=text):
            self.assertEqual(resolve("平安银行")[0]["name"], "平安银行")
            self.assertEqual(resolve("平安银行", market="HK"), [])

    def test_eastmoney_search_fallback(self):
        payload = {
            "QuotationCodeTable": {
                "Data": [
                    {
                        "Code": "000001",
                        "Name": "平安银行",
                        "QuoteID": "0.000001",
                        "SecurityTypeName": "深A",
                    }
                ]
            }
        }
        with (
            patch("findata.symbols.get_text", side_effect=DataError("unavailable")),
            patch("findata.symbols.get_json", return_value=payload),
        ):
            self.assertEqual(resolve("平安银行")[0]["symbol"], "000001")

    def test_us_search_normalizes_provider_suffix(self):
        with patch("findata.symbols.get_text", return_value='v_hint="us~baba.n~Alibaba~baba~GP"'):
            self.assertEqual(resolve("阿里巴巴")[0]["symbol"], "BABA")

    def test_invalid_market(self):
        with self.assertRaises(DataError):
            resolve("600519", market="EU")


class ParserTests(unittest.TestCase):
    def test_tencent_normalizes_cn_lots_and_hk_shares(self):
        cn = markets._parse_tencent(CN_RECORDING, "sh600519", "daily", "qfq", resolve("600519")[0])
        self.assertEqual(
            cn[-1],
            dict(
                date="2026-10-27",
                open=1343.88,
                close=1355.9,
                high=1357.0,
                low=1338.01,
                volume=3866900.0,
            ),
        )
        hk = markets._parse_tencent(HK_RECORDING, "hk00700", "daily", "qfq", resolve("00700")[0])
        self.assertEqual(hk[-1]["volume"], 16230914)

    def test_eastmoney_optional_amount_and_percent_change(self):
        payload = {
            "data": {
                "klines": [
                    "2026-10-26,1325.4,1349.98,1350,1324.79,41599,5600000000,1.12",
                    "2026-10-27,1343.88,1355.90,1357,1338.01,38669,5226798679,0.44",
                ]
            }
        }
        with patch.object(markets, "get_json", return_value=payload) as fetch:
            rows, _ = markets._eastmoney_history(
                resolve("600519")[0], "daily", None, None, "qfq", 2
            )
        self.assertEqual(rows[-1]["amount"], 5226798679)
        self.assertEqual(rows[-1]["pct_chg"], 0.44)
        self.assertTrue(fetch.call_args.args[1]["fields2"].endswith("f57,f59"))
        weekly = markets._aggregate(rows, "weekly")[0]
        self.assertEqual(weekly["amount"], 10826798679)
        self.assertNotIn("pct_chg", weekly)
        del rows[-1]["amount"]
        self.assertNotIn("amount", markets._aggregate(rows, "weekly")[0])

    def test_tencent_bad_data_and_missing_adjustment(self):
        for payload in [{"code": 1, "data": []}, {"data": {"sh600519": {"day": [["2026-10-27"]]}}}]:
            with self.assertRaises(DataError):
                markets._parse_tencent(payload, "sh600519", "daily", "qfq", resolve("600519")[0])

    def test_yahoo_exchange_date_and_adjustment(self):
        rows, _ = markets._parse_yahoo(YAHOO_RECORDING, resolve("AAPL")[0], "qfq")
        self.assertEqual(rows[0]["date"], "2026-10-26")
        self.assertTrue(rows[0]["timestamp"].endswith("-04:00"))
        self.assertAlmostEqual(rows[0]["close"], 343.9100036621094)
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1793021400],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100],
                                    "close": [110],
                                    "high": [120],
                                    "low": [90],
                                    "volume": [10],
                                }
                            ],
                            "adjclose": [{"adjclose": [55]}],
                        },
                    }
                ]
            }
        }
        rows, _ = markets._parse_yahoo(payload, resolve("AAPL")[0], "qfq")
        self.assertEqual(
            (rows[0]["open"], rows[0]["close"], rows[0]["high"], rows[0]["low"]), (50, 55, 60, 45)
        )

    def test_period_aggregation_uses_last_actual_date(self):
        rows = [
            dict(
                date="2026-10-26",
                open=1,
                close=2,
                high=3,
                low=1,
                volume=10,
                timestamp="2026-10-26T09:30:00-04:00",
            ),
            dict(
                date="2026-10-27",
                open=2,
                close=4,
                high=5,
                low=2,
                volume=20,
                timestamp="2026-10-27T09:30:00-04:00",
            ),
        ]
        for period in ["weekly", "monthly"]:
            result = markets._aggregate(rows, period)[0]
            self.assertEqual(
                (result["date"], result["open"], result["close"], result["volume"]),
                ("2026-10-27", 1, 4, 30),
            )
            self.assertTrue(result["timestamp"].startswith(result["date"]))

    def test_stooq_csv_parser(self):
        text = "Date,Open,High,Low,Close,Volume\n2026-10-26,343.92,347.09,341.03,343.91,42667300\n"
        with patch.object(markets, "get_text", return_value=text):
            rows, source = markets._stooq_history(resolve("AAPL")[0], "daily", None, None, "qfq", 1)
        self.assertEqual(source, "stooq")
        self.assertEqual(rows[0]["close"], 343.91)
        with patch.object(markets, "get_text", return_value="<html>challenge</html>"):
            with self.assertRaises(DataError):
                markets._stooq_history(resolve("AAPL")[0], "daily", None, None, "qfq", 1)

    def test_us_daily_sources_include_full_first_aggregation_bucket(self):
        cases = [
            (
                "monthly",
                "2024-01-15",
                "2024-01-31",
                "2024-01-01",
                ("2024-01-31", 10, 25, 8, 23, 1500),
            ),
            (
                "weekly",
                "2024-01-17",
                "2024-01-19",
                "2024-01-15",
                ("2024-01-19", 16, 22, 15, 20, 700),
            ),
            (
                "monthly",
                "2024-01-15",
                "2024-01-19",
                "2024-01-01",
                ("2024-01-19", 10, 22, 8, 20, 1000),
            ),
            (
                "daily",
                "2024-01-17",
                "2024-01-19",
                "2024-01-17",
                ("2024-01-19", 18, 22, 17, 20, 400),
            ),
        ]
        for provider in ("yahoo", "stooq"):
            for period, start, end, expected_begin, expected in cases:
                with self.subTest(provider=provider, period=period, end=end):
                    requests = []

                    def yahoo_response(url, params):
                        begin = (
                            datetime.fromtimestamp(params["period1"], timezone.utc)
                            .date()
                            .isoformat()
                        )
                        exclusive_end = (
                            datetime.fromtimestamp(params["period2"], timezone.utc)
                            .date()
                            .isoformat()
                        )
                        requests.append(begin)
                        bars = [bar for bar in JANUARY_BARS if begin <= bar[0] < exclusive_end]
                        quote = {
                            key: [bar[index] for bar in bars]
                            for key, index in [
                                ("open", 1),
                                ("high", 2),
                                ("low", 3),
                                ("close", 4),
                                ("volume", 5),
                            ]
                        }
                        return {
                            "chart": {
                                "result": [
                                    {
                                        "timestamp": [
                                            int(
                                                datetime.fromisoformat(
                                                    bar[0] + "T14:30:00+00:00"
                                                ).timestamp()
                                            )
                                            for bar in bars
                                        ],
                                        "indicators": {
                                            "quote": [quote],
                                            "adjclose": [{"adjclose": quote["close"]}],
                                        },
                                    }
                                ]
                            }
                        }

                    def stooq_response(url, params):
                        begin = datetime.strptime(params["d1"], "%Y%m%d").date().isoformat()
                        final = datetime.strptime(params["d2"], "%Y%m%d").date().isoformat()
                        requests.append(begin)
                        bars = [bar for bar in JANUARY_BARS if begin <= bar[0] <= final]
                        return "Date,Open,High,Low,Close,Volume\n" + "\n".join(
                            ",".join(map(str, bar)) for bar in bars
                        )

                    with (
                        patch.object(
                            markets,
                            "get_json",
                            side_effect=yahoo_response
                            if provider == "yahoo"
                            else DataError("unavailable"),
                        ),
                        patch.object(markets, "get_text", side_effect=stooq_response),
                    ):
                        result = markets.kline("AAPL", period=period, start=start, end=end)
                    self.assertEqual(requests, [expected_begin])
                    self.assertEqual(result["source"], provider)
                    self.assertEqual(len(result["data"]), 1)
                    row = result["data"][0]
                    self.assertEqual(
                        tuple(
                            row[key] for key in ("date", "open", "high", "low", "close", "volume")
                        ),
                        expected,
                    )

    def test_yahoo_history_rejects_raw_but_quote_keeps_current_ohlc(self):
        info = resolve("AAPL")[0]
        with patch.object(markets, "get_json", return_value=YAHOO_RECORDING) as fetch:
            with self.assertRaisesRegex(DataError, "already split-adjusted"):
                markets._yahoo_history(info, "daily", None, None, "none", 1)
            fetch.assert_not_called()
            result = markets._yahoo_quote(info)
        self.assertEqual(
            result["open"],
            YAHOO_RECORDING["chart"]["result"][0]["indicators"]["quote"][0]["open"][0],
        )

    def test_yahoo_date_arithmetic_errors_are_data_errors(self):
        cases = [
            (None, date.max, 1),
            (None, date(2024, 1, 31), 10**100),
            (None, date(2024, 1, 31), 1000000),
            (None, date.min, 1),
        ]
        for start, end, limit in cases:
            with self.subTest(end=end, limit=limit), patch.object(markets, "get_json") as fetch:
                with self.assertRaisesRegex(DataError, "date range is not representable"):
                    markets._yahoo_history(resolve("AAPL")[0], "monthly", start, end, "qfq", limit)
                fetch.assert_not_called()

    def test_yahoo_unrepresentable_response_timestamp_falls_back_host(self):
        malformed = {
            "chart": {
                "result": [
                    {
                        "timestamp": [10**100],
                        "indicators": {
                            "quote": [
                                {"open": [1], "close": [1], "high": [1], "low": [1], "volume": [1]}
                            ],
                            "adjclose": [{"adjclose": [1]}],
                        },
                    }
                ]
            }
        }
        with patch.object(markets, "get_json", side_effect=[malformed, YAHOO_RECORDING]) as fetch:
            rows, source = markets._yahoo_history(
                resolve("AAPL")[0], "daily", date(2026, 10, 26), date(2026, 10, 27), "qfq", 1
            )
        self.assertEqual((source, rows[0]["date"]), ("yahoo", "2026-10-26"))
        self.assertEqual(fetch.call_count, 2)


class FallbackTests(unittest.TestCase):
    def test_kline_falls_back_and_filters_sorts_deduplicates(self):
        rows = [
            dict(date=day, open=1, close=1, high=1, low=1, volume=1)
            for day in ["2026-10-27", "2026-10-23", "2026-10-26", "2026-10-26"]
        ]
        with (
            patch.object(markets, "_tencent_history", side_effect=DataError("unavailable")),
            patch.object(markets, "_eastmoney_history", return_value=(rows, "eastmoney")),
        ):
            result = markets.kline("600519", start="2026-10-24", end="2026-10-27", limit=1)
        self.assertEqual(result["source"], "eastmoney")
        self.assertEqual([r["date"] for r in result["data"]], ["2026-10-27"])
        self.assertEqual(result["last_trading_date"], "2026-10-27")

    def test_us_stooq_fallback(self):
        rows = [dict(date="2026-10-26", open=1, close=1, high=1, low=1, volume=1)]
        with (
            patch.object(markets, "_yahoo_history", side_effect=DataError("rate limited")),
            patch.object(markets, "_stooq_history", return_value=(rows, "stooq")),
        ):
            self.assertEqual(markets.kline("AAPL")["source"], "stooq")

    def test_quote_fallback(self):
        expected = {
            "source": "sina",
            "price": 1355.9,
            "data_timestamp": "2026-10-27T15:00:00+08:00",
        }
        with (
            patch.object(markets, "get_text", side_effect=DataError("unavailable")),
            patch.object(markets, "_sina_quote", return_value=expected),
        ):
            self.assertEqual(markets.quote("600519"), expected)

    def test_us_quote_stooq_fallback_is_labeled_daily_close(self):
        rows = [
            dict(date="2026-10-23", open=340, close=347.73, high=350, low=340, volume=10),
            dict(date="2026-10-26", open=343.92, close=343.91, high=347.09, low=341.03, volume=20),
        ]
        with (
            patch.object(markets, "_yahoo_quote", side_effect=DataError("rate limited")),
            patch.object(markets, "_stooq_history", return_value=(rows, "stooq")),
        ):
            result = markets.quote("AAPL")
        self.assertEqual((result["source"], result["quote_type"]), ("stooq", "daily_close"))
        self.assertEqual(result["data_timestamp"], "2026-10-26")
        self.assertEqual(result["price"], 343.91)

    def test_tencent_backward_pagination(self):
        from datetime import date, timedelta

        newest = date(2026, 10, 27)
        bars = [
            [(newest - timedelta(days=i)).isoformat(), "1", "2", "3", "0.5", "10"]
            for i in range(700)
        ]
        pages = [
            {"data": {"sh600519": {"qfqday": bars[:640]}}},
            {"data": {"sh600519": {"qfqday": bars[640:]}}},
        ]
        with patch.object(markets, "get_json", side_effect=pages) as fetch:
            result = markets.kline("600519", limit=700)
        self.assertEqual(len(result["data"]), 700)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("2025-01-25", fetch.call_args.args[1]["param"])
        self.assertEqual(result["data"], sorted(result["data"], key=lambda r: r["date"]))

    def test_hk_unadjusted_uses_fqkline(self):
        payload = {
            "data": {
                "hk00700": {"day": [["2026-10-27", "387", "385.2", "390.6", "383.6", "16230914"]]}
            }
        }
        with patch.object(markets, "get_json", return_value=payload) as fetch:
            markets.kline("00700", adjust="none", limit=1)
        self.assertIn("/fqkline/", fetch.call_args.args[0])

    def test_us_rejects_none_and_hfq_without_fetching(self):
        for symbol in ("AAPL", "^GSPC"):
            for adjust in ("none", "hfq"):
                with (
                    self.subTest(symbol=symbol, adjust=adjust),
                    patch.object(markets, "get_json") as json_fetch,
                    patch.object(markets, "get_text") as text_fetch,
                ):
                    with self.assertRaisesRegex(DataError, "US history supports qfq only"):
                        markets.kline(symbol, adjust=adjust)
                    json_fetch.assert_not_called()
                    text_fetch.assert_not_called()

    def test_history_provider_overflow_is_fallback_error(self):
        rows = [dict(date="2026-10-26", open=1, close=1, high=1, low=1, volume=1)]
        for failure in (OverflowError("out of range"), OSError("timestamp out of range")):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(markets, "_yahoo_history", side_effect=failure),
                patch.object(markets, "_stooq_history", return_value=(rows, "stooq")),
            ):
                self.assertEqual(markets.kline("AAPL")["source"], "stooq")

    def test_extreme_history_dates_and_limits_do_not_abort_batch(self):
        for options in (dict(end="9999-12-31"), dict(limit=10**100), dict(end="0001-01-01")):
            with (
                self.subTest(options=options),
                patch.object(markets, "_stooq_history", side_effect=DataError("Stooq unavailable")),
                patch.object(markets, "get_json") as fetch,
            ):
                results = fetch_many(["AAPL", "MSFT"], feature="kline", **options)
            fetch.assert_not_called()
            self.assertEqual([item["symbol"] for item in results], ["AAPL", "MSFT"])
            for item in results:
                self.assertEqual(item["error"]["type"], "DataError")
                self.assertIn("date range is not representable", item["error"]["message"])

    def test_validation(self):
        for options in [
            dict(start="2026-02-30"),
            dict(start="2026-10-27", end="2026-10-01"),
            dict(limit=0),
            dict(limit=True),
            dict(period="hourly"),
            dict(adjust="bogus"),
        ]:
            with self.subTest(options=options), self.assertRaises(DataError):
                markets.kline("600519", **options)


if __name__ == "__main__":
    unittest.main()
