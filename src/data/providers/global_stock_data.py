"""``global-stock-data`` financial data provider (free, no API key).

Wraps the direct-HTTP fetchers in :mod:`src.data.providers._gsd_client` (adapted
from Simon Lin's *global-stock-data* skill, Apache-2.0) behind the project's
:class:`FinancialDataProvider` contract.

Why this provider exists alongside ``yfinance``:

* **No ``yfinance`` package dependency** — pure ``requests`` against the same
  underlying free sources, so it works where the package's scraping breaks.
* **Native US + HK coverage** — HK tickers (``0700.HK`` / ``00700`` / ``700``) are
  normalised automatically.
* **Multi-source resilience** — prices fall back from Yahoo to Sina (US), and
  fundamentals fall back from Yahoo quoteSummary to Eastmoney's GMAININDICATOR,
  which stays reachable from mainland China.

Fundamentals caveat: Yahoo's quoteSummary statement-history endpoint has been
pared back so that only ``revenue`` and ``net income`` come through reliably (the
rest arrive as ``0``/empty placeholders). The latest period's ratios are therefore
sourced from Yahoo's still-rich precomputed snapshot (``financialData`` /
``defaultKeyStatistics``); older per-period ratios are limited to what the
statements still carry. When Yahoo returns no statement rows at all, the whole
fundamentals path falls back to Eastmoney's GMAININDICATOR, which does provide
multi-period ratios for US + HK.

Source priority follows the upstream skill's own table. ``get_insider_trades``
returns an empty list because global-stock-data exposes no insider-transactions
endpoint — that is genuine no-data, not a failure, so it stays falsy per the
loud-fail contract.
"""

from __future__ import annotations

import datetime
from typing import Any

from src.data.cache import get_cache
from src.data.models import CompanyNews, FinancialMetrics, InsiderTrade, LineItem, Price
from src.data.providers import _gsd_client as client
from src.data.providers.base import FinancialDataProvider
from src.data.providers.exceptions import ProviderFetchError

_cache = get_cache()

# Yahoo quoteSummary statement field -> canonical line item, grouped by statement.
_INCOME_FIELDS = {
    "revenue": ["totalRevenue"],
    "gross_profit": ["grossProfit"],
    "operating_income": ["operatingIncome"],
    "ebit": ["ebit", "operatingIncome"],
    "net_income": ["netIncome"],
    "research_and_development": ["researchDevelopment"],
    "operating_expense": ["totalOperatingExpenses"],
    "interest_expense": ["interestExpense"],
}
_BALANCE_FIELDS = {
    "total_assets": ["totalAssets"],
    "total_liabilities": ["totalLiab"],
    "shareholders_equity": ["totalStockholderEquity"],
    "current_assets": ["totalCurrentAssets"],
    "current_liabilities": ["totalCurrentLiabilities"],
    "cash_and_equivalents": ["cash", "cashAndCashEquivalents"],
}
_CASHFLOW_FIELDS = {
    "operating_cash_flow": ["totalCashFromOperatingActivities"],
    "capital_expenditure": ["capitalExpenditures"],
    "dividends_and_other_cash_distributions": ["dividendsPaid"],
    "depreciation_and_amortization": ["depreciation"],
}

# Flow items are summed over trailing quarters for TTM; stock items are point-in-time.
_FLOW_ITEMS = (
    set(_INCOME_FIELDS)
    | set(_CASHFLOW_FIELDS)
    | {"ebitda", "free_cash_flow", "earnings_per_share"}
)

# Canonical line item -> key in the Yahoo key_statistics snapshot. Used to fill the
# latest period in search_line_items for items the gutted statements no longer carry
# (free_cash_flow, total_debt, ebitda, ...). capital_expenditure is derived below.
_STATS_LINE_ITEMS = {
    "revenue": "total_revenue",
    "net_income": "net_income",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "ebitda": "ebitda",
    "free_cash_flow": "free_cash_flow",
    "operating_cash_flow": "operating_cash_flow",
    "total_debt": "total_debt",
    "cash_and_equivalents": "total_cash",
    "outstanding_shares": "shares_outstanding",
    "earnings_per_share": "trailing_eps",
    "book_value_per_share": "book_value_per_share",
}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if previous == 0:
        return 0.0 if current == 0 else None
    return (current - previous) / abs(previous)


def _pct(value: Any) -> float | None:
    """Convert an Eastmoney percentage (e.g. ``18.5``) to a ratio (``0.185``)."""
    numeric = _safe_float(value)
    return numeric / 100.0 if numeric is not None else None


def _raw(node: Any) -> float | None:
    """Pull ``.raw`` out of a Yahoo ``{raw, fmt}`` node (or a bare number)."""
    if isinstance(node, dict):
        return _safe_float(node.get("raw"))
    return _safe_float(node)


def _stmt_raw(node: Any) -> float | None:
    """Like :func:`_raw`, but treats Yahoo's ``{"raw": 0, "fmt": null}`` placeholder
    as missing.

    Yahoo's quoteSummary statement-history endpoint has been pared back so that
    most line items (grossProfit, ebit, costOfRevenue, balance-sheet and
    cash-flow rows) come back as a bare ``0`` with a null ``fmt`` — its signature
    for "no data". Reading those as a literal zero leaks junk into reconstructed
    ratios, so we treat them as missing here. Genuine values (totalRevenue,
    netIncome) keep a non-null ``fmt`` and pass through unchanged.
    """
    if isinstance(node, dict):
        raw = node.get("raw")
        if raw in (None, ""):
            return None
        if raw == 0 and node.get("fmt") is None:
            return None
        return _safe_float(raw)
    return _safe_float(node)


def _is_hk(ticker: str) -> bool:
    cleaned = ticker.upper().strip()
    return cleaned.endswith(".HK") or cleaned.isdigit()


def _to_yahoo_symbol(ticker: str) -> str:
    """Normalise a user ticker to a Yahoo symbol (``700`` -> ``0700.HK``)."""
    cleaned = ticker.upper().strip()
    if not _is_hk(cleaned):
        return cleaned
    digits = cleaned.replace(".HK", "").lstrip("0") or "0"
    return f"{digits.zfill(4)}.HK"


def _to_eastmoney_secucode(ticker: str, mkt_num: int, code: str) -> str:
    if mkt_num == 116:
        return f"{code.zfill(5)}.HK"
    if mkt_num == 106:
        return f"{code}.N"
    return f"{code}.O"  # 105 NASDAQ / 107 other


def _em_key(value: str) -> str:
    """Normalise a ticker/code for matching (drop .HK suffix, case, leading zeros)."""
    return value.upper().replace(".HK", "").lstrip("0")


def _pick_eastmoney_match(matches: list[dict[str, Any]], ticker: str) -> dict[str, Any]:
    """Prefer an exact code match for ``ticker`` before falling back to the top hit.

    The suggest endpoint ranks by relevance, but a bare numeric/short code can
    substring-match a different listing; an exact-code preference stops the
    fallback from silently resolving fundamentals for the wrong security.
    """
    want = _em_key(ticker)
    for match in matches:
        if _em_key(str(match.get("code") or "")) == want:
            return match
    return matches[0]


class GlobalStockDataProvider(FinancialDataProvider):
    name = "globalstockdata"

    # ------------------------------------------------------------------ #
    # Prices
    # ------------------------------------------------------------------ #
    def get_prices(self, ticker: str, start_date: str, end_date: str, api_key: str | None = None) -> list[Price]:
        cache_key = f"{self.name}:{ticker}_{start_date}_{end_date}"
        if cached := _cache.get_prices(cache_key):
            return [Price(**price) for price in cached]

        symbol = _to_yahoo_symbol(ticker)
        try:
            rows = client.yahoo_chart(symbol, start_date, end_date)
            if not rows and not _is_hk(ticker):
                rows = self._sina_window(ticker, start_date, end_date)
        except Exception as exc:
            if _is_hk(ticker):
                raise ProviderFetchError(f"global-stock-data price fetch failed for {ticker}") from exc
            try:
                rows = self._sina_window(ticker, start_date, end_date)
            except Exception as fallback_exc:
                raise ProviderFetchError(f"global-stock-data price fetch failed for {ticker}") from fallback_exc

        prices: list[Price] = []
        for row in rows:
            open_, close, high, low = row.get("open"), row.get("close"), row.get("high"), row.get("low")
            if None in (open_, close, high, low):
                continue
            prices.append(
                Price(
                    open=open_,
                    close=close,
                    high=high,
                    low=low,
                    volume=int(row.get("volume") or 0),
                    time=f"{row['date']}T00:00:00",
                )
            )
        if not prices:
            return []

        _cache.set_prices(cache_key, [p.model_dump() for p in prices])
        return prices

    def _sina_window(self, ticker: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        rows = client.sina_us_daily(ticker)
        return [row for row in rows if row.get("date") and start_date <= row["date"] <= end_date]

    # ------------------------------------------------------------------ #
    # Fundamentals
    # ------------------------------------------------------------------ #
    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
        api_key: str | None = None,
    ) -> list[FinancialMetrics]:
        cache_key = f"{self.name}:{ticker}_{period}_{end_date}_{limit}"
        if cached := _cache.get_financial_metrics(cache_key):
            return [FinancialMetrics(**metric) for metric in cached]

        symbol = _to_yahoo_symbol(ticker)
        try:
            stats = self._key_statistics(symbol)
            records = self._statement_records(symbol, end_date, period, limit, items=list(_all_items()))
            if records:
                metrics = self._metrics_from_statements(ticker, period, records, stats)
            else:
                metrics = self._metrics_from_eastmoney(ticker, end_date, period, limit, stats)
        except ProviderFetchError:
            raise
        except Exception as exc:
            raise ProviderFetchError(f"global-stock-data financial metrics fetch failed for {ticker}") from exc

        if not metrics:
            return []
        _cache.set_financial_metrics(cache_key, [m.model_dump() for m in metrics])
        return metrics

    def search_line_items(
        self,
        ticker: str,
        line_items: list[str],
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
        api_key: str | None = None,
    ) -> list[LineItem]:
        cache_key = f"{self.name}:line_items:{ticker}_{','.join(line_items)}_{period}_{end_date}_{limit}"
        if cached := _cache.get_line_items(cache_key):
            return [LineItem(**item) for item in cached]

        symbol = _to_yahoo_symbol(ticker)
        try:
            records = self._statement_records(symbol, end_date, period, limit, items=line_items)
        except ProviderFetchError:
            raise
        except Exception as exc:
            raise ProviderFetchError(f"global-stock-data line item fetch failed for {ticker}") from exc

        stats: dict[str, Any] | None = None
        results: list[LineItem] = []
        for index, record in enumerate(records):
            payload = {
                "ticker": ticker.upper(),
                "report_period": record["report_period"],
                "period": period,
                "currency": record.get("currency") or "USD",
            }
            for item in line_items:
                payload[item] = record.get(item)
            # Fill the latest period from the precomputed snapshot for items the gutted
            # quoteSummary statements no longer return (free_cash_flow, total_debt, ...).
            if index == 0:
                if stats is None:
                    stats = self._key_statistics(symbol)
                payload["currency"] = stats.get("currency") or payload["currency"]
                for item in line_items:
                    if payload.get(item) is None:
                        payload[item] = _stats_line_item(item, stats)
            results.append(LineItem(**payload))

        if results:
            _cache.set_line_items(cache_key, [item.model_dump() for item in results])
        return results

    def get_market_cap(self, ticker: str, end_date: str, api_key: str | None = None) -> float | None:
        symbol = _to_yahoo_symbol(ticker)
        try:
            stats = self._key_statistics(symbol)
        except Exception as exc:
            raise ProviderFetchError(f"global-stock-data market cap fetch failed for {ticker}") from exc
        return stats.get("market_cap")

    # ------------------------------------------------------------------ #
    # News
    # ------------------------------------------------------------------ #
    def get_company_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
        api_key: str | None = None,
    ) -> list[CompanyNews]:
        cache_key = f"{self.name}:{ticker}_{start_date or 'none'}_{end_date}_{limit}"
        if cached := _cache.get_company_news(cache_key):
            return [CompanyNews(**news) for news in cached]

        try:
            items = client.yahoo_news(_to_yahoo_symbol(ticker), count=min(limit, 50))
        except Exception as exc:
            raise ProviderFetchError(f"global-stock-data news fetch failed for {ticker}") from exc

        start = _date(start_date)
        end = _date(end_date)
        news: list[CompanyNews] = []
        for item in items:
            published = item.get("providerPublishTime")
            # Undated items fall back to "now" (not end_date) so the date filter
            # still applies instead of always admitting them at the window edge.
            when = (
                datetime.datetime.fromtimestamp(published, datetime.timezone.utc)
                if published
                else datetime.datetime.now(datetime.timezone.utc)
            )
            date_str = when.isoformat()
            parsed = _date(date_str)
            if start and parsed and parsed < start:
                continue
            if end and parsed and parsed > end:
                continue
            news.append(
                CompanyNews(
                    ticker=ticker.upper(),
                    title=item.get("title") or "",
                    author=item.get("publisher") or "",
                    source=item.get("publisher") or "",
                    date=date_str,
                    url=item.get("link") or "",
                    sentiment=None,
                )
            )
            if len(news) >= limit:
                break

        if news:
            _cache.set_company_news(cache_key, [n.model_dump() for n in news])
        return news

    # ------------------------------------------------------------------ #
    # Insider trades — not provided by global-stock-data (genuine no-data)
    # ------------------------------------------------------------------ #
    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
        api_key: str | None = None,
    ) -> list[InsiderTrade]:
        return []

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _key_statistics(self, symbol: str) -> dict[str, Any]:
        """Yahoo's precomputed valuation + TTM ratio snapshot.

        ``financialData`` / ``defaultKeyStatistics`` survive Yahoo's gutting of the
        raw statement endpoint, so they are the authoritative source for the latest
        period's profitability, liquidity, leverage and cash-flow ratios. These are
        a current snapshot only — they apply to the most recent period, not history.
        """
        data = client.yahoo_quote_summary(symbol, ["financialData", "defaultKeyStatistics", "summaryDetail", "price"])
        fd = data.get("financialData") or {}
        ks = data.get("defaultKeyStatistics") or {}
        sd = data.get("summaryDetail") or {}
        px = data.get("price") or {}

        total_revenue = _raw(fd.get("totalRevenue"))
        total_assets = _raw(ks.get("totalAssets"))
        total_debt = _raw(fd.get("totalDebt"))
        total_cash = _raw(fd.get("totalCash"))
        shares = _raw(ks.get("sharesOutstanding"))
        book_value_per_share = _raw(ks.get("bookValue"))
        operating_margin = _raw(fd.get("operatingMargins"))
        debt_to_equity = _raw(fd.get("debtToEquity"))  # Yahoo reports this as a percent

        equity = book_value_per_share * shares if (book_value_per_share is not None and shares) else None
        operating_income = operating_margin * total_revenue if (operating_margin is not None and total_revenue is not None) else None
        invested_capital = (total_debt or 0) + equity - (total_cash or 0) if equity is not None else None

        return {
            # Valuation / size
            "market_cap": _raw(sd.get("marketCap")) or _raw(px.get("marketCap")),
            "enterprise_value": _raw(ks.get("enterpriseValue")),
            "trailing_pe": _raw(sd.get("trailingPE")),
            "price_to_book": _raw(ks.get("priceToBook")),
            "price_to_sales": _raw(sd.get("priceToSalesTrailing12Months")) or _raw(ks.get("priceToSalesTrailing12Months")),
            "ev_to_ebitda": _raw(ks.get("enterpriseToEbitda")),
            "ev_to_revenue": _raw(ks.get("enterpriseToRevenue")),
            "peg_ratio": _raw(ks.get("pegRatio")),
            "shares_outstanding": shares,
            "trailing_eps": _raw(ks.get("trailingEps")),
            "payout_ratio": _raw(sd.get("payoutRatio")),
            # Profitability / efficiency (precomputed TTM ratios)
            "gross_margin": _raw(fd.get("grossMargins")),
            "operating_margin": operating_margin,
            "net_margin": _raw(fd.get("profitMargins")) or _raw(ks.get("profitMargins")),
            "return_on_equity": _raw(fd.get("returnOnEquity")),
            "return_on_assets": _raw(fd.get("returnOnAssets")),
            "return_on_invested_capital": _ratio(operating_income, invested_capital),
            "asset_turnover": _ratio(total_revenue, total_assets),
            # Liquidity / leverage
            "current_ratio": _raw(fd.get("currentRatio")),
            "quick_ratio": _raw(fd.get("quickRatio")),
            "debt_to_equity": debt_to_equity / 100.0 if debt_to_equity is not None else None,
            "debt_to_assets": _ratio(total_debt, total_assets),
            # Growth (YoY)
            "revenue_growth": _raw(fd.get("revenueGrowth")),
            "earnings_growth": _raw(fd.get("earningsGrowth")),
            "eps_growth": _raw(ks.get("earningsQuarterlyGrowth")),
            # Absolute line-item values (for search_line_items latest-period overlay)
            "net_income": _raw(ks.get("netIncomeToCommon")),
            "operating_income": operating_income,
            "gross_profit": _raw(fd.get("grossProfits")),
            "ebitda": _raw(fd.get("ebitda")),
            "free_cash_flow": _raw(fd.get("freeCashflow")),
            "operating_cash_flow": _raw(fd.get("operatingCashflow")),
            "total_debt": total_debt,
            "total_cash": total_cash,
            "total_revenue": total_revenue,
            "total_assets": total_assets,
            "book_value_per_share": book_value_per_share,
            "currency": px.get("currency") or fd.get("financialCurrency") or "USD",
        }

    def _statement_records(self, symbol: str, end_date: str, period: str, limit: int, items: list[str]) -> list[dict[str, Any]]:
        quarterly = period.lower() in {"ttm", "quarter", "quarterly"}
        is_ttm = period.lower() == "ttm"
        suffix = "Quarterly" if quarterly else ""
        data = client.yahoo_quote_summary(
            symbol,
            [f"incomeStatementHistory{suffix}", f"balanceSheetHistory{suffix}", f"cashflowStatementHistory{suffix}"],
        )
        by_period = self._statements_by_period(data, suffix)
        periods = [p for p in sorted(by_period, reverse=True) if p <= end_date]
        if not periods:
            return []

        records: list[dict[str, Any]] = []
        for index, report_period in enumerate(periods[:limit]):
            trailing = periods[index : index + 4] if is_ttm else [report_period]
            if is_ttm and len(trailing) < 4:
                continue
            record: dict[str, Any] = {"report_period": report_period, "currency": "USD"}
            for item in items:
                record[item] = self._resolve_item(by_period, report_period, trailing, item, is_ttm)
            records.append(record)
        return records

    def _statements_by_period(self, data: dict[str, Any], suffix: str) -> dict[str, dict[str, float | None]]:
        groups = {
            "income": (data.get(f"incomeStatementHistory{suffix}") or {}).get("incomeStatementHistory") or [],
            "balance": (data.get(f"balanceSheetHistory{suffix}") or {}).get("balanceSheetStatements") or [],
            "cashflow": (data.get(f"cashflowStatementHistory{suffix}") or {}).get("cashflowStatements") or [],
        }
        field_maps = {"income": _INCOME_FIELDS, "balance": _BALANCE_FIELDS, "cashflow": _CASHFLOW_FIELDS}

        by_period: dict[str, dict[str, float | None]] = {}
        for group, rows in groups.items():
            for row in rows:
                period_key = (row.get("endDate") or {}).get("fmt")
                if not period_key:
                    continue
                values = by_period.setdefault(period_key, {})
                for canonical, aliases in field_maps[group].items():
                    if values.get(canonical) is not None:
                        continue
                    for alias in aliases:
                        raw = _stmt_raw(row.get(alias))
                        if raw is not None:
                            values[canonical] = raw
                            break
        return by_period

    def _resolve_item(self, by_period, report_period, trailing, item, is_ttm) -> float | None:
        if item == "free_cash_flow":
            ocf = self._resolve_item(by_period, report_period, trailing, "operating_cash_flow", is_ttm)
            capex = self._resolve_item(by_period, report_period, trailing, "capital_expenditure", is_ttm)
            return None if ocf is None else ocf + (capex or 0)
        if item == "ebitda":
            ebit = self._resolve_item(by_period, report_period, trailing, "ebit", is_ttm)
            dep = self._resolve_item(by_period, report_period, trailing, "depreciation_and_amortization", is_ttm)
            return None if ebit is None else ebit + (dep or 0)

        if is_ttm and item in _FLOW_ITEMS:
            values = [by_period.get(p, {}).get(item) for p in trailing]
            present = [v for v in values if v is not None]
            # Require all four trailing quarters: a partial sum mislabeled "ttm"
            # would understate the figure and corrupt every ratio built on it.
            return sum(present) if len(present) == 4 else None
        return by_period.get(report_period, {}).get(item)

    def _metrics_from_statements(self, ticker, period, records, stats) -> list[FinancialMetrics]:
        """Build metrics from per-period statements, overlaying Yahoo's precomputed
        ratios onto the latest period.

        Yahoo's quoteSummary statements now carry only ``revenue`` and ``net_income``
        reliably, so per-period reconstruction covers margins/growth from those two
        and little else. The latest period is therefore filled from ``stats`` (the
        ``financialData`` snapshot); older periods keep whatever the statements still
        provide. ``stats`` values are preferred only when present, so statement-driven
        fallbacks survive when a ratio is absent.
        """
        shares = stats.get("shares_outstanding")
        metrics: list[FinancialMetrics] = []
        for index, record in enumerate(records):
            previous = records[index + 1] if index + 1 < len(records) else {}
            is_latest = index == 0

            def latest(key: str, computed: float | None) -> float | None:
                """Prefer the precomputed snapshot value on the latest period only."""
                if is_latest and stats.get(key) is not None:
                    return stats.get(key)
                return computed

            market_cap = stats.get("market_cap") if is_latest else None
            enterprise_value = stats.get("enterprise_value") if is_latest else None

            revenue = record.get("revenue")
            net_income = record.get("net_income")
            equity = record.get("shareholders_equity")
            total_assets = record.get("total_assets")
            cash = record.get("cash_and_equivalents")
            debt = record.get("total_debt")
            current_assets = record.get("current_assets")
            current_liabilities = record.get("current_liabilities")
            ebit = record.get("ebit")
            ebitda = record.get("ebitda")
            interest_expense = record.get("interest_expense")
            free_cash_flow = stats.get("free_cash_flow") if is_latest else None
            if free_cash_flow is None:
                free_cash_flow = record.get("free_cash_flow")
            eps = stats.get("trailing_eps") if is_latest else None

            metrics.append(
                FinancialMetrics(
                    ticker=ticker.upper(),
                    report_period=record["report_period"],
                    period=period,
                    currency=stats.get("currency") or record.get("currency") or "USD",
                    market_cap=market_cap,
                    enterprise_value=enterprise_value,
                    price_to_earnings_ratio=stats.get("trailing_pe") if is_latest else None,
                    price_to_book_ratio=stats.get("price_to_book") if is_latest else None,
                    price_to_sales_ratio=stats.get("price_to_sales") if is_latest else None,
                    enterprise_value_to_ebitda_ratio=stats.get("ev_to_ebitda") if is_latest else _ratio(enterprise_value, ebitda),
                    enterprise_value_to_revenue_ratio=stats.get("ev_to_revenue") if is_latest else _ratio(enterprise_value, revenue),
                    free_cash_flow_yield=_ratio(free_cash_flow, market_cap),
                    peg_ratio=stats.get("peg_ratio") if is_latest else None,
                    gross_margin=latest("gross_margin", _ratio(record.get("gross_profit"), revenue)),
                    operating_margin=latest("operating_margin", _ratio(record.get("operating_income"), revenue)),
                    net_margin=latest("net_margin", _ratio(net_income, revenue)),
                    return_on_equity=latest("return_on_equity", _ratio(net_income, equity)),
                    return_on_assets=latest("return_on_assets", _ratio(net_income, total_assets)),
                    return_on_invested_capital=latest("return_on_invested_capital", _ratio(record.get("operating_income"), (debt or 0) + (equity or 0) - (cash or 0))),
                    asset_turnover=latest("asset_turnover", _ratio(revenue, total_assets)),
                    inventory_turnover=None,
                    receivables_turnover=None,
                    days_sales_outstanding=None,
                    operating_cycle=None,
                    working_capital_turnover=_ratio(revenue, (current_assets or 0) - (current_liabilities or 0)),
                    current_ratio=latest("current_ratio", _ratio(current_assets, current_liabilities)),
                    quick_ratio=latest("quick_ratio", None),
                    cash_ratio=_ratio(cash, current_liabilities),
                    operating_cash_flow_ratio=_ratio(record.get("operating_cash_flow"), current_liabilities),
                    debt_to_equity=latest("debt_to_equity", _ratio(debt, equity)),
                    debt_to_assets=latest("debt_to_assets", _ratio(debt, total_assets)),
                    interest_coverage=_ratio(ebit, abs(interest_expense)) if interest_expense else None,
                    revenue_growth=latest("revenue_growth", _growth(revenue, previous.get("revenue"))),
                    earnings_growth=latest("earnings_growth", _growth(net_income, previous.get("net_income"))),
                    book_value_growth=_growth(equity, previous.get("shareholders_equity")),
                    earnings_per_share_growth=latest("eps_growth", None),
                    free_cash_flow_growth=_growth(free_cash_flow, previous.get("free_cash_flow")),
                    operating_income_growth=_growth(record.get("operating_income"), previous.get("operating_income")),
                    ebitda_growth=_growth(ebitda, previous.get("ebitda")),
                    payout_ratio=stats.get("payout_ratio") if is_latest else None,
                    earnings_per_share=eps,
                    book_value_per_share=latest("book_value_per_share", _ratio(equity, shares)),
                    free_cash_flow_per_share=_ratio(free_cash_flow, shares),
                )
            )
        return metrics

    def _metrics_from_eastmoney(self, ticker, end_date, period, limit, stats) -> list[FinancialMetrics]:
        """Fallback ratios from Eastmoney GMAININDICATOR (China-accessible, key-less)."""
        matches = client.eastmoney_search(ticker)
        if not matches:
            return []
        top = _pick_eastmoney_match(matches, ticker)
        secucode = _to_eastmoney_secucode(ticker, top["mkt_num"], str(top["code"]))
        rows = client.eastmoney_key_indicators(secucode, page_size=limit + 4)
        rows = [r for r in rows if (r.get("REPORT_DATE") or "")[:10] <= end_date][:limit]
        if not rows:
            return []

        metrics: list[FinancialMetrics] = []
        for index, row in enumerate(rows):
            is_latest = index == 0
            report_period = (row.get("REPORT_DATE") or "")[:10]
            metrics.append(
                FinancialMetrics(
                    ticker=ticker.upper(),
                    report_period=report_period,
                    period=period,
                    currency=stats.get("currency") or "USD",
                    market_cap=stats.get("market_cap") if is_latest else None,
                    enterprise_value=stats.get("enterprise_value") if is_latest else None,
                    price_to_earnings_ratio=stats.get("trailing_pe") if is_latest else None,
                    price_to_book_ratio=stats.get("price_to_book") if is_latest else None,
                    price_to_sales_ratio=stats.get("price_to_sales") if is_latest else None,
                    enterprise_value_to_ebitda_ratio=stats.get("ev_to_ebitda") if is_latest else None,
                    enterprise_value_to_revenue_ratio=stats.get("ev_to_revenue") if is_latest else None,
                    free_cash_flow_yield=None,
                    peg_ratio=stats.get("peg_ratio") if is_latest else None,
                    gross_margin=_pct(row.get("GROSS_PROFIT_RATIO")),
                    operating_margin=None,
                    net_margin=_pct(row.get("NET_PROFIT_RATIO")),
                    return_on_equity=_pct(row.get("ROE_AVG")),
                    return_on_assets=_pct(row.get("ROA")),
                    return_on_invested_capital=_pct(row.get("ROIC")),
                    asset_turnover=None,
                    inventory_turnover=None,
                    receivables_turnover=None,
                    days_sales_outstanding=None,
                    operating_cycle=None,
                    working_capital_turnover=None,
                    current_ratio=_safe_float(row.get("CURRENT_RATIO")),
                    quick_ratio=None,
                    cash_ratio=None,
                    operating_cash_flow_ratio=None,
                    debt_to_equity=None,
                    debt_to_assets=_pct(row.get("DEBT_ASSET_RATIO")),
                    interest_coverage=None,
                    revenue_growth=_pct(row.get("OPERATE_INCOME_YOY")),
                    earnings_growth=None,
                    book_value_growth=None,
                    earnings_per_share_growth=_pct(row.get("BASIC_EPS_YOY")),
                    free_cash_flow_growth=None,
                    operating_income_growth=None,
                    ebitda_growth=None,
                    payout_ratio=stats.get("payout_ratio") if is_latest else None,
                    earnings_per_share=_safe_float(row.get("BASIC_EPS")),
                    book_value_per_share=_safe_float(row.get("BPS")),
                    free_cash_flow_per_share=_safe_float(row.get("PER_NETCASH_OPERATE")),
                )
            )
        return metrics


def _all_items() -> set[str]:
    return set(_INCOME_FIELDS) | set(_BALANCE_FIELDS) | set(_CASHFLOW_FIELDS) | {"ebitda", "free_cash_flow"}


def _stats_line_item(item: str, stats: dict[str, Any]) -> float | None:
    """Resolve a single canonical line item from the Yahoo key_statistics snapshot."""
    if item == "capital_expenditure":
        ocf, fcf = stats.get("operating_cash_flow"), stats.get("free_cash_flow")
        return fcf - ocf if (ocf is not None and fcf is not None) else None  # capex is OCF - FCF (negative)
    key = _STATS_LINE_ITEMS.get(item)
    return stats.get(key) if key else None


def _date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
