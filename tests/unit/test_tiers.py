from retrieval.tiers import classify_tier


def test_gov_domain_is_tier_1():
    assert classify_tier("sec.gov") == 1
    assert classify_tier("www.sec.gov") == 1


def test_known_news_domain_is_tier_2():
    assert classify_tier("reuters.com") == 2


def test_forum_hint_is_tier_4():
    assert classify_tier("old.reddit.com") == 4


def test_unknown_domain_is_tier_3():
    assert classify_tier("somecompany.example") == 3


def test_missing_domain_is_tier_4():
    assert classify_tier(None) == 4
