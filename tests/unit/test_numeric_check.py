from verify.numeric_check import numbers_consistent


def test_matching_number_passes():
    ok, missing = numbers_consistent("Revenue grew by 42%.", "the company's revenue grew by 42% last quarter")
    assert ok is True
    assert missing == []


def test_altered_number_fails():
    ok, missing = numbers_consistent("Revenue grew by 42%.", "the company's revenue grew by 12% last quarter")
    assert ok is False
    assert "42%" in missing or "42" in missing


def test_claim_with_no_numbers_trivially_passes():
    ok, missing = numbers_consistent("Revenue grew significantly.", "the company's revenue grew by 12%")
    assert ok is True
    assert missing == []


def test_currency_and_comma_formatting_normalizes():
    ok, _ = numbers_consistent("The deal was worth $1,200,000.", "the deal was reportedly worth $1200000 in cash")
    assert ok is True
