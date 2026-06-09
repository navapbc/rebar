"""Sanity test for the paginating_acli_stub fixture."""


def test_paginating_stub_basic(paginating_acli_stub):
    stub = paginating_acli_stub([{"id": i} for i in range(250)], max_results_cap=100)
    page1 = stub("project = DIG", start_at=0, max_results=100)
    page2 = stub("project = DIG", start_at=100, max_results=100)
    page3 = stub("project = DIG", start_at=200, max_results=100)
    page4 = stub("project = DIG", start_at=300, max_results=100)
    assert page1["total"] == 250
    assert len(page1["issues"]) == 100
    assert len(page2["issues"]) == 100
    assert len(page3["issues"]) == 50
    assert len(page4["issues"]) == 0
    assert page1["startAt"] == 0
    assert page2["startAt"] == 100


def test_paginating_stub_respects_cap(paginating_acli_stub):
    stub = paginating_acli_stub([{"id": i} for i in range(10)], max_results_cap=100)
    page = stub("jql", start_at=0, max_results=1000)
    # cap clamps to 100; only 10 items in pages so 10 returned
    assert page["maxResults"] == 100
    assert len(page["issues"]) == 10
