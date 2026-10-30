"""Public API: resolve, quote, kline, overview, sectors, fund_nav/rank/info, fetch_many.

Results are JSON-compatible dictionaries; kline()['data'] is directly accepted by
rich-report kline_chart. to_frame(result) optionally creates a pandas DataFrame.
"""

from concurrent.futures import ThreadPoolExecutor

from .errors import DataError


def resolve(query, market=None):
    from .symbols import resolve as impl

    return impl(query, market=market)


def quote(symbol, market=None):
    from .markets import quote as impl

    return impl(symbol, market=market)


def kline(symbol, market=None, period="daily", start=None, end=None, adjust="qfq", limit=250):
    from .markets import kline as impl

    return impl(
        symbol, market=market, period=period, start=start, end=end, adjust=adjust, limit=limit
    )


def overview():
    from .overview import overview as impl

    return impl()


def sectors(kind="industry", direction="top", limit=10):
    from .overview import sectors as impl

    return impl(kind=kind, direction=direction, limit=limit)


def fund_nav(symbol, start=None, end=None, limit=250):
    from .funds import nav

    return nav(symbol, start=start, end=end, limit=limit)


def fund_rank(period="1y", fund_type="all", limit=20):
    from .funds import rank

    return rank(period=period, fund_type=fund_type, limit=limit)


def fund_info(symbol):
    from .funds import info

    return info(symbol)


def fetch_many(symbols, feature="quote", workers=6, **kwargs):
    """Ordered, bounded concurrent requests; one failure never discards siblings.

    Returns [{symbol, result}, ...], or {symbol, error} for each failed request.
    feature is quote/kline; kwargs pass through to the corresponding API.
    """
    if feature not in ("quote", "kline"):
        raise DataError("feature must be quote or kline")
    if not 1 <= workers <= 16:
        raise DataError("workers must be between 1 and 16")
    operation = quote if feature == "quote" else kline

    def fetch(symbol):
        try:
            return {"symbol": symbol, "result": operation(symbol, **kwargs)}
        except (DataError, ValueError) as exc:
            error = (
                exc.as_dict()["error"]
                if isinstance(exc, DataError)
                else {"type": "ValueError", "message": str(exc)}
            )
            return {"symbol": symbol, "error": error}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch, symbols))


def to_frame(result):
    """Convert normalized result['data'] to a DataFrame (requires pandas)."""
    import pandas as pd

    return pd.DataFrame(result["data"])


__all__ = [
    "DataError",
    "resolve",
    "quote",
    "kline",
    "overview",
    "sectors",
    "fund_nav",
    "fund_rank",
    "fund_info",
    "fetch_many",
    "to_frame",
]
