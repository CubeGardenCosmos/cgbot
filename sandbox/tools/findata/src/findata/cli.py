"""The JSON-only command line interface."""

import argparse
import json
from pathlib import Path
import sys

import findata


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise findata.DataError(message)


def parser():
    p = JsonArgumentParser(
        description="Public market data, normalized for rich-report K-lines.",
        epilog="Examples: findata quote 600519 --market CN; findata kline 00700 --out /workspace/hk.json; findata fund rank --period 1y --type equity; findata batch quote AAPL MSFT --market US",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--pretty", action="store_true", help="indent JSON (also accepted after a subcommand)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, help):
        c = sub.add_parser(name, help=help)
        c.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
        return c

    def market(c):
        c.add_argument("--market", choices=["CN", "HK", "US", "cn", "hk", "us"])

    def history(c):
        c.add_argument("--period", choices=["daily", "weekly", "monthly"], default="daily")
        c.add_argument("--start", help="inclusive YYYY-MM-DD")
        c.add_argument("--end", help="inclusive YYYY-MM-DD")
        c.add_argument("--adjust", choices=["qfq", "none"], default="qfq")
        c.add_argument("--limit", type=int, default=250)

    c = cmd("resolve", "resolve code/name to market symbols")
    c.add_argument("query")
    market(c)
    c = cmd("quote", "last source quote with trading date")
    c.add_argument("symbol")
    market(c)
    c = cmd("kline", "normalized OHLCV history")
    c.add_argument("symbol")
    market(c)
    history(c)
    c.add_argument("--out", help="write plain OHLCV array JSON for kline_chart.file_path")
    cmd("overview", "A-share benchmark and breadth snapshot")
    c = cmd("sectors", "industry/sector performance ranking")
    c.add_argument("--kind", choices=["industry", "concept"], default="industry")
    c.add_argument("--direction", choices=["top", "bottom"], default="top")
    c.add_argument("--limit", type=int, default=10)
    c = cmd("fund", "fund NAV, ranking and basic information")
    fs = c.add_subparsers(dest="fund_command", required=True)
    for name in ("nav", "rank", "info"):
        f = fs.add_parser(name)
        f.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
        if name != "rank":
            f.add_argument("symbol")
        if name == "nav":
            f.add_argument("--start")
            f.add_argument("--end")
            f.add_argument("--limit", type=int, default=250)
        if name == "rank":
            f.add_argument("--period", default="1y")
            f.add_argument("--type", dest="fund_type", default="all")
            f.add_argument("--limit", type=int, default=20)
    c = cmd("batch", "bounded concurrent quotes or OHLCV; preserves per-symbol errors")
    c.add_argument("feature", choices=["quote", "kline"])
    c.add_argument("symbols", nargs="+")
    market(c)
    history(c)
    c.add_argument("--workers", type=int, default=6)
    return p


def main(argv=None):
    p = parser()
    try:
        args = p.parse_args(argv)
        values = vars(args).copy()
        pretty = values.pop("pretty", False)
        command = values.pop("command")
        if values.get("market"):
            values["market"] = values["market"].upper()
        out = values.pop("out", None)
        if command == "fund":
            operation = getattr(findata, "fund_" + values.pop("fund_command"))
        elif command == "batch":
            operation = findata.fetch_many
            if values["feature"] == "quote":
                for key in ("period", "start", "end", "adjust", "limit"):
                    values.pop(key)
        else:
            operation = getattr(findata, command)
        result = operation(**values)
        if out:
            target = Path(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    result["data"], ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ),
                encoding="utf-8",
            )
            result = dict(result, file_path=str(target.resolve()))
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
        )
        return 0
    except (findata.DataError, ValueError, OSError, KeyError, TypeError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, findata.DataError)
            else {"error": {"type": type(exc).__name__, "message": str(exc)}}
        )
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
