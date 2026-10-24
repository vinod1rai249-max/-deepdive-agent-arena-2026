from verify.confidence import score_confidence


def test_supported_tier1_is_high_confidence():
    level, score = score_confidence(entailment="supported", tier=1, corroborating_sources=1)
    assert level == "high"
    assert score > 0.9


def test_partial_tier4_is_lower_confidence_than_supported_tier1():
    low_level, low_score = score_confidence(entailment="partial", tier=4, corroborating_sources=1)
    high_level, high_score = score_confidence(entailment="supported", tier=1, corroborating_sources=1)
    assert low_score < high_score


def test_corroboration_increases_score():
    _, one_source = score_confidence(entailment="supported", tier=3, corroborating_sources=1)
    _, three_sources = score_confidence(entailment="supported", tier=3, corroborating_sources=3)
    assert three_sources > one_source


def test_unknown_date_with_time_scope_lowers_score():
    _, with_date = score_confidence(entailment="supported", tier=2, published_at=__import__("datetime").datetime(2025, 1, 1), time_scope="2025")
    _, without_date = score_confidence(entailment="supported", tier=2, published_at=None, time_scope="2025")
    assert without_date < with_date


def test_stale_source_vs_time_scope_lowers_score():
    import datetime as dt

    _, fresh = score_confidence(entailment="supported", tier=2, published_at=dt.datetime(2026, 3, 1), time_scope="2026")
    _, stale = score_confidence(entailment="supported", tier=2, published_at=dt.datetime(2022, 3, 1), time_scope="2026")
    assert stale < fresh


def test_no_year_in_time_scope_does_not_penalize_old_source():
    import datetime as dt

    _, old = score_confidence(entailment="supported", tier=2, published_at=dt.datetime(2015, 1, 1), time_scope="historical")
    _, none = score_confidence(entailment="supported", tier=2, published_at=dt.datetime(2015, 1, 1), time_scope=None)
    assert old == none
