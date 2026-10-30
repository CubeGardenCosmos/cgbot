"""Merge per-engine results into one ranked list. Pure local computation, no requests.

Order of input matters: engines are passed in priority order, and on a duplicate URL the first
occurrence wins. Scoring ranks by keyword coverage (title weighted above snippet); ties keep the
engine priority and then the engine's own order.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cgbot.search.models import SearchResult

W_TITLE: Final = 3.0  # token found in title
W_SNIPPET: Final = 1.0  # token found in snippet only
BONUS_EXACT_TITLE: Final = 5.0  # title equals the whole query
BONUS_ALL_IN_TITLE: Final = 2.0  # every token found in title

_WS_RE = re.compile(r"\s+")
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")


def normalize_text(text: str) -> str:
    """NFKC then lower-case: unifies full/half-width forms and letter case."""
    return unicodedata.normalize("NFKC", text or "").lower()


def normalize_url(url: str) -> str:
    """Key used only for de-duplication: fragment, trailing slash and case ignored."""
    if not url:
        return ""
    return url.strip().split("#", 1)[0].rstrip("/").lower()


def tokenize(query: str) -> list[str]:
    """Split a query into match tokens.

    Whitespace-separated tokens; punctuation-only tokens dropped. A single long token (typically
    unspaced Chinese) also contributes its adjacent character bigrams, so a long query is not
    matched only when the entire string appears verbatim.
    """
    norm = normalize_text(query).strip()
    if not norm:
        return []
    tokens = [t for t in _WS_RE.split(norm) if t and not _PUNCT_ONLY_RE.match(t)]
    if len(tokens) == 1 and len(tokens[0]) > 2:
        word = tokens[0]
        tokens += [word[i : i + 2] for i in range(len(word) - 1)]
    return tokens


def score_result(query: str, result: SearchResult) -> float:
    """Keyword-coverage score. Each token counts at most once per field.

    A query without matchable tokens scores 0.0 for every result, which leaves engine order intact.
    """
    tokens = tokenize(query)
    if not tokens:
        return 0.0
    norm_query = normalize_text(query).strip()
    title = normalize_text(result.title)
    snippet = normalize_text(result.snippet)
    score = 0.0
    title_hits = 0
    for token in tokens:
        if token in title:
            score += W_TITLE
            title_hits += 1
        elif token in snippet:
            score += W_SNIPPET
    if title and title == norm_query:
        score += BONUS_EXACT_TITLE
    if title_hits == len(tokens):
        score += BONUS_ALL_IN_TITLE
    return score


@dataclass(frozen=True, slots=True)
class _Ranked:
    score: float
    engine_rank: int
    seq: int
    result: SearchResult


def merge_results(
    query: str,
    per_engine: Sequence[Sequence[SearchResult]],
    count: int,
) -> list[SearchResult]:
    """Dedupe by URL (priority order decides the survivor), score, sort, and cap at ``count``."""
    seen: set[str] = set()
    ranked: list[_Ranked] = []
    seq = 0
    for engine_rank, results in enumerate(per_engine):
        for result in results:
            key = normalize_url(result.url)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            ranked.append(
                _Ranked(
                    score=score_result(query, result),
                    engine_rank=engine_rank,
                    seq=seq,
                    result=result,
                )
            )
            seq += 1
    ranked.sort(key=lambda r: (-r.score, r.engine_rank, r.seq))
    return [r.result for r in ranked[:count]]
