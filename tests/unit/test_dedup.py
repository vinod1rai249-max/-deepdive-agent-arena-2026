from retrieval.dedup import is_near_duplicate, shingle_jaccard


def test_identical_text_is_near_duplicate():
    text = "The quick brown fox jumps over the lazy dog in the forest today."
    assert is_near_duplicate(text, text) is True


def test_unrelated_text_is_not_near_duplicate():
    a = "The quick brown fox jumps over the lazy dog in the forest today."
    b = "Quarterly revenue grew by twelve percent amid strong demand overseas."
    assert is_near_duplicate(a, b) is False


def test_syndicated_copy_with_minor_edits_is_near_duplicate():
    a = "The company reported record revenue this quarter driven by strong overseas demand and new product lines"
    b = "The company reported record revenue this quarter driven by strong overseas demand and new product lines!"
    assert shingle_jaccard(a, b) > 0.8
