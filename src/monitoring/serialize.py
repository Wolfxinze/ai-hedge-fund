"""The serialization chokepoints for product output (PRD v4 §12, M7, §9.9).

Every export path and API projection of a research record MUST go through the
matching ``serialize_*`` function. They refuse to emit a record missing a
disclaimer — a column (NOT NULL) + a DB CHECK is not enough on its own; the
disclaimer cannot be dropped by a projection that forgets it, and the ``.strip()``
here also rejects exotic whitespace (e.g. a non-breaking space) that the SQLite
CHECK's ASCII ``trim()`` admits, so the serialization and DB layers compose.
"""

from src.storage.models import OpportunityReport, SerenityResearchRecord


class DisclaimerError(ValueError):
    """Raised when a record would be emitted without a disclaimer."""


def serialize_report(report: OpportunityReport) -> dict:
    """Project an OpportunityReport to a dict, enforcing the disclaimer invariant."""
    disclaimer = (report.disclaimer or "").strip()
    version = (report.disclaimer_version or "").strip()
    if not disclaimer or not version:
        raise DisclaimerError(f"refusing to serialize report id={getattr(report, 'id', '?')} ticker={report.ticker}: " "missing disclaimer/disclaimer_version")
    return {
        "id": report.id,
        "monitor_id": report.monitor_id,
        "ticker": report.ticker,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "label": report.label,
        "confidence": report.confidence,
        "degraded": report.degraded,
        "time_horizon": report.time_horizon,
        "summary": report.summary,
        "agent_signals": report.agent_signals,
        "serenity_context": report.serenity_context,
        "risks": report.risks,
        "next_checks": report.next_checks,
        "disclaimer": disclaimer,
        "disclaimer_version": version,
    }


def serialize_serenity(record: SerenityResearchRecord) -> dict:
    """Project a SerenityResearchRecord to a dict, enforcing the disclaimer invariant.

    The serenity research GET route projects through here so its disclaimer is guarded
    at the serialization layer too (parity with ``serialize_report`` for opportunity
    reports), not only by the DB NOT NULL + CHECK.
    """
    disclaimer = (record.disclaimer or "").strip()
    version = (record.disclaimer_version or "").strip()
    if not disclaimer or not version:
        raise DisclaimerError(f"refusing to serialize serenity record id={getattr(record, 'id', '?')} ticker={record.ticker}: " "missing disclaimer/disclaimer_version")
    return {
        "id": record.id,
        "ticker": record.ticker,
        "platform_key": record.platform_key,
        "theme": record.theme,
        "chain_layer": record.chain_layer,
        "bottleneck_hypothesis": record.bottleneck_hypothesis,
        "evidence_grade": record.evidence_grade,
        "serenity_score": record.serenity_score,
        "recommended_action": record.recommended_action,
        "disclaimer": disclaimer,
        "disclaimer_version": version,
    }
