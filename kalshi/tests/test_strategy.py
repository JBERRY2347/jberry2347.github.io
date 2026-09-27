import json

import pytest

from kalshi_trader.strategy import MarketPlan, best_ask, decide, load_plan

BOOK = {"yes": [[40, 100], [38, 50]], "no": [[55, 20], [50, 10]]}  # yes ask = 45, no ask = 60


def test_best_ask_derived_from_opposite_bids():
    assert best_ask(BOOK, "yes") == 45
    assert best_ask(BOOK, "no") == 60
    assert best_ask({"yes": [], "no": []}, "yes") is None


def test_decide_buys_yes_when_cheap():
    plan = MarketPlan("T", yes_prob=0.55, edge_cents=5, max_contracts=10)
    order = decide(plan, BOOK, position=0)
    assert order.side == "yes" and order.price_cents == 45 and order.count == 10 and order.action == "buy"


def test_decide_buys_no_when_cheap():
    plan = MarketPlan("T", yes_prob=0.30, edge_cents=5, max_contracts=4)  # fair no = 70, ask 60
    order = decide(plan, BOOK, position=0)
    assert order.side == "no" and order.price_cents == 60 and order.count == 4


def test_decide_respects_edge_and_position():
    plan = MarketPlan("T", yes_prob=0.48, edge_cents=5, max_contracts=10)  # edge only 3c
    assert decide(plan, BOOK, position=0) is None
    plan = MarketPlan("T", yes_prob=0.55, edge_cents=5, max_contracts=10)
    assert decide(plan, BOOK, position=10) is None
    assert decide(plan, BOOK, position=7).count == 3


def test_load_plan(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"edge_cents": 4, "markets": {"A": {"yes_prob": 0.6}, "B": {"yes_prob": 0.2, "max_contracts": 3}}}))
    plans = load_plan(p)
    assert [(x.ticker, x.edge_cents, x.max_contracts) for x in plans] == [("A", 4, 10), ("B", 4, 3)]
    p.write_text(json.dumps({"markets": {"A": {"yes_prob": 1.5}}}))
    with pytest.raises(ValueError):
        load_plan(p)
