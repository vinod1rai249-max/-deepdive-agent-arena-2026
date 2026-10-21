"""Unit tests for the shared lexical relevance scorer (BLUEPRINT.md §12).
This module backs the Verifier's pre-filter, re-attribution ranking,
Drafter passage selection, Curator trimming, and Follow-up retrieval — it
has no LLM dependency, so these tests need no Mesh key and no mocking."""

from __future__ import annotations

from core.relevance import passes_prefilter, rank, score, top_k


def test_score_high_for_near_identical_text():
    a = "The EV battery market faces supply chain risks in 2026."
    b = "The EV battery market faces supply chain risks in 2026."
    assert score(a, b) > 0.9


def test_score_low_for_unrelated_text():
    a = "The EV battery market faces supply chain risks in 2026."
    b = "A recipe for sourdough bread requires a long fermentation."
    assert score(a, b) < 0.1


def test_score_rewards_shared_numbers():
    base = "revenue grew significantly last year"
    with_number = "revenue grew by 42 percent last year"
    query = "the company reported 42 percent growth"
    assert score(query, with_number) > score(query, base)


def test_score_handles_empty_text():
    assert score("", "something") == 0.0
    assert score("something", "") == 0.0
    assert score("", "") == 0.0


def test_rank_orders_most_relevant_first():
    query = "battery supply chain risk"
    candidates = [
        "a recipe for sourdough bread",
        "battery supply chain risks are significant",
        "the weather today is sunny",
    ]
    ranked = rank(query, candidates)
    assert ranked[0].text == "battery supply chain risks are significant"
    assert ranked[0].score >= ranked[1].score >= ranked[2].score


def test_rank_with_key_extractor_on_objects():
    class Passage:
        def __init__(self, text: str) -> None:
            self.text = text

    query = "battery supply chain risk"
    candidates = [Passage("sourdough bread recipe"), Passage("battery supply chain risk analysis")]
    ranked = rank(query, candidates, key=lambda p: p.text)
    assert ranked[0].item.text == "battery supply chain risk analysis"


def test_top_k_caps_result_count():
    query = "battery risk"
    candidates = [f"battery risk item {i}" for i in range(20)]
    result = top_k(query, candidates, k=5)
    assert len(result) == 5


def test_passes_prefilter_true_for_overlapping_text():
    assert passes_prefilter("battery supply chain risk", "the battery supply chain faces risk") is True


def test_passes_prefilter_false_for_unrelated_text():
    assert passes_prefilter("battery supply chain risk", "sourdough bread fermentation time") is False
