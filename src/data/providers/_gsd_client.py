"""Direct-HTTP fetchers adapted from the *global-stock-data* skill.

Source: https://github.com/simonlin1212/global-stock-data (``SKILL.md`` v1.0.1,
Simon Lin, Apache-2.0). Only the subset of endpoints needed by
:class:`~src.data.providers.global_stock_data.GlobalStockDataProvider` is vendored
here. Adaptations vs. the upstream skill:

* the Yahoo chart endpoint takes an explicit ``period1``/``period2`` date window
  instead of a coarse ``range`` string, and
* transport/parse exceptions are allowed to propagate so the provider can map them
  onto the project's loud-fail contract (``ProviderFetchError``).

Every function here is a thin wrapper over one free, key-less HTTP source. A
*genuine* empty result is returned as ``[]``/``{}``; a *failure* raises (it is the
provider's job, not this module's, to decide policy).
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

import requests

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_TIMEOUT = 15

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"

_yahoo_session: requests.Session | None = None


# --------------------------------------------------------------------------- #
# Yahoo Finance
# --------------------------------------------------------------------------- #
def yahoo_session() -> requests.Session:
    """Return a cached Yahoo session carrying the cookie+crumb pair.

    quoteSummary / search (v1/v7/v10) require a crumb; the v8 chart endpoint does
    not. The crumb is fetched once and reused for the process lifetime.
    """
    global _yahoo_session
    if _yahoo_session is not None and getattr(_yahoo_session, "_crumb", None):
        return _yahoo_session

    session = requests.Session()
    session.headers["User-Agent"] = _UA
    session.get("https://fc.yahoo.com", timeout=_TIMEOUT)
    response = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=_TIMEOUT)
    response.raise_for_status()
    session._crumb = response.text  # type: ignore[attr-defined]

    _yahoo_session = session
    return session


def reset_yahoo_session() -> None:
    """Drop the cached Yahoo session (used by tests and on crumb expiry)."""
    global _yahoo_session
    _yahoo_session = None


def yahoo_chart(symbol: str, start_date: str, end_date: str, interval: str = "1d") -> list[dict[str, Any]]:
    """Daily OHLCV for ``symbol`` over [start_date, end_date] via the v8 chart API.

    ``symbol`` is a Yahoo symbol (``AAPL`` for US, ``0700.HK`` for HK). Returns a
    list of ``{date, open, high, low, close, volume}`` dicts (rows with a null
    close are skipped). Raises on HTTP/parse failure; returns ``[]`` for an empty
    chart.
    """
    period1 = _epoch(start_date)
    period2 = _epoch(end_date) + 86400  # inclusive of end_date
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "period1": period1, "period2": period2, "events": "div,splits"}
    response = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    response.raise_for_status()

    results = (response.json().get("chart") or {}).get("result") or []
    if not results:
        return []
    chart = results[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]

    rows: list[dict[str, Any]] = []
    for index, ts in enumerate(timestamps):
        close = _at(quote.get("close"), index)
        if close is None:
            continue
        rows.append(
            {
                "date": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d"),
                "open": _at(quote.get("open"), index),
                "high": _at(quote.get("high"), index),
                "low": _at(quote.get("low"), index),
                "close": close,
                "volume": _at(quote.get("volume"), index) or 0,
            }
        )
    return rows


def yahoo_quote_summary(symbol: str, modules: list[str]) -> dict[str, Any]:
    """Fetch one or more quoteSummary modules for ``symbol``."""
    session = yahoo_session()
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    response = session.get(
        url,
        params={"modules": ",".join(modules), "crumb": session._crumb},  # type: ignore[attr-defined]
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    results = (response.json().get("quoteSummary") or {}).get("result") or [{}]
    return results[0] if results else {}


def yahoo_news(query: str, count: int = 50) -> list[dict[str, Any]]:
    """News items for ``query`` (ticker or name) via the v1 search endpoint."""
    session = yahoo_session()
    url = "https://query2.finance.yahoo.com/v1/finance/search"
    response = session.get(url, params={"q": query, "quotesCount": 0, "newsCount": count}, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json().get("news") or []


# --------------------------------------------------------------------------- #
# Sina (US daily K-line fallback) — robust, key-less, no crumb
# --------------------------------------------------------------------------- #
def sina_us_daily(ticker: str, num: int = 360) -> list[dict[str, Any]]:
    """US daily OHLCV from Sina (history back to 1984). Returns newest-last."""
    url = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
    response = requests.get(
        url,
        params={"symbol": ticker.upper(), "num": num},
        headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    match = re.search(r"\((\[.+\])\)", response.text)
    if not match:
        return []
    rows: list[dict[str, Any]] = []
    for item in json.loads(match.group(1)):
        rows.append(
            {
                "date": item.get("d"),
                "open": _to_float(item.get("o")),
                "high": _to_float(item.get("h")),
                "low": _to_float(item.get("l")),
                "close": _to_float(item.get("c")),
                "volume": int(_to_float(item.get("v")) or 0),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Eastmoney (search → secucode resolution, GMAININDICATOR fundamentals)
# --------------------------------------------------------------------------- #
def eastmoney_search(keyword: str, count: int = 10) -> list[dict[str, Any]]:
    """Resolve ``keyword`` to US/HK listings with their Eastmoney market number.

    ``mkt_num``: 105=NASDAQ, 106=NYSE, 107=US other/ETF, 116=HK.
    """
    # Public, shared token shipped with the upstream key-less suggest endpoint — not a user secret.
    params = {"input": keyword, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": count}
    response = requests.get(_SEARCH_URL, params=params, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    response.raise_for_status()
    suggestions = (response.json().get("QuotationCodeTable") or {}).get("Data") or []
    results: list[dict[str, Any]] = []
    for item in suggestions:
        mkt = str(item.get("MktNum", ""))
        if mkt not in ("105", "106", "107", "116"):
            continue
        results.append({"code": item.get("Code"), "name": item.get("Name"), "mkt_num": int(mkt)})
    return results


def eastmoney_datacenter(report_name: str, filter_str: str, page_size: int = 8) -> list[dict[str, Any]]:
    """Generic Eastmoney datacenter query (newest report first)."""
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    response = requests.get(_DATACENTER_URL, params=params, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") or {}
    return result.get("data") or []


def eastmoney_key_indicators(secucode: str, page_size: int = 8) -> list[dict[str, Any]]:
    """GMAININDICATOR key financial ratios for ``secucode`` (e.g. ``AAPL.O`` / ``00700.HK``)."""
    market = "HK" if secucode.endswith(".HK") else "US"
    report_name = f"RPT_{market}F10_FN_GMAININDICATOR"
    return eastmoney_datacenter(report_name, f'(SECUCODE="{secucode}")', page_size=page_size)


# --------------------------------------------------------------------------- #
# Small parse helpers
# --------------------------------------------------------------------------- #
def _epoch(date_str: str) -> int:
    return int(datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp())


def _at(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    return _to_float(values[index])


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
