"""CLI 参数与错误 JSON 形状的离线测试(所有用例在发起网络请求之前失败)。"""

import json

import pytest

from cgdl.cli import main

URL = "https://musescore.com/user/39593079/scores/6969530"


def _stderr_error(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert set(payload) == {"error", "message"}
    return payload


def test_bare_id_is_bad_url(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["musescore-info", "6969530"]) == 1
    assert _stderr_error(capsys)["error"] == "bad_url"


def test_get_unknown_kind_is_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["musescore-get", URL, "--kind", "gif"]) == 1
    assert _stderr_error(capsys)["error"] == "unsupported_kind"


def test_get_relative_output_dir_is_bad_input(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["musescore-get", URL, "--kind", "mid", "-o", "relative/dir"]) == 1
    assert _stderr_error(capsys)["error"] == "bad_input"


@pytest.mark.parametrize("limit", ["0", "51"])
def test_search_limit_out_of_range(limit: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["musescore-search", "gymnopedie", "--limit", limit]) == 1
    assert _stderr_error(capsys)["error"] == "bad_input"


def test_missing_subcommand_exits_2_with_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2
    assert _stderr_error(capsys)["error"] == "bad_input"


def test_get_requires_kind(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["musescore-get", URL])
    assert info.value.code == 2
    assert _stderr_error(capsys)["error"] == "bad_input"
