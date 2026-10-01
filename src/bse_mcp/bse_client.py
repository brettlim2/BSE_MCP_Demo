"""Async client for BSE India's (unofficial) JSON endpoints.

BSE has no official public API; the website's own XHR endpoints under
api.bseindia.com return data, but they reject requests that don't look like a
browser and often require a cookie handshake with www.bseindia.com first.

This client:
  * sends realistic browser headers on every request,
  * warms up session cookies against the web front-end and retries once on 403,
  * normalizes the drifting response shapes into our Pydantic models,
  * keeps only an in-memory cookie jar + a few-second micro-cache (no storage).

Endpoint notes (verified against the community BseIndiaApi reference):
  * /PeerSmartSearch/w   -> returns an HTML fragment of <a> results, not JSON.
  * /getScripHeaderData/w-> JSON quote header.
  * /AnnSubCategoryGetData/w -> JSON announcements; dates are YYYYMMDD.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta

import httpx

from .config import Settings
from .http_util import MicroCache, find_value, to_float
from .models import CompanyMatch, Disclosure, StockQuote

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "Connection": "keep-alive",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# BSE serves announcement attachments from this path.
_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
_SPAN_SPLIT_RE = re.compile(r"(.*?)<span[^>]*>(.*?)</span>", re.S | re.I)
_SIXDIGIT_RE = re.compile(r"\b(\d{6})\b")
_ISIN_RE = re.compile(r"\bINE[0-9A-Z]{9}\b")


class BSEError(RuntimeError):
    """Raised when BSE cannot be reached or returns an unusable response."""


class BSEClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._cache = MicroCache(settings.micro_cache_ttl)
        self._client = httpx.AsyncClient(
            headers=_BROWSER_HEADERS,
            timeout=settings.http_timeout,
            follow_redirects=True,
        )
        self._warmed = False
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _warm_cookies(self) -> None:
        """Fetch the web front-end once so BSE sets its session cookies."""
        try:
            await self._client.get(self._s.bse_web_base)
            self._warmed = True
        except httpx.HTTPError:
            self._warmed = False  # non-fatal; the API call may still work

    async def _fetch(self, path: str, params: dict) -> httpx.Response:
        url = f"{self._s.bse_api_base}{path}"
        async with self._lock:
            if not self._warmed:
                await self._warm_cookies()

        last_exc: Exception | None = None
        for attempt in range(self._s.max_retries + 1):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code in (401, 403):
                    await self._warm_cookies()  # likely anti-bot / cookie expiry
                    resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise BSEError(
            f"BSE request failed for {path}: {last_exc}. "
            "BSE may be rate-limiting or blocking this host's IP."
        )

    async def _get_json(self, path: str, params: dict) -> object:
        cache_key = f"json:{path}?{sorted(params.items())}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        resp = await self._fetch(path, params)
        try:
            data = resp.json()
        except ValueError as exc:
            raise BSEError(f"BSE returned non-JSON for {path}: {exc}") from exc
        self._cache.set(cache_key, data)
        return data

    @staticmethod
    def _rows(data: object) -> list[dict]:
        """BSE wraps result rows under 'Table' (and friends); unwrap defensively."""
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("Table", "Table1", "data", "d"):
                value = data.get(key)
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, dict)]
        return []

    # ------------------------------------------------------------------ tools

    async def search_company(self, query: str) -> list[CompanyMatch]:
        """Resolve a company name / ticker to BSE scrip code(s).

        PeerSmartSearch returns an HTML fragment of <a> results. We also handle
        a JSON response in case BSE changes the shape.
        """
        cache_key = f"search:{query.lower()}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        resp = await self._fetch("/PeerSmartSearch/w", {"Type": "SS", "text": query})
        text = resp.text or ""
        results: list[CompanyMatch] = []

        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                for row in self._rows(resp.json()):
                    code = find_value(row, "scrip_cd", "scripcode", "scrip_code", "FScrpCd")
                    name = find_value(row, "scrip_name", "scripname", "slongname", "name")
                    if code:
                        results.append(
                            CompanyMatch(
                                scrip_code=str(code).strip(),
                                company_name=_s(name) or "",
                                symbol=_s(find_value(row, "symbol", "scrip_id", "scripid")),
                                group=_s(find_value(row, "group", "grp")),
                                status=_s(find_value(row, "status")),
                            )
                        )
            except ValueError:
                pass
        if not results:
            results = _parse_smart_search(text)

        self._cache.set(cache_key, results)
        return results

    async def get_quote(self, scrip_code: str) -> StockQuote:
        """Live quote for a scrip.

        The header endpoint carries price/OHLC; we best-effort enrich with the
        52-week range (HighLow) and traded quantity (StockTrading). Enrichment
        failures are non-fatal — the core quote still returns.
        """
        header = await self._get_json("/getScripHeaderData/w", {"scripcode": scrip_code})
        if not isinstance(header, (dict, list)):
            raise BSEError(f"Unexpected quote payload for {scrip_code}.")

        hl = await self._try_json("/HighLow/w", {"Type": "EQ", "flag": "C", "scripcode": scrip_code})
        st = await self._try_json(
            "/StockTrading/w", {"flag": "", "quotetype": "EQ", "scripcode": scrip_code}
        )

        return StockQuote(
            scrip_code=str(scrip_code),
            company=_s(find_value(header, "FullN", "SeriesN", "ScripName", "SLONGNAME")),
            symbol=_s(find_value(header, "ShortN", "Scrip_ID", "symbol")),
            ltp=to_float(find_value(header, "LTP", "CurrRate", "Cur_Rate")),
            change=to_float(find_value(header, "Chg", "Change", "PriceChange")),
            pct_change=to_float(find_value(header, "PcChg", "PerChg", "ChangePercent")),
            open=to_float(find_value(header, "Open", "OpenRate")),
            high=to_float(find_value(header, "High", "DayHigh")),
            low=to_float(find_value(header, "Low", "DayLow")),
            prev_close=to_float(find_value(header, "PrevClose", "Prev_Close", "PreviousClose")),
            volume=_scaled_qty(find_value(st, "TTQ", "TradedQty"), find_value(st, "TTQin")),
            week52_high=to_float(find_value(hl, "Fifty2WkHigh_adj", "FiftyTwoWeekHigh", "WeekHigh")),
            week52_low=to_float(find_value(hl, "Fifty2WkLow_adj", "FiftyTwoWeekLow", "WeekLow")),
            as_of=_s(find_value(header, "Ason", "UpdatedOn", "TradeDate")),
        )

    async def _try_json(self, path: str, params: dict) -> object:
        """Best-effort JSON fetch for optional enrichment; returns {} on failure."""
        try:
            return await self._get_json(path, params)
        except BSEError:
            return {}

    async def list_disclosures(
        self,
        scrip_code: str,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> list[Disclosure]:
        """Corporate announcements/disclosures for a scrip in a date window.

        Dates are sent to BSE as YYYYMMDD. Defaults to the last 7 days.
        """
        to_d = to_date or date.today()
        from_d = from_date or (to_d - timedelta(days=7))
        params = {
            "pageno": 1,
            "strCat": category or "-1",
            "subcategory": "-1",
            "strPrevDate": from_d.strftime("%Y%m%d"),
            "strToDate": to_d.strftime("%Y%m%d"),
            "strSearch": "P",
            "strscrip": scrip_code,
            "strType": "C",
        }
        data = await self._get_json("/AnnSubCategoryGetData/w", params)
        results: list[Disclosure] = []
        for row in self._rows(data):
            headline = find_value(row, "NEWSSUB", "HEADLINE", "Headline", "News_submission")
            if not headline:
                continue
            attachment = _s(find_value(row, "ATTACHMENTNAME", "Attachmentname", "attachment"))
            pdf_url = f"{_ATTACH_BASE}{attachment}" if attachment else None
            results.append(
                Disclosure(
                    date_time=_s(find_value(row, "NEWS_DT", "News_dt", "DissemDT", "dt_tm")),
                    headline=str(headline).strip(),
                    category=_s(find_value(row, "CATEGORYNAME", "Category", "CATEGORY")),
                    subcategory=_s(find_value(row, "SUBCATNAME", "SubCategory", "SUBCATEGORY")),
                    pdf_url=pdf_url,
                    more_detail=_s(find_value(row, "MORE", "More", "NEWSBODY", "DETAILS")),
                )
            )
            if len(results) >= limit:
                break
        return results


def _parse_smart_search(html: str) -> list[CompanyMatch]:
    """Parse the PeerSmartSearch HTML fragment into CompanyMatch rows.

    Each result is an <a> whose visible text carries the company name, and whose
    nested <span> carries 'SYMBOL | ISIN | SCRIP_CODE | STATUS' (order varies).
    We locate the 6-digit scrip code and ISIN robustly rather than by position.
    """
    results: list[CompanyMatch] = []
    seen: set[str] = set()
    for anchor in _ANCHOR_RE.findall(html):
        span_match = _SPAN_SPLIT_RE.search(anchor)
        if span_match:
            name = _strip_tags(span_match.group(1))
            meta = _strip_tags(span_match.group(2))
        else:
            name = _strip_tags(anchor)
            meta = ""
        blob = f"{name} {meta}"
        code_match = _SIXDIGIT_RE.search(blob)
        if not code_match:
            continue
        code = code_match.group(1)
        if code in seen:
            continue
        seen.add(code)

        parts = [p.strip() for p in re.split(r"[|]", meta) if p.strip()]
        symbol = None
        status = None
        for part in parts:
            if _SIXDIGIT_RE.fullmatch(part) or _ISIN_RE.fullmatch(part):
                continue
            if part.lower() in ("active", "suspended", "delisted"):
                status = part
            elif symbol is None and len(part) <= 20 and part.upper() == part:
                symbol = part
        results.append(
            CompanyMatch(
                scrip_code=code,
                company_name=name.strip(),
                symbol=symbol,
                group=None,
                status=status,
            )
        )
    return results


def _scaled_qty(value: object, unit: object) -> int | None:
    """Convert BSE's traded quantity to a share count.

    BSE reports traded quantity with a separate unit label, e.g. TTQ "19.15" with
    TTQin "(Lakh)". 1 lakh = 1e5, 1 crore = 1e7. The result is therefore rounded
    to the unit's precision (approximate for large counts).
    """
    amount = to_float(value)
    if amount is None:
        return None
    label = str(unit or "").lower()
    if "cr" in label:
        amount *= 1e7
    elif "lakh" in label or "lac" in label:
        amount *= 1e5
    return int(round(amount))


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub(" ", text).replace("\xa0", " ").strip()


def _s(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
