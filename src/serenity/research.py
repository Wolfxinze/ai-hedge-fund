"""Build a Serenity research record from a bottleneck hypothesis + references.

The caller (human or an LLM proposing a bottleneck) supplies the scorecard and a
list of source URLs with claim/excerpt text. This module derives source types +
substantiation + the computed grade deterministically and persists the record
with a non-null disclaimer (PRD v4 §9.6, §9.9).
"""

import logging

from sqlalchemy.orm import Session

from src.compliance import research_disclaimer
from src.serenity.evidence import classify_reference
from src.serenity.fetch import fetch_excerpt
from src.serenity.grading import grade_evidence, recommended_action, serenity_score
from src.storage.models import (
    EvidenceGrade,
    EvidenceReference,
    SerenityResearchRecord,
)

logger = logging.getLogger(__name__)


def build_record(
    session: Session,
    *,
    theme: str,
    references: list[dict],
    scorecard: dict,
    ticker: str | None = None,
    platform_key: str | None = None,
    chain_layer: str | None = None,
    bottleneck_hypothesis: str | None = None,
    risks: list[str] | None = None,
    downgrade_triggers: list[str] | None = None,
    min_grade: EvidenceGrade = EvidenceGrade.C,
    fetch_missing: bool = False,
    fetch_headers: dict[str, str] | None = None,
) -> SerenityResearchRecord:
    """Create + persist a SerenityResearchRecord and its EvidenceReferences.

    ``references`` items: ``{source_url, claim_summary?, excerpt?}``. When
    ``fetch_missing`` is True, a reference with no excerpt is fetched through the
    SSRF-guarded fetcher (src.serenity.fetch); a blocked/failed fetch leaves the
    excerpt None so the reference simply stays unsubstantiated — the record always
    persists. Default False keeps the path offline/deterministic (research-only,
    single-user-local); live fetch is opt-in.

    ``fetch_headers`` is an opaque header dict forwarded to the fetcher per reference
    (e.g. the EDGAR adapter's declared User-Agent). research stays source-agnostic —
    it never inspects the headers; the SSRF guard is unaffected (see fetch_excerpt).
    """
    classified = []
    for ref in references:
        excerpt = ref.get("excerpt")
        if not excerpt and fetch_missing:
            try:
                result = fetch_excerpt(ref["source_url"], headers=fetch_headers)
                excerpt = result.excerpt if result.ok else None
                if not result.ok:
                    # Security-relevant blocks (an SSRF attempt) are warnings; benign misses are info.
                    level = logging.WARNING if result.reason in ("blocked_redirect", "blocked_private_ip", "blocked_scheme") else logging.INFO
                    logger.log(level, "serenity evidence not fetched (%s): %s", result.reason, ref["source_url"])
            except Exception as exc:  # a record must persist even if a fetch blows up
                logger.warning("serenity fetch error for %s: %s", ref["source_url"], exc)
                excerpt = None
        cls = classify_reference(
            source_url=ref["source_url"],
            claim_summary=ref.get("claim_summary"),
            excerpt=excerpt,
        )
        if not cls["substantiated"]:
            # Make a withheld grade auditable in logs; the reason is also persisted
            # onto evidence_references.reason below (§11.5 durability, audit-only).
            logger.info("serenity reference unsubstantiated (%s): %s", cls.get("reason"), ref["source_url"])
        classified.append(
            {**cls, "source_url": ref["source_url"], "claim_summary": ref.get("claim_summary"), "excerpt": excerpt}
        )

    grade = grade_evidence(classified)
    score = serenity_score(scorecard, grade, min_grade=min_grade)
    action = recommended_action(score, grade)
    disclaimer, disclaimer_version = research_disclaimer()

    record = SerenityResearchRecord(
        ticker=ticker,
        platform_key=platform_key,
        theme=theme,
        chain_layer=chain_layer,
        bottleneck_hypothesis=bottleneck_hypothesis,
        scorecard=scorecard,
        evidence_grade=grade.value,
        serenity_score=score,
        recommended_action=action.value,
        risks=risks,
        downgrade_triggers=downgrade_triggers,
        disclaimer=disclaimer,
        disclaimer_version=disclaimer_version,
    )
    session.add(record)
    session.flush()  # need record.id for evidence FK

    for c in classified:
        session.add(
            EvidenceReference(
                record_id=record.id,
                source_url=c["source_url"],
                source_host=c["source_host"],
                source_type=c["source_type"].value,
                substantiated=c["substantiated"],
                reason=c.get("reason"),  # §11.5: persist the already-computed reason (audit-only)
                excerpt=c["excerpt"],
                claim_summary=c["claim_summary"],
            )
        )
    session.flush()
    return record
