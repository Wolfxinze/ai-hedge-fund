"""Unit tests for seed-universe ingestion (``load_seed_csv``) plus a pipeline-level
test that a rejected row marks the refresh run PARTIAL with ``run.rejected`` set.

No network, no LLM: temp CSVs via ``tmp_path`` and the deterministic stub committee
pattern (see ``test_pipeline.py``) over in-memory sqlite.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.observing_pools.pipeline import RefreshConfig, refresh_pool
from src.observing_pools.universe import load_seed_csv

_HEADER = "ticker,name,exchange,sector,industry,platforms\n"


def _write_csv(tmp_path, rows: str, *, header: str = _HEADER):
    path = tmp_path / "seed.csv"
    path.write_text(header + rows, encoding="utf-8")
    return path


def test_empty_ticker_row_rejected(tmp_path):
    path = _write_csv(tmp_path, ",Nameless,NASDAQ,Technology,Software,ai\n")
    valid, rejected = load_seed_csv(path)
    assert valid == []
    # Empty-ticker rows are keyed by line number (no ticker to key on).
    assert list(rejected.values()) == ["empty ticker"]


@pytest.mark.parametrize(
    "bad_ticker",
    # symbol / >10 chars / no leading letter. (Lowercase is normalized via .upper(),
    # so it is NOT an invalid-format case — the implementation upper-cases first.)
    ["NV$DA", "TOOLONGTICKER", "1NVDA"],
)
def test_invalid_ticker_format_rejected(tmp_path, bad_ticker):
    path = _write_csv(tmp_path, f"{bad_ticker},Bad Co,NASDAQ,Technology,Software,ai\n")
    valid, rejected = load_seed_csv(path)
    assert valid == []
    # Keyed by the upper-cased raw ticker; reason starts with the invalid-format text.
    assert len(rejected) == 1
    reason = next(iter(rejected.values()))
    assert reason.startswith("invalid ticker format")


def test_duplicate_ticker_first_wins_second_rejected(tmp_path):
    path = _write_csv(
        tmp_path,
        "NVDA,NVIDIA,NASDAQ,Technology,Semiconductors,ai\n" "NVDA,NVIDIA Dup,NASDAQ,Technology,Semiconductors,ai\n",
    )
    valid, rejected = load_seed_csv(path)
    assert [r.ticker for r in valid] == ["NVDA"]
    assert valid[0].name == "NVIDIA"  # first occurrence wins
    assert "NVDA" in rejected
    assert rejected["NVDA"].startswith("duplicate ticker")


def test_missing_required_column_raises_naming_it(tmp_path):
    # Drop the ``platforms`` column entirely from the header.
    header = "ticker,name,exchange,sector,industry\n"
    path = _write_csv(tmp_path, "NVDA,NVIDIA,NASDAQ,Technology,Semiconductors\n", header=header)
    with pytest.raises(ValueError) as exc:
        load_seed_csv(path)
    assert "platforms" in str(exc.value)


def test_valid_row_parses_platforms_and_blanks_to_none(tmp_path):
    # ``a;b`` platforms split to ['a', 'b']; blank exchange/sector/industry → None.
    path = _write_csv(tmp_path, "NVDA,NVIDIA,,,,a;b\n")
    valid, rejected = load_seed_csv(path)
    assert rejected == {}
    assert len(valid) == 1
    row = valid[0]
    assert row.ticker == "NVDA"
    assert row.name == "NVIDIA"
    assert row.platforms == ["a", "b"]
    assert row.exchange is None
    assert row.sector is None
    assert row.industry is None


# --- Pipeline-level: a rejected row makes the run PARTIAL with run.rejected set. ---


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _stub(tickers, selected, end_date):
    """Deterministic all-bullish committee at confidence 80 (no network)."""
    signals = {f"{k}_agent": {} for k in selected}
    for t in tickers:
        for k in selected:
            signals[f"{k}_agent"][t] = {"signal": "bullish", "confidence": 80, "reasoning": "stub"}
    return signals, {"calls": len(selected) * len(tickers)}


def test_rejected_row_marks_run_partial(tmp_path, session):
    # One valid AI candidate (curated ``ai`` label → classifies) + one invalid-ticker
    # row that gets rejected. The rejection alone must mark the run PARTIAL.
    path = _write_csv(
        tmp_path,
        "NVDA,NVIDIA,NASDAQ,Technology,Semiconductors,ai\n" "nvda,Bad Lowercase,NASDAQ,Technology,Semiconductors,ai\n",
    )
    config = RefreshConfig(platform_key="ai", universe_csv=str(path), top_n=5, token_budget=10_000)
    run = refresh_pool(session, config, _stub, end_date="2026-06-12")
    session.commit()

    assert run.status == m.RefreshRunStatus.PARTIAL.value
    assert run.rejected  # populated, not None/empty
    assert "NVDA" in run.rejected  # the rejected lowercase row is keyed by upper-cased ticker
