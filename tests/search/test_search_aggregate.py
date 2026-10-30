"""Aggregation: tokenizing, URL de-duplication, scoring, and merge ordering (pure, offline)."""

from __future__ import annotations

from search_fakes import hit

from cgbot.search.aggregate import (
    BONUS_ALL_IN_TITLE,
    BONUS_EXACT_TITLE,
    W_SNIPPET,
    W_TITLE,
    merge_results,
    normalize_url,
    score_result,
    tokenize,
)


def test_tokenize_splits_words_and_drops_punctuation_only_tokens() -> None:
    assert tokenize("Python 教程 !!!") == ["python", "教程"]
    assert tokenize("   ") == []


def test_tokenize_adds_bigrams_for_one_long_unspaced_token() -> None:
    tokens = tokenize("量子计算")
    assert "量子计算" in tokens
    assert "量子" in tokens and "计算" in tokens
    assert tokenize("ab") == ["ab"]


def test_normalize_url_ignores_fragment_trailing_slash_and_case() -> None:
    assert normalize_url("HTTPS://Ex.com/A/#frag") == normalize_url("https://ex.com/a")
    assert normalize_url("") == ""


def test_title_match_outscores_snippet_only_match() -> None:
    query = "rust guide"
    in_title = hit("rust guide 入门", "https://a.example/1", "无关")
    in_snippet = hit("其他页面", "https://a.example/2", "rust guide 入门")
    assert score_result(query, in_title) == 2 * W_TITLE + BONUS_ALL_IN_TITLE
    assert score_result(query, in_snippet) == 2 * W_SNIPPET
    assert score_result(query, in_title) > score_result(query, in_snippet)


def test_exact_title_gets_bonus_on_top_of_coverage() -> None:
    query = "rust guide"
    exact = hit("rust guide", "https://a.example/exact")
    partial = hit("rust guide book", "https://a.example/partial")
    assert score_result(query, exact) == (2 * W_TITLE + BONUS_EXACT_TITLE + BONUS_ALL_IN_TITLE)
    assert score_result(query, exact) > score_result(query, partial)


def test_query_without_tokens_scores_zero_and_keeps_engine_order() -> None:
    first = hit("一", "https://a.example/1")
    second = hit("二", "https://a.example/2")
    assert score_result("   ", first) == 0.0
    merged = merge_results("   ", [[first], [second]], 10)
    assert merged == [first, second]


def test_merge_dedupes_by_url_first_engine_wins() -> None:
    bing = hit("Alpha", "https://example.com/a/", "from bing", source="bing")
    baidu = hit("Alpha copy", "https://EXAMPLE.com/a#x", "from baidu", source="baidu")
    merged = merge_results("alpha", [[bing], [baidu]], 10)
    assert len(merged) == 1
    assert merged[0].source == "bing"


def test_merge_sorts_by_score_then_engine_rank_and_caps_count() -> None:
    weak = hit("nothing", "https://example.com/weak", "", source="bing")
    strong = hit("rust guide", "https://example.com/strong", "", source="baidu")
    tie_a = hit("zzz", "https://example.com/tie-a", "", source="bing")
    tie_b = hit("yyy", "https://example.com/tie-b", "", source="baidu")
    merged = merge_results("rust guide", [[weak, tie_a], [strong, tie_b]], 3)
    assert [r.url for r in merged] == [
        "https://example.com/strong",
        "https://example.com/weak",
        "https://example.com/tie-a",
    ]


def test_merge_with_no_results_is_empty() -> None:
    assert merge_results("rust", [[], []], 5) == []
