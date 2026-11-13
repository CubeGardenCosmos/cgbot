"""cgdl 命令行。stdout 输出一行 JSON;错误以 {"error", "message"} 写入 stderr 并非零退出。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn

from cgdl.client import KINDS, MuseScoreClient
from cgdl.errors import BAD_INPUT, UNSUPPORTED_KIND, CgdlError
from cgdl.files import DEFAULT_OUTPUT_DIR, ensure_output_dir
from cgdl.urls import parse_score_ref

MAX_LIMIT = 50


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _emit_error(BAD_INPUT, message)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="cgdl", description="MuseScore 搜索与乐谱下载")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    search = sub.add_parser("musescore-search", help="搜索乐谱")
    search.add_argument("query", help="搜索词")
    search.add_argument("--limit", type=int, default=10, help=f"结果数量 1-{MAX_LIMIT}")

    info = sub.add_parser("musescore-info", help="读取乐谱元数据")
    info.add_argument("url", help="https://musescore.com/user/<uid>/scores/<id>")

    get = sub.add_parser("musescore-get", help="下载乐谱文件;png 为指定页的 PNG 图片")
    get.add_argument("url", help="https://musescore.com/user/<uid>/scores/<id>")
    get.add_argument("--kind", required=True, help="mid | mxl | mp3 | pdf | png")
    get.add_argument("--page", type=int, default=None, help="png 的页码,从 1 开始(默认 1)")
    get.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR, help="绝对路径输出目录")
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "musescore-search":
        if not 1 <= args.limit <= MAX_LIMIT:
            raise CgdlError(BAD_INPUT, f"--limit must be 1-{MAX_LIMIT}")
        with MuseScoreClient() as client:
            hits = client.search(args.query, args.limit)
        return {"query": args.query, "results": [hit.to_dict() for hit in hits]}

    if args.command == "musescore-info":
        ref = parse_score_ref(args.url)
        with MuseScoreClient() as client:
            return client.info(ref).to_dict()

    if args.kind not in KINDS:
        raise CgdlError(
            UNSUPPORTED_KIND, f"unsupported kind {args.kind!r}; use mid, mxl, mp3, pdf or png"
        )
    page = _page_number(args)
    ref = parse_score_ref(args.url)
    out_dir = ensure_output_dir(args.output_dir)
    with MuseScoreClient() as client:
        return client.get(ref, args.kind, out_dir, page=page)


def _page_number(args: argparse.Namespace) -> int:
    if args.kind != "png":
        if args.page is not None:
            raise CgdlError(BAD_INPUT, "--page only applies to --kind png")
        return 1
    page = 1 if args.page is None else args.page
    if page < 1:
        raise CgdlError(BAD_INPUT, "--page must be >= 1")
    return page


def _emit_error(code: str, message: str) -> None:
    print(json.dumps({"error": code, "message": message}, ensure_ascii=False), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except CgdlError as exc:
        _emit_error(exc.code, exc.message)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
