"""Minimal graph regression harness for the node-boundary handler (PRD v4 §8.2 / M3).

The loud-fail provider raises ``ProviderFetchError``; ``resilient_analyst_node``
ensures that a fetch failure in one analyst degrades *that analyst* for the run
rather than aborting the whole graph. These tests mirror the real
``start → analysts → END`` shape (src/observing_pools/scoring_graph.py and
src/main.py both build nodes via get_analyst_nodes, which now applies the wrapper).
"""

import logging

from langgraph.graph import END, StateGraph

from src.data.providers.exceptions import ProviderFetchError
from src.graph.state import AgentState
from src.utils.analysts import resilient_analyst_node


def _start(state: AgentState) -> AgentState:
    return state


def _build_graph(nodes):
    """nodes: list[(node_name, node_func)] wired start → node → END, each wrapped."""
    wf = StateGraph(AgentState)
    wf.add_node("start_node", _start)
    for name, func in nodes:
        wf.add_node(name, resilient_analyst_node(name, func))
        wf.add_edge("start_node", name)
        wf.add_edge(name, END)
    wf.set_entry_point("start_node")
    return wf.compile()


def _initial_state(tickers):
    return {
        "messages": [],
        "data": {"tickers": tickers, "analyst_signals": {}},
        "metadata": {},
    }


def _healthy_node(agent_id, signal):
    """A faithful stand-in for a real agent: fill analyst_signals, return data."""

    def node(state):
        for ticker in state["data"]["tickers"]:
            state["data"]["analyst_signals"].setdefault(agent_id, {})[ticker] = {
                "signal": signal,
                "confidence": 90,
            }
        return {"data": state["data"]}

    return node


def _raising_node(state):
    raise ProviderFetchError("simulated provider outage for one analyst")


# ── wrapper unit behavior ────────────────────────────────────────────────────

def test_wrapper_passes_through_on_success():
    node = resilient_analyst_node("x_agent", _healthy_node("x_agent", "bearish"))
    out = node(_initial_state(["MSFT"]))
    assert out["data"]["analyst_signals"]["x_agent"]["MSFT"]["signal"] == "bearish"


def test_wrapper_catches_fetch_error_and_returns_valid_state(caplog):
    node = resilient_analyst_node("y_agent", _raising_node)
    with caplog.at_level(logging.WARNING):
        out = node(_initial_state(["MSFT"]))
    assert "y_agent" not in out["data"]["analyst_signals"]  # no contribution
    assert "data" in out  # valid state for the reducer → run continues
    assert any("degraded" in r.message or "degraded" in r.getMessage() for r in caplog.records)


def test_wrapper_does_not_catch_unrelated_errors():
    def boom(state):
        raise ValueError("a real bug, not a fetch error")

    node = resilient_analyst_node("z_agent", boom)
    try:
        node(_initial_state(["MSFT"]))
    except ValueError:
        return
    raise AssertionError("unrelated errors must still surface loudly")


# ── compiled-graph behavior (the real seam) ──────────────────────────────────

def test_compiled_graph_with_failing_node_does_not_abort():
    graph = _build_graph([("burry_agent", _raising_node)])
    final = graph.invoke(_initial_state(["AAPL"]))  # must NOT raise
    assert "burry_agent" not in final["data"]["analyst_signals"]


def test_compiled_graph_healthy_node_still_scores():
    graph = _build_graph([("buffett_agent", _healthy_node("buffett_agent", "bullish"))])
    final = graph.invoke(_initial_state(["AAPL"]))
    assert final["data"]["analyst_signals"]["buffett_agent"]["AAPL"]["signal"] == "bullish"
