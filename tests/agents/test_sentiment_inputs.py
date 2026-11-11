"""The sentiment analyst must not call a ticker bullish on evidence that is not
there: insider rows with no direction and news items with no sentiment label are
"no evidence", and a ticker whose only directional insider activity is selling
must not come out bullish."""

from src.agents import sentiment as sentiment_module
from src.data.models import CompanyNews, InsiderTrade


def _trade(shares):
    return InsiderTrade(
        ticker="TEST", issuer=None, name="Jane Doe", title="CFO", is_board_director=None,
        transaction_date="2024-12-15", transaction_shares=shares, transaction_price_per_share=None,
        transaction_value=None, shares_owned_before_transaction=None, shares_owned_after_transaction=None,
        security_title=None, filing_date="2024-12-15",
    )


def _news(sentiment):
    return CompanyNews(ticker="TEST", title="t", author="a", source="s", date="2024-12-15", url="u", sentiment=sentiment)


def _run(monkeypatch, trades, news):
    monkeypatch.setattr(sentiment_module, "get_insider_trades", lambda **kwargs: trades)
    monkeypatch.setattr(sentiment_module, "get_company_news", lambda *args, **kwargs: news)
    state = {"data": {"end_date": "2024-12-31", "tickers": ["TEST"], "analyst_signals": {}}, "metadata": {"show_reasoning": False}}
    sentiment_module.sentiment_analyst_agent(state)
    return state["data"]["analyst_signals"]["sentiment_analyst_agent"]["TEST"]


def test_only_sales_is_not_bullish(monkeypatch):
    result = _run(monkeypatch, [_trade(-100), _trade(-50), _trade(None), _trade(None), _trade(None)], [_news(None)] * 10)

    assert result["signal"] == "bearish"
    metrics = result["reasoning"]["insider_trading"]["metrics"]
    assert metrics["bullish_trades"] == 0
    assert metrics["bearish_trades"] == 2


def test_unlabelled_news_is_reported_not_counted(monkeypatch):
    result = _run(monkeypatch, [], [_news(None)] * 7 + [_news("positive")])

    metrics = result["reasoning"]["news_sentiment"]["metrics"]
    assert metrics["total_articles"] == 1
    assert metrics["unscored_articles"] == 7


def test_no_directional_evidence_is_neutral_with_zero_confidence(monkeypatch):
    result = _run(monkeypatch, [_trade(None)] * 20, [_news(None)] * 10)

    assert result["signal"] == "neutral"
    assert result["confidence"] == 0


def test_growth_insider_conviction_tolerates_directionless_trades():
    """A valued option exercise has no direction (shares None); it must be skipped,
    not crash the growth analyst with a None comparison."""
    from src.agents.growth_agent import analyze_insider_conviction

    exercise = _trade(None).model_copy(update={"transaction_value": 45000.0})
    sale = _trade(-100).model_copy(update={"transaction_value": 5000.0})

    result = analyze_insider_conviction([exercise, sale])

    assert result["net_flow_ratio"] == -1
