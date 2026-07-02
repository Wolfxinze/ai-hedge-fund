"""Doc-guard: PRD §22/§11.5/§11.2/§8.2 reconcile (issue-ledger + risk-haircut + DRY-seam accuracy).

These pin the corrected ledger against regression to the pre-reconcile (inverted) text:
- §22 lists #66 as the ONLY open issue; #23 CLOSED (Phase-11 deferral, NOT counsel sign-off),
  #25 shipped→#66, #43 wontfix-by-design/CLOSED, #51 shipped, naming-drift RESOLVED.
- §11.5 drops "Deliberately deferred (issue #43)" for a wontfix-by-design/CLOSED framing.
- §11.2 marks the risk_adjusted_momentum "− risk haircut" as DEFERRED (momentum-only today); the
  scoring.py comment is trued-up to match.
- §8.2 corrects the DRY-seam clause: app/backend/services/graph.py wraps resilient_analyst_node
  INLINE (it does not use the get_analyst_nodes() seam).

Pure text assertions — verified against the actual code (graph.py wraps inline; the #66 ledger via
`gh issue view`). No import, no boot.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PRD = _REPO / "docs" / "prd-observing-pools.md"
_SCORING = _REPO / "src" / "observing_pools" / "scoring.py"


def _section(title_marker: str, text: str) -> str:
    """Slice a PRD section from its `### §X` header to the next header of the same-or-higher level."""
    start = text.index(title_marker)
    rest = text[start + len(title_marker):]
    # next section header (## or ###) ends this slice
    ends = [rest.index(m) for m in ("\n### ", "\n## ", "\n---") if m in rest]
    return rest[: min(ends)] if ends else rest


# ── §22 issue-ledger ────────────────────────────────────────────────────────
def test_s22_lists_66_as_the_only_open_issue():
    s22 = _section("## §22", _PRD.read_text())
    assert "#66" in s22, "§22 must list #66 (the only open issue)"
    assert "only open" in s22.lower(), "§22 must state #66 is the only open issue"


def test_s22_marks_23_closed_not_counsel_signoff():
    s22 = _section("## §22", _PRD.read_text())
    # the inverted line ("#23 — counsel sign-off ... Open by design") must be gone
    assert "#23 — counsel sign-off" not in s22, "§22 must not mislabel #23 as counsel sign-off"
    assert "#23" in s22 and "CLOSED" in s22, "§22 must mark #23 CLOSED (Phase-11 deferral)"


def test_s22_marks_43_wontfix_closed():
    s22 = _section("## §22", _PRD.read_text())
    assert "#43" in s22
    assert "wontfix" in s22.lower() and "CLOSED" in s22, "§22 must mark #43 wontfix-by-design/CLOSED"


def test_s22_marks_51_shipped_and_naming_resolved():
    s22 = _section("## §22", _PRD.read_text())
    assert "#51" in s22 and "shipped" in s22.lower()
    assert "RESOLVED" in s22, "§22 must mark the v3-*/v4-* naming drift RESOLVED"


def test_s22_adds_risk_haircut_deferred_bullet_with_i1():
    s22 = _section("## §22", _PRD.read_text())
    low = s22.lower()
    assert "risk" in low and "haircut" in low, "§22 must carry a Risk-Manager haircut deferred bullet"
    assert "i1" in low and "no risk_manager import" in low, "the haircut bullet must note I1 (signal-only)"


# ── §11.5 wontfix reframe ───────────────────────────────────────────────────
def test_s115_drops_deliberately_deferred_for_wontfix():
    prd = _PRD.read_text()
    assert "Deliberately deferred (issue #43)" not in prd, "§11.5 must drop the 'deferred' framing"
    s115 = _section("### §11.5", prd)
    assert "#43" in s115 and "wontfix" in s115.lower(), "§11.5 must frame #43 as wontfix-by-design/CLOSED"


# ── §11.2 risk-haircut truth-up ─────────────────────────────────────────────
def test_s112_marks_risk_haircut_deferred():
    s112 = _section("### §11.2", _PRD.read_text())
    low = s112.lower()
    assert "momentum-only" in low, "§11.2 must state the component is momentum-only today"
    assert "deferred" in low, "§11.2 must mark the risk haircut DEFERRED"


def test_scoring_comment_trued_up_to_match():
    src = _SCORING.read_text()
    # the risk_adjusted_momentum weight line must no longer imply an applied haircut
    line = next(l for l in src.splitlines() if '"risk_adjusted_momentum"' in l)
    assert "deferred" in line.lower(), "scoring.py risk_adjusted_momentum comment must mark the haircut DEFERRED"


# ── §8.2 DRY-seam accuracy ──────────────────────────────────────────────────
def test_s82_corrects_dry_seam_backend_wraps_inline():
    s82_line = next(l for l in _PRD.read_text().splitlines() if "§8.2" in l)
    low = s82_line.lower()
    assert "inline" in low, "§8.2 must state app/backend/services/graph.py wraps resilient_analyst_node inline"
    # the incorrect 'single DRY seam ... incl. graph.py' claim must be gone
    assert "incl. `app/backend/services/graph.py`" not in s82_line, "§8.2 must not claim graph.py uses the single seam"
