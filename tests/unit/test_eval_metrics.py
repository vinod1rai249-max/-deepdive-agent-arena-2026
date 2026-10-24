"""Unit tests for the pure metric-calculation functions in
eval/run_eval.py and eval/report.py — independent of any live pipeline
execution, so these need no Mesh key or credits."""

from __future__ import annotations

from eval.report import render
from eval.run_eval import (
    bootstrap_ci,
    cohen_kappa,
    compute_citation_precision,
    compute_planted_claim_recall,
    compute_rubric_coverage,
    compute_sentence_support_rate,
)


def test_planted_claim_recall_overall_and_by_type():
    results = [
        {"type": "wrong_number", "rejected": True},
        {"type": "wrong_number", "rejected": False},
        {"type": "contradiction", "rejected": True},
        {"type": "contradiction", "rejected": True},
    ]
    metrics = compute_planted_claim_recall(results)
    assert metrics["overall"] == 0.75
    assert metrics["by_type"]["wrong_number"] == 0.5
    assert metrics["by_type"]["contradiction"] == 1.0


def test_planted_claim_recall_empty_is_none():
    assert compute_planted_claim_recall([]) == {"overall": None, "by_type": {}}


def test_sentence_support_rate():
    sentences = [{"audit_passed": True}, {"audit_passed": True}, {"audit_passed": False}]
    assert compute_sentence_support_rate(sentences) == round(2 / 3, 4)


def test_citation_precision_all_verified():
    sentences = [{"claim_ids": ["c1", "c2"]}, {"claim_ids": ["c3"]}]
    status = {"c1": "verified", "c2": "partial", "c3": "re_cited"}
    assert compute_citation_precision(sentences, status) == 1.0


def test_citation_precision_catches_a_regression():
    sentences = [{"claim_ids": ["c1", "c2"]}]
    status = {"c1": "verified", "c2": "rejected"}  # shouldn't happen by construction, but the metric must catch it
    assert compute_citation_precision(sentences, status) == 0.5


def test_citation_precision_no_citations_is_none():
    assert compute_citation_precision([], {}) is None


def test_rubric_coverage_partial_and_padded():
    assert compute_rubric_coverage(["a", "b", "c", "d"], [True, False]) == 0.25  # short list padded with False


def test_bootstrap_ci_is_centered_near_the_mean_for_identical_values():
    lo, hi = bootstrap_ci([0.8, 0.8, 0.8, 0.8], n_resamples=200)
    assert lo == hi == 0.8


def test_cohen_kappa_perfect_agreement_is_one():
    labels = ["supported", "unsupported", "supported", "partial"]
    assert cohen_kappa(labels, labels) == 1.0


def test_cohen_kappa_mismatched_length_is_zero():
    assert cohen_kappa(["supported"], ["supported", "unsupported"]) == 0.0


def test_report_render_includes_planted_recall_and_question_stats():
    results = {
        "timestamp": "20260101T000000Z",
        "planted_claim_recall": {"overall": 0.9, "by_type": {"wrong_number": 1.0}},
        "questions": [
            {"cost_usd": 0.01, "citation_precision": 1.0, "stop_reason": "coverage_met"},
            {"cost_usd": 0.02, "citation_precision": 1.0, "stop_reason": "max_iter"},
        ],
    }
    text = render(results)
    assert "0.9" in text
    assert "wrong_number" in text
    assert "Questions evaluated (research / false-premise) | 2 / 0" in text
