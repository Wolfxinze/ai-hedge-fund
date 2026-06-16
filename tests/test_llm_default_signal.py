"""create_default_response must NOT default a signal field to the first Literal
("bullish") on LLM failure — it prefers a neutral member, so an LLM outage cannot
silently fabricate a bullish signal the composite reads as real (PRD O1 / review C1).
"""

from pydantic import BaseModel
from typing_extensions import Literal

from src.utils.llm import create_default_response


class _Signal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    confidence: int


class _SentimentLike(BaseModel):
    sentiment: Literal["positive", "negative", "neutral"]


class _NoNeutral(BaseModel):
    choice: Literal["a", "b"]


def test_signal_defaults_to_neutral_not_bullish():
    out = create_default_response(_Signal)
    assert out.signal == "neutral"  # NOT "bullish" (the first literal)
    assert out.confidence == 0


def test_prefers_neutral_member_when_present():
    assert create_default_response(_SentimentLike).sentiment == "neutral"


def test_falls_back_to_first_literal_when_no_neutral():
    # No neutral member → first value is the only sane default.
    assert create_default_response(_NoNeutral).choice == "a"
