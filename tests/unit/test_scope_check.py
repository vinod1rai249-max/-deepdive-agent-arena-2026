"""The planted unsupported_extrapolation cases the Verifier let through as
`partial` (EVAL.md gallery #5–#7), plus guards against false rejections."""

from verify.scope_check import scope_consistent


def test_planted_overall_vs_regional_is_rejected():
    ok, missing = scope_consistent(
        "The company's overall sales grew 14% last quarter, driven by a new product launch.",
        "Sales in the Northeast region grew 14% last quarter, driven by a new product launch.",
    )
    assert not ok and missing == ["overall"]


def test_planted_entire_economy_is_rejected():
    ok, _ = scope_consistent(
        "Consumer spending across the entire economy slowed during the holiday season.",
        "One major retailer reported a slowdown in consumer spending during the holiday season.",
    )
    assert not ok


def test_planted_national_vs_single_facility_is_rejected():
    ok, missing = scope_consistent(
        "The program will reduce national emissions by 15% once rolled out.",
        "The pilot program reduced emissions by 15% at the single facility where it was tested.",
    )
    assert not ok and "national" in missing


def test_synonym_in_passage_is_accepted():
    ok, _ = scope_consistent("Global EV sales rose 20%.", "EV sales rose 20% worldwide in 2024.")
    assert ok


def test_claim_without_scope_words_always_passes():
    assert scope_consistent("Revenue grew 12% to $4.2 billion.", "Revenue was $4.2 billion, up 12%.")[0]


def test_scope_word_present_in_passage_passes():
    assert scope_consistent("Most analysts expect a slowdown.", "Most analysts surveyed expect a slowdown.")[0]
