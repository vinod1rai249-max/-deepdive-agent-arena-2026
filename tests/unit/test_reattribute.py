from verify.reattribute import candidate_passages


def test_ranks_siblings_by_relevance_and_excludes_original():
    passages = [
        {"id": "p1", "text": "battery supply chain risk is significant"},
        {"id": "p2", "text": "a recipe for sourdough bread"},
        {"id": "p3", "text": "battery supply chain risk and lithium sourcing"},
    ]
    result = candidate_passages("battery supply chain risk", passages, exclude_id="p1", k=5)
    assert all(p["id"] != "p1" for p in result)
    assert result[0]["id"] == "p3"  # more relevant sibling ranked first


def test_caps_at_k():
    passages = [{"id": f"p{i}", "text": f"battery risk item {i}"} for i in range(10)]
    result = candidate_passages("battery risk", passages, exclude_id="p0", k=3)
    assert len(result) == 3


def test_empty_when_no_siblings():
    passages = [{"id": "p1", "text": "only one passage"}]
    result = candidate_passages("query", passages, exclude_id="p1", k=5)
    assert result == []
