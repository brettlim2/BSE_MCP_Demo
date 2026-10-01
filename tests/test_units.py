"""Offline unit tests — no network. Run with: pytest

Cover the fragile bits: BSE HTML/JSON parsing, number coercion, MIRA response
extraction, and tool registration.
"""

import asyncio

from bse_mcp.bse_client import _parse_smart_search, _scaled_qty
from bse_mcp.http_util import find_value, to_float, to_int
from bse_mcp.mira_client import _extract_citations, _extract_output_text
from bse_mcp.server import build_app, mcp


def test_parse_smart_search():
    html = (
        "<li><a href='/x/500325/'>RELIANCE INDUSTRIES LTD"
        "<span>RELIANCE | INE002A01018 | 500325 | Active</span></a></li>"
        "<li><a href='/x/532540/'>TATA CONSULTANCY SERVICES LTD"
        "<span>TCS | INE467B01029 | 532540 | Active</span></a></li>"
    )
    matches = _parse_smart_search(html)
    assert [m.scrip_code for m in matches] == ["500325", "532540"]
    assert matches[0].symbol == "RELIANCE"
    assert matches[0].status == "Active"
    assert "RELIANCE" in matches[0].company_name


def test_number_coercion():
    assert to_float("2,345.60") == 2345.60
    assert to_float("-") is None
    assert to_float(None) is None
    assert to_int("1,000") == 1000


def test_find_value_nested():
    data = {"Header": {"ScripName": "X", "LTP": "10.5"}, "a": {"b": {"PcChg": "1.2"}}}
    assert find_value(data, "ltp") == "10.5"
    assert find_value(data, "pcchg") == "1.2"
    assert find_value(data, "missing") is None


def test_scaled_qty():
    assert _scaled_qty("19.15", "(Lakh)") == 1915000
    assert _scaled_qty("2.5", "(Cr.)") == 25000000
    assert _scaled_qty("100", "") == 100
    assert _scaled_qty(None, "(Lakh)") is None


def test_mira_extraction_and_dedup():
    data = {
        "output": [{"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}],
        "annotations": [
            {"title": "R", "url": "https://r", "type": "news"},
            {"title": "R", "url": "https://r", "type": "news"},
        ],
    }
    assert _extract_output_text(data) == "A\nB"
    cites = _extract_citations(data)
    assert len(cites) == 1 and cites[0].type == "news"


def test_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == [
        "bse_get_stock_price",
        "bse_list_disclosures",
        "bse_search_company",
        "mira_ask",
        "mira_list_projects",
    ]


def test_health_route_present():
    app = build_app()
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/healthz" in paths
