"""No-trade / research-only suite (architecture invariant). The scoring graph wires
``start -> analysts -> END`` and NEVER a risk/portfolio (trade) node; and the
observing-pools scoring/classification modules do not directly import any trade
path. Two offline graders: a structural node-set check on the built graph (built,
never invoked -> no LLM) and an AST import scan (reads source, imports nothing
heavy). The agent stack is lazy-imported inside the grader, mirroring the CLI's
lazy-import discipline, so the suite stays light at import time.
"""

from __future__ import annotations

import ast
import pathlib

from src.evals.core import CodeGrader, EvalCase, Recorder
from src.evals.registry import suite

_SUITE = "no_trade"
_SCANNED_MODULES = ("pipeline", "agents_bridge", "scoring", "classify", "scoring_graph")
# Direct imports of any of these in the scoring/classification modules would reopen
# a path to the trade graph. The shared analyst registry (src/utils/analysts.py)
# imports portfolio_manager, but these modules must not import it DIRECTLY.
_FORBIDDEN_IMPORT_SUBSTRINGS = ("run_hedge_fund", "portfolio_manager", "risk_manager", "risk_management", "src.main")


def _scoring_graph_has_no_trade_nodes(rec: Recorder) -> bool:
    from src.observing_pools.scoring_graph import (
        _build_scoring_workflow,  # heavy: lazy-imported
    )

    selected = ["warren_buffett", "cathie_wood"]
    workflow = _build_scoring_workflow(selected)
    nodes = set(workflow.nodes)
    forbidden = sorted(n for n in nodes if "risk" in n.lower() or "portfolio" in n.lower())
    rec.record("scoring_graph", nodes=sorted(nodes), forbidden=forbidden)
    if forbidden:
        return False
    if "start_node" not in nodes:
        return False
    return len(nodes) == len(selected) + 1  # start + one node per selected analyst, nothing else


def _modules_have_no_direct_trade_imports(rec: Recorder) -> bool:
    import src.observing_pools as pkg

    base = pathlib.Path(pkg.__file__).parent
    for mod in _SCANNED_MODULES:
        tree = ast.parse((base / f"{mod}.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        bad = [name for name in imported for sub in _FORBIDDEN_IMPORT_SUBSTRINGS if sub in name]
        if bad:
            rec.record("ast_imports", module=mod, forbidden=bad)
            return False
    rec.record("ast_imports", scanned=len(_SCANNED_MODULES))
    return True


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("scoring_graph_has_no_trade_nodes", _SUITE, CodeGrader("no_trade.scoring_graph_has_no_trade_nodes", _scoring_graph_has_no_trade_nodes)),
        EvalCase("modules_have_no_direct_trade_imports", _SUITE, CodeGrader("no_trade.modules_have_no_direct_trade_imports", _modules_have_no_direct_trade_imports)),
    ]
