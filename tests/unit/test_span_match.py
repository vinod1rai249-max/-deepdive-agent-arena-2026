from verify.span_match import span_exists


def test_exact_span_found():
    passage = "The company reported revenue of $50 million in fiscal year 2025."
    assert span_exists("The company reported revenue of $50 million in fiscal year 2025.", passage) is True


def test_span_with_minor_punctuation_differences_still_found():
    passage = "The company reported revenue of $50 million in fiscal year 2025."
    assert span_exists("the company reported revenue of $50 million in fiscal year 2025", passage) is True


def test_fabricated_span_not_found():
    passage = "The company reported revenue of $50 million in fiscal year 2025."
    assert span_exists("The company reported a net loss of $10 million.", passage) is False


def test_empty_span_or_passage_is_false():
    assert span_exists("", "some passage") is False
    assert span_exists("some span", "") is False
    assert span_exists(None, "some passage") is False
