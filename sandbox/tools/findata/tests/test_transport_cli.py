import io
import json
from urllib.error import HTTPError
from email.message import Message
from unittest.mock import patch

import findata
from findata import cli, transport
from findata.errors import DataError


class Response(io.BytesIO):
    headers = Message()


def test_retry_and_gb18030(monkeypatch):
    monkeypatch.setenv("FINDATA_RETRIES", "1")
    with (
        patch.object(
            transport,
            "urlopen",
            side_effect=[
                HTTPError("url", 503, "busy", {}, None),
                Response("贵州茅台".encode("gb18030")),
            ],
        ) as request,
        patch.object(transport.time, "sleep"),
    ):
        assert transport.get_text("https://example.test", {"q": "600519"}) == "贵州茅台"
        assert request.call_count == 2
        assert request.call_args.args[0].full_url.endswith("q=600519")


def test_permanent_http_error_not_retried():
    with patch.object(
        transport, "urlopen", side_effect=HTTPError("url", 403, "denied", {}, None)
    ) as request:
        try:
            transport.get_text("https://example.test")
        except DataError as exc:
            assert exc.attempts == ["HTTP 403"]
        else:
            assert False, "expected DataError"
        assert request.call_count == 1


def test_batch_preserves_errors_and_order():
    def quote(symbol, **kwargs):
        if symbol == "bad":
            raise DataError("missing")
        return {"price": 1}

    with patch.object(findata, "quote", quote):
        result = findata.fetch_many(["good", "bad"], workers=2)
    assert result[0]["result"]["price"] == 1
    assert result[1]["error"]["message"] == "missing"


def test_cli_out_is_chart_array(tmp_path, capsys):
    rows = [{"date": "2026-10-26", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}]
    target = tmp_path / "kline.json"
    with patch.object(findata, "kline", return_value={"data": rows, "source": "fixture"}):
        assert cli.main(["kline", "AAPL", "--out", str(target)]) == 0
    assert json.loads(target.read_text()) == rows
    assert json.loads(capsys.readouterr().out)["file_path"] == str(target)


def test_cli_error_json(capsys):
    with patch.object(findata, "quote", side_effect=DataError("unavailable")):
        assert cli.main(["quote", "AAPL"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "unavailable"


def test_invalid_cli_argument_is_json(capsys):
    assert cli.main(["kline", "AAPL", "--period", "hourly"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "DataError"
