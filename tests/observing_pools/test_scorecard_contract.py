"""Pin the Serenity scorecard-dimension contract across the backend/frontend seam.

The backend defines the canonical bottleneck dimensions in
``src.serenity.grading.SCORECARD_DIMENSIONS``; the frontend hand-copies the same
list into ``serenity-panel.tsx`` (order + keys drive the form + i18n lookup).
Nothing failed on drift. Two guards:

(a) Pin ``SCORECARD_DIMENSIONS`` verbatim — the canonical order and keys.
(b) House-style doc-guard (sibling of ``tests/docs/test_readme_bind_host.py``):
    read the frontend component from the repo root and assert every backend
    dimension string appears, so renaming/adding a backend dimension fails until
    the frontend copy is updated.
"""

from __future__ import annotations

from pathlib import Path

from src.serenity.grading import SCORECARD_DIMENSIONS

_REPO = Path(__file__).resolve().parents[2]
_SERENITY_PANEL = _REPO / "app" / "frontend" / "src" / "components" / "observing-pools" / "serenity-panel.tsx"

# The canonical order + keys. Pinning these verbatim makes an unreviewed reorder
# or rename of the backend contract fail loudly.
_CANONICAL_DIMENSIONS = (
    "supplier_concentration",
    "validation_cycle",
    "capacity_expansion",
    "certification_strictness",
    "purity_precision",
)


def test_scorecard_dimensions_pinned_verbatim():
    assert SCORECARD_DIMENSIONS == _CANONICAL_DIMENSIONS


def test_frontend_serenity_panel_mirrors_every_backend_dimension():
    """Every backend dimension key must appear in serenity-panel.tsx.

    Renaming or adding a backend dimension without updating the frontend copy
    turns this RED (the frontend hand-copies the list; there is no shared source).
    """
    source = _SERENITY_PANEL.read_text()
    missing = [dim for dim in SCORECARD_DIMENSIONS if dim not in source]
    assert not missing, (
        f"{_SERENITY_PANEL.relative_to(_REPO)} is missing backend scorecard dimension(s) "
        f"{missing}; the frontend hand-copies SCORECARD_DIMENSIONS and must be updated in lockstep"
    )
