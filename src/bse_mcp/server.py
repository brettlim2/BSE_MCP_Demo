"""FastMCP server exposing BSE + Meltwater MIRA tools over Streamable HTTP.

Run locally:
    MCP_API_KEYS=dev-key uv run bse-mcp            # or: python -m bse_mcp.server

All data is fetched live per call and never stored.
"""

from __future__ import annotations

from datetime import date, datetime

from mcp.server.mcpserver import MCPServer

from .bse_client import BSEClient, BSEError
from .config import settings
from .mira_client import MiraClient, MiraError

INSTRUCTIONS = """\
Live, read-only access to Indian capital-markets data. Three sources, no storage —
every call fetches fresh from the upstream API.

Typical flow:
  1. Call `bse_search_company` with a name or ticker to get the numeric `scrip_code`.
  2. Use that `scrip_code` with `bse_get_stock_price` (live quote) or
     `bse_list_disclosures` (corporate announcements/filings).
For market/PR intelligence grounded on Meltwater content, use `mira_ask`
(optionally scoped to a project from `mira_list_projects`).

Notes: BSE prices are INR and reflect Indian market hours (IST). Disclosure text
and attachments come straight from bseindia.com. MIRA answers include citations.
"""

mcp = MCPServer(
    name="BSE + Meltwater MIRA",
    instructions=INSTRUCTIONS,
)

# Lazily-created, process-lifetime clients (in-memory state only).
_bse: BSEClient | None = None
_mira: MiraClient | None = None


def _bse_client() -> BSEClient:
    global _bse
    if _bse is None:
        _bse = BSEClient(settings)
    return _bse


def _mira_client() -> MiraClient:
    global _mira
    if _mira is None:
        _mira = MiraClient(settings)
    return _mira


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Date must be ISO format YYYY-MM-DD, got {value!r}.") from exc


# ----------------------------------------------------------------- BSE tools


@mcp.tool()
async def bse_search_company(query: str) -> list[dict]:
    """Search BSE-listed companies by name or ticker and return matches with their
    BSE `scrip_code`.

    This is the entry point: `bse_get_stock_price` and `bse_list_disclosures` both
    require a `scrip_code`, which you obtain here.

    Args:
        query: Company name or ticker, e.g. "Reliance", "TCS", "INFY".

    Returns a list of {scrip_code, company_name, symbol, group, status}.
    """
    try:
        matches = await _bse_client().search_company(query)
    except BSEError as exc:
        return [{"error": str(exc)}]
    return [m.model_dump() for m in matches]


@mcp.tool()
async def bse_get_stock_price(scrip_code: str) -> dict:
    """Get the current (live) BSE quote for a security.

    Args:
        scrip_code: Numeric BSE scrip code (from `bse_search_company`), e.g. "500325".

    Returns {scrip_code, company, ltp, change, pct_change, open, high, low,
    prev_close, volume, week52_high, week52_low, as_of}. Prices are INR; `ltp` is
    the last traded price. Values reflect Indian market hours (IST).
    """
    try:
        quote = await _bse_client().get_quote(scrip_code)
    except BSEError as exc:
        return {"error": str(exc)}
    return quote.model_dump()


@mcp.tool()
async def bse_list_disclosures(
    scrip_code: str,
    from_date: str | None = None,
    to_date: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List corporate disclosures / announcements filed by a company on BSE.

    Args:
        scrip_code: Numeric BSE scrip code (from `bse_search_company`).
        from_date: Start date, ISO "YYYY-MM-DD". Defaults to 7 days before to_date.
        to_date: End date, ISO "YYYY-MM-DD". Defaults to today.
        category: Optional BSE category filter (e.g. "Result", "Board Meeting").
            Omit for all categories.
        limit: Max rows to return (default 20).

    Returns a list of {date_time, headline, category, subcategory, pdf_url,
    more_detail}. `pdf_url` links to the filing attachment on bseindia.com.
    """
    try:
        disclosures = await _bse_client().list_disclosures(
            scrip_code=scrip_code,
            from_date=_parse_date(from_date),
            to_date=_parse_date(to_date),
            category=category,
            limit=limit,
        )
    except (BSEError, ValueError) as exc:
        return [{"error": str(exc)}]
    return [d.model_dump() for d in disclosures]


# ------------------------------------------------------------ Meltwater tools


@mcp.tool()
async def mira_ask(
    question: str,
    project_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Ask Meltwater MIRA a question and get a grounded answer with citations.

    MIRA is Meltwater's AI assistant grounded on news/social media content — use it
    for brand, PR, competitive, and market-sentiment questions.

    Args:
        question: The natural-language question to ask.
        project_id: Optional MIRA project to scope grounding (see `mira_list_projects`).
        thread_id: Optional id returned by a previous call to continue the conversation.

    Returns {answer, citations: [{title, url, type}], thread_id}. Pass the returned
    `thread_id` back on your next call to keep context.
    """
    try:
        result = await _mira_client().ask(question, project_id=project_id, thread_id=thread_id)
    except MiraError as exc:
        return {"error": str(exc)}
    return result.model_dump()


@mcp.tool()
async def mira_list_projects() -> list[dict]:
    """List available Meltwater MIRA projects (grounding scopes).

    Use a returned project `id` as the `project_id` argument to `mira_ask` to scope
    answers to that project's content.

    Returns a list of {id, name}.
    """
    try:
        projects = await _mira_client().list_projects()
    except MiraError as exc:
        return [{"error": str(exc)}]
    return [p.model_dump() for p in projects]


# --------------------------------------------------------------------- app


def build_app():
    """Build the Streamable-HTTP ASGI app with the API-key gate and health route."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from .auth import APIKeyMiddleware

    app_kwargs: dict = {"streamable_http_path": settings.mount_path}
    # Opt-in Host/Origin validation: only when the operator pins allowed hosts
    # (e.g. their public domain). Otherwise leave the SDK's HTTP default in place.
    if settings.allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        app_kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins or settings.allowed_hosts,
        )
    app = mcp.streamable_http_app(**app_kwargs)

    async def healthz(_request):
        return JSONResponse(
            {
                "status": "ok",
                "auth": "enabled" if settings.auth_enabled else "disabled",
                "meltwater": "configured" if settings.meltwater_api_key else "unset",
            }
        )

    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    app.add_middleware(APIKeyMiddleware, settings=settings)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
