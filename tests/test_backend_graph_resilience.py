"""The backend React-Flow graph (app/backend/services/graph.py) must also apply
the node-boundary handler (PRD v4 §8.2/M3): a provider fetch error in one analyst
degrades that analyst rather than aborting the whole backend run.

Builds a minimal valid graph (start → analyst → risk → portfolio → END) with the
analyst's agent function monkeypatched to raise ``ProviderFetchError``, and asserts
the compiled graph still completes.
"""

from types import SimpleNamespace

from app.backend.services import graph as backend_graph
from src.data.providers.exceptions import ProviderFetchError


def test_backend_graph_does_not_abort_on_analyst_fetch_error(monkeypatch):
    # Replace create_agent_function: the analyst raises; risk/portfolio pass through.
    def fake_create_agent_function(node_func, unique_agent_id):
        if unique_agent_id.startswith("warren_buffett"):
            def raising(state):
                raise ProviderFetchError("simulated outage in backend analyst")

            return raising

        def passthrough(state):
            return {"data": state["data"]}

        return passthrough

    monkeypatch.setattr(backend_graph, "create_agent_function", fake_create_agent_function)

    nodes = [
        SimpleNamespace(id="warren_buffett_aaaaaa"),
        SimpleNamespace(id="portfolio_manager_aaaaaa"),
    ]
    edges = [SimpleNamespace(source="warren_buffett_aaaaaa", target="portfolio_manager_aaaaaa")]

    compiled = backend_graph.create_graph(nodes, edges).compile()
    final = compiled.invoke(
        {
            "messages": [],
            "data": {"tickers": ["AAPL"], "analyst_signals": {}, "portfolio": {"cash": 0.0, "positions": {}}},
            "metadata": {},
        }
    )
    # Reaching here means the fetch error did not abort the run.
    assert "warren_buffett_aaaaaa" not in final["data"]["analyst_signals"]
