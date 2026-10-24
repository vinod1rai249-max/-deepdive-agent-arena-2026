"""EVAL.md failure gallery #3/#4/#6: the Auditor's own reason described the
gap and the sentence passed anyway."""

import pytest

from verify.gap_reason import reason_admits_gap


@pytest.mark.parametrize(
    "reason",
    [
        "The evidence mentions the supply chain crisis but does not link it to the Federal Reserve's interest rate decisions.",
        "The evidence discusses neuronal mechanisms but does not address the hepatic molecular mechanisms.",
        "The evidence suggests a need for diversification but does not specifically state that factories are reliant on imports.",
        "There is no mention of high interest rates in the passage.",
    ],
)
def test_partial_with_admitted_gap_fails(reason):
    assert reason_admits_gap("partial", reason)


def test_supported_verdict_is_never_overridden():
    assert not reason_admits_gap("supported", "The passage states it directly and does not mention anything contradictory.")


def test_partial_without_a_stated_gap_still_passes():
    assert not reason_admits_gap("partial", "The span supports the figure but with a slightly different qualifier.")


def test_none_reason_is_safe():
    assert not reason_admits_gap("partial", None)
