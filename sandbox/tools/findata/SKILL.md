---
name: findata
description: Fetch normalized real CN/HK/US quotes, OHLCV, A-share breadth/sectors and public fund data before writing market reports; JSON CLI and Python batch API.
---

# Financial data

Use this skill before researching stocks, indices, market conditions or funds. `findata` is preinstalled; do not rediscover quote endpoints with hand-written HTTP probes. Fetching is separate from the offline rich-report renderer.

## Rules

1. **Always obtain real OHLCV from findata for every K-line.** Never invent prices, volume, NAV, breadth or returns. If both sources fail, state the limitation, do not substitute a simulated chart. For stock/market reports include daily K-lines for every index/stock discussed (normally at least 60 bars).
2. **One market per report task.** Split A-share, HK and US analysis into separate report tasks; do not mix session dates, currency or trading calendars.
3. State `source`, `last_trading_date`, `data_timestamp`, interval, adjustment, timezone, currency and volume units from actual output. Retrieval time is not a trading timestamp. A weekend/holiday's last close must not be described as today's trading. Delayed snapshots are not guaranteed real-time exchange feeds.
4. Keep null/unavailable fields unavailable. A fallback may have fewer fields or different delay; disclose its source. Do not extrapolate advance/decline counts from a benchmark's constituents.
5. Use `batch` or `fetch_many` instead of serial symbol probing. Keep concurrency at the default six or lower if a source throttles. Public sources have no SLA; avoid repeated bulk full-history downloads.
6. K-line rows are chronological numeric OHLCV. Do not round/copy prices manually. Preserve adjustment basis. For rich-report use green-up/red-down (`colors: "intl"`) unless explicitly requested otherwise.

## Command catalogue

JSON is compact by default. Add `--pretty` for human inspection. Errors are JSON with a nonzero exit code; batch retains individual errors alongside successful symbols.

```sh
findata --help
findata resolve '贵州茅台'
findata resolve 'тАЛ600519' --market CN
findata resolve 00700 --market HK
findata quote 600519 --market CN --pretty
findata quote 00700 --market HK
findata quote AAPL --market US
findata kline 600519 --market CN --period daily --adjust qfq --limit 120 --out /workspace/artifacts/600519.json
findata kline 00700 --market HK --period weekly --limit 100
findata kline AAPL --market US --period monthly --start 2020-01-01 --end 2025-12-31 --limit 100
findata overview
findata sectors --kind industry --direction top --limit 10
findata sectors --kind concept --direction bottom --limit 10
findata fund nav 000001 --start 2024-01-01 --limit 120
findata fund rank --period 1y --type all --limit 20
findata fund info 000001
findata batch quote 600519 000001 --market CN --workers 4
findata batch kline AAPL MSFT --market US --period daily --limit 100 --workers 2
```

Use explicit market/prefixed index symbols where stock/index numeric codes overlap. `--start`/`--end` are inclusive dates; `--limit` returns the latest matching observations, not calendar days. Weekly/monthly buckets may include a currently incomplete period or a period truncated by the requested end date; inspect dates and timestamps before interpreting a final candle. `--adjust none` requests unadjusted CN/HK bars; US unadjusted history is explicitly unsupported because Yahoo OHLC is already split-adjusted and Stooq is adjusted. US `qfq` uses Yahoo adjusted-close ratios (split/distribution adjusted) or Stooq's provider-adjusted series, not a promise that vendors use identical corporate-action methodology.

Index examples: `sh000001` (上证), `sz399001` (深证), `sz399006` (创业板), `sh000688` (科创50), `sh000300` (沪深300); `HSI` with `--market HK`; `^GSPC` with `--market US`. Bare CN `000001` is a stock, **not** the Shanghai index. Fund ranking periods: `1d,1w,1m,3m,6m,1y,2y,3y,ytd,all`; types: `all,stock,hybrid,bond,index,qdii` (`equity` aliases `stock`). Fund NAV/ranking limits are 1–10,000; mobile fund ranking pages are capped by the provider at 30 rows and are paginated internally.

## Output and chart interchange

A successful history result contains metadata plus `data`, a list of rows. The following is an illustrative schema, not a live result:

```json
{"symbol":"600519","market":"CN","source":"source name","last_trading_date":"YYYY-MM-DD","data_timestamp":"source/session timestamp","period":"daily","adjust":"qfq","data":[{"date":"YYYY-MM-DD","open":1.0,"high":1.2,"low":0.9,"close":1.1,"volume":100.0}]}
```

Rows additionally contain `amount` and `pct_chg` when supplied. Quotes expose source-provided prices/change/session fields; funds use NAV/return fields, not fictitious OHLCV. Overview/sector/fund outputs also include their source and date metadata where available. A date-only source cannot establish intraday freshness.

Stock history volumes are normalized to shares where the provider's units are known (Tencent CN quotes round to lots). Index volume remains provider-defined and must not be compared across sources as shares. CN/HK currency is CNY/HKD respectively; US symbols use their exchange quote currency (normally USD). Yahoo candle timestamps identify the candle's opening/session time, **not** its last price update; quote timestamps identify the source's market snapshot. Date-only CN/HK history uses its final bar date. Fund `retrieved_at` is separately recorded and must never be used as the NAV date; `data_date_range` discloses mixed observation dates. Fund-info `data` is a basic-information object, not a row list.

| Command | Payload schema (fields may be null/unavailable where source lacks them) |
| --- | --- |
| `resolve` | array of `{symbol,name,market,kind,exchange}` |
| `quote` | envelope plus `{symbol,name,market,price,previous_close,open,high,low,volume,change,change_percent}`; Stooq adds `quote_type: "daily_close"` |
| `overview` | `data[]`: `{symbol,name,label,price,change,change_pct,turnover,advances,declines,advance_decline,timestamp,unavailable[]}`; envelope describes `breadth_scope` |
| `sectors` | `data[]`: `{symbol,name,price,change,change_pct,turnover,advances,declines,advance_decline,timestamp,unavailable[]}` plus `kind,direction` |
| `fund nav` | `data[]`: `{date,nav,accumulated_nav,change_pct}` (source may add `nav_type`) |
| `fund rank` | `data[]`: `{symbol,name,date,nav,return_pct}` plus `period,fund_type,data_date_range` |
| `fund info` | `data`: source basic-information object (name/company/type/manager/assets fields where available); top-level `symbol,name` |
| `batch` | ordered array of `{symbol,result}` or `{symbol,error:{type,message}}` |
| error | `{error:{type,message,attempts?}}`, status1; no invented success payload |

`findata kline ... --out PATH` writes **only the JSON array** to PATH (UTF-8) and prints the metadata envelope with `file_path`. Give this path directly to rich-report:

```json
{"file_path":"/workspace/artifacts/600519.json","symbol":"600519","title":"贵州茅台 · 日线 · 前复权","colors":"intl","name":"600519-kline"}
```

Never pass the metadata envelope as `kline_chart.data`; use `result["data"]`. Chart tool accepts exactly one of `data` and `file_path`. Write deliverable artifacts under `/workspace`.

## Python API

```python
import findata

symbols = findata.resolve("贵州茅台", market="CN")
snapshot = findata.quote("00700", market="HK")
history = findata.kline("AAPL", market="US", period="daily", adjust="qfq", limit=120)
rows = history["data"]  # directly accepted by kline_chart
frame = findata.to_frame(history)  # optional pandas dependency
many = findata.fetch_many(["AAPL", "MSFT"], feature="kline", market="US", limit=120, workers=4)
market = findata.overview()
leaders = findata.sectors(kind="industry", direction="top", limit=10)
nav = findata.fund_nav("000001", start="2024-01-01", limit=250)
ranking = findata.fund_rank(period="1y", fund_type="all", limit=20)
basic = findata.fund_info("000001")
```

`findata.DataError` means invalid request or exhausted sources. `fetch_many` returns ordered `{symbol,result}` or `{symbol,error}` entries and limits workers to 1–16. No pandas dependency is required unless calling `to_frame`.

HTTP uses environment proxy discovery (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, including standard lowercase variants). `FINDATA_TIMEOUT` is per-request seconds (default 10); `FINDATA_RETRIES` is additional attempts (default 1; range 0–5). Fallback follows bounded retries. Do not disable TLS verification or bypass sandbox egress.

## Contacted hosts and sandbox egress

This inventory lists request destinations, not just corporate domains. Routing is based on `egress/whitelist.txt` and the external geosite-cn database. `.cn` domains go DIRECT; non-`.cn` Chinese providers may also match geosite-cn, but that database is deployment-specific. A local successful request is **not proof of sandbox reachability**.

| Host | Feature | Egress coverage |
| --- | --- | --- |
| `qt.gtimg.cn` | CN/HK quote, index overview | `.cn`: DIRECT |
| `web.ifzq.gtimg.cn` | CN/HK OHLCV | `.cn`: DIRECT |
| `smartbox.gtimg.cn` | symbol/name search | `.cn`: DIRECT |
| `hq.sinajs.cn` | quote and fund-info fallback | `.cn`: DIRECT |
| `quotes.sina.cn` | daily unadjusted history fallback | `.cn`: DIRECT |
| `vip.stock.finance.sina.com.cn` | industry/concept ranking fallback | `.cn`: DIRECT |
| `searchapi.eastmoney.com` | symbol-search fallback | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `push2.eastmoney.com` | quote, overview, industry/concept ranking | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `push2his.eastmoney.com` | OHLCV fallback | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `api.fund.eastmoney.com` | paginated NAV history | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `fund.eastmoney.com` | NAV JS fallback, fund-ranking fallback | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `fundmobapi.eastmoney.com` | fund ranking/basic info | **Not in explicit whitelist**; requires geosite-cn match or lead-added `eastmoney.com` |
| `query1.finance.yahoo.com` | US chart/quote primary | covered by `yahoo.com` |
| `query2.finance.yahoo.com` | alternate Yahoo chart host | covered by `yahoo.com` |
| `stooq.com` | US daily CSV fallback | covered by `stooq.com` |

The Referer values `finance.sina.com.cn` and `fund.eastmoney.com` are headers, not additional HTTP requests. No Nasdaq endpoint is contacted. Do not change the egress allowlist or proxy from an agent session to force access; report missing coverage.
