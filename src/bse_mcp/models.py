"""Pydantic models for tool outputs.

These give MCP clients a stable, documented schema regardless of how messy the
upstream BSE/Meltwater payloads are. They are return shapes only — we never
persist them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CompanyMatch(BaseModel):
    """A single result from a BSE company search."""

    scrip_code: str = Field(description="BSE scrip (security) code, e.g. '500325'.")
    company_name: str = Field(description="Full company name.")
    symbol: str | None = Field(default=None, description="BSE ticker symbol, if available.")
    group: str | None = Field(default=None, description="BSE group (e.g. 'A', 'B').")
    status: str | None = Field(default=None, description="Listing status (e.g. 'Active').")


class StockQuote(BaseModel):
    """A live BSE quote. All numbers are as reported by BSE at `as_of`."""

    scrip_code: str
    company: str | None = None
    symbol: str | None = None
    ltp: float | None = Field(default=None, description="Last traded price (INR).")
    change: float | None = Field(default=None, description="Absolute change vs previous close.")
    pct_change: float | None = Field(default=None, description="Percent change vs previous close.")
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: int | None = Field(default=None, description="Traded quantity.")
    week52_high: float | None = None
    week52_low: float | None = None
    as_of: str | None = Field(default=None, description="Timestamp/trade date reported by BSE.")


class Disclosure(BaseModel):
    """A single corporate announcement/filing."""

    date_time: str | None = Field(default=None, description="Announcement date/time (as reported).")
    headline: str = Field(description="Announcement headline/subject.")
    category: str | None = None
    subcategory: str | None = None
    pdf_url: str | None = Field(default=None, description="Direct URL to the attachment, if any.")
    more_detail: str | None = Field(default=None, description="Longer text / body snippet.")


class Citation(BaseModel):
    """A grounding citation returned by Meltwater MIRA."""

    title: str | None = None
    url: str | None = None
    type: str | None = Field(default=None, description="Citation type: news, social, or generic.")


class MiraAnswer(BaseModel):
    """A grounded answer from Meltwater MIRA."""

    answer: str = Field(description="Plain-text answer (output_text).")
    citations: list[Citation] = Field(default_factory=list)
    thread_id: str | None = Field(
        default=None,
        description="Pass this back on the next mira_ask call to continue the conversation.",
    )


class MiraProject(BaseModel):
    """A MIRA project (grounding scope)."""

    id: str
    name: str | None = None
